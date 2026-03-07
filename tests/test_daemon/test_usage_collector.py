"""Tests for the daemon usage collector."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.daemon.usage_collector import UsageCollector, _PROJECTS_DIR


def test_format_tokens_compact():
    assert UsageCollector.format_tokens(0) == "0"
    assert UsageCollector.format_tokens(999) == "999"
    assert UsageCollector.format_tokens(1_500) == "1.5K"
    assert UsageCollector.format_tokens(1_200_000) == "1.2M"
    assert UsageCollector.format_tokens(1_200_000_000) == "1.2B"


def test_parse_context_from_jsonl():
    """Parsing last assistant message extracts cache_read_input_tokens."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "user", "message": "hello"}) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 80000,
                    "cache_creation_input_tokens": 200,
                    "output_tokens": 100,
                }
            },
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 3,
                    "cache_read_input_tokens": 120000,
                    "cache_creation_input_tokens": 500,
                    "output_tokens": 50,
                }
            },
        }) + "\n")
        path = f.name

    try:
        collector = UsageCollector()
        result = collector.get_context_percent(path)
        assert result is not None
        assert result.context_tokens == 120000
        assert abs(result.context_percent - 60.0) < 0.1
    finally:
        os.unlink(path)


def test_parse_context_empty_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        collector = UsageCollector()
        assert collector.get_context_percent(path) is None
    finally:
        os.unlink(path)


def test_parse_context_no_usage():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": "hi"}}) + "\n")
        path = f.name
    try:
        collector = UsageCollector()
        assert collector.get_context_percent(path) is None
    finally:
        os.unlink(path)


def _make_jsonl_entry(ts: datetime, input_tokens: int = 100, output_tokens: int = 50) -> str:
    """Create a JSONL line for an assistant entry with usage."""
    return json.dumps({
        "type": "assistant",
        "timestamp": ts.isoformat(),
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
            }
        },
    }) + "\n"


def test_scan_usage_entries():
    """Scan finds assistant entries with usage from JSONL files."""
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = os.path.join(tmpdir, "test-project")
        os.makedirs(project_dir)
        jsonl_path = os.path.join(project_dir, "session1.jsonl")

        with open(jsonl_path, "w") as f:
            # Entry from 2 hours ago
            f.write(_make_jsonl_entry(now - timedelta(hours=2), 100, 50))
            # Entry from 30 min ago
            f.write(_make_jsonl_entry(now - timedelta(minutes=30), 200, 80))
            # Non-assistant entry (should be skipped)
            f.write(json.dumps({"type": "user", "timestamp": now.isoformat()}) + "\n")

        with patch("src.daemon.usage_collector._PROJECTS_DIR", tmpdir):
            entries = UsageCollector._scan_usage_entries(since=now - timedelta(hours=3))

        assert len(entries) == 2
        assert entries[0][1] == 100  # first entry input_tokens
        assert entries[1][1] == 200  # second entry input_tokens


def test_scan_skips_old_entries():
    """Entries older than 'since' are excluded."""
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = os.path.join(tmpdir, "test-project")
        os.makedirs(project_dir)
        jsonl_path = os.path.join(project_dir, "session1.jsonl")

        with open(jsonl_path, "w") as f:
            # Old entry (10 hours ago)
            f.write(_make_jsonl_entry(now - timedelta(hours=10), 500, 200))
            # Recent entry (1 hour ago)
            f.write(_make_jsonl_entry(now - timedelta(hours=1), 100, 50))

        with patch("src.daemon.usage_collector._PROJECTS_DIR", tmpdir):
            entries = UsageCollector._scan_usage_entries(since=now - timedelta(hours=5))

        assert len(entries) == 1
        assert entries[0][1] == 100


def test_compute_block_stats_active():
    """Block is active when there are entries in the last 5 hours."""
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = os.path.join(tmpdir, "test-project")
        os.makedirs(project_dir)
        jsonl_path = os.path.join(project_dir, "session1.jsonl")

        with open(jsonl_path, "w") as f:
            f.write(_make_jsonl_entry(now - timedelta(hours=2), 100, 50))
            f.write(_make_jsonl_entry(now - timedelta(minutes=30), 200, 80))

        with patch("src.daemon.usage_collector._PROJECTS_DIR", tmpdir):
            collector = UsageCollector()
            result = collector.compute_block_stats()

        assert result.is_active is True
        assert 0 < result.elapsed_pct <= 100
        assert result.remaining_minutes > 0
        assert result.reset_time  # non-empty HH:MM


def test_compute_block_stats_inactive():
    """No block when no entries in the last 5 hours."""
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = os.path.join(tmpdir, "test-project")
        os.makedirs(project_dir)
        jsonl_path = os.path.join(project_dir, "session1.jsonl")

        with open(jsonl_path, "w") as f:
            # Entry from 8 hours ago — outside 5h window
            f.write(_make_jsonl_entry(now - timedelta(hours=8), 100, 50))

        with patch("src.daemon.usage_collector._PROJECTS_DIR", tmpdir):
            collector = UsageCollector()
            result = collector.compute_block_stats()

        assert result.is_active is False


def test_compute_weekly_stats():
    """Weekly stats sum tokens from the last 7 days."""
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = os.path.join(tmpdir, "test-project")
        os.makedirs(project_dir)
        jsonl_path = os.path.join(project_dir, "session1.jsonl")

        with open(jsonl_path, "w") as f:
            # 3 entries: 150 + 280 + 300 = 730 total tokens
            f.write(_make_jsonl_entry(now - timedelta(days=1), 100, 50))
            f.write(_make_jsonl_entry(now - timedelta(days=3), 200, 80))
            f.write(_make_jsonl_entry(now - timedelta(hours=2), 150, 150))

        with patch("src.daemon.usage_collector._PROJECTS_DIR", tmpdir):
            collector = UsageCollector()
            result = collector.compute_weekly_stats()

        assert result.total_tokens == 730
        assert result.display == "730"


def test_compute_weekly_stats_excludes_old():
    """Entries older than 7 days are excluded from weekly total."""
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = os.path.join(tmpdir, "test-project")
        os.makedirs(project_dir)
        jsonl_path = os.path.join(project_dir, "session1.jsonl")

        with open(jsonl_path, "w") as f:
            # Old entry (10 days ago)
            f.write(_make_jsonl_entry(now - timedelta(days=10), 999999, 999999))
            # Recent entry
            f.write(_make_jsonl_entry(now - timedelta(hours=1), 100, 50))

        with patch("src.daemon.usage_collector._PROJECTS_DIR", tmpdir):
            collector = UsageCollector()
            result = collector.compute_weekly_stats()

        assert result.total_tokens == 150
