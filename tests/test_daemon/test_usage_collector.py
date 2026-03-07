"""Tests for the daemon usage collector."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.daemon.usage_collector import UsageCollector


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


def test_parse_block_stats_from_json():
    blocks_json = {
        "blocks": [
            {
                "id": "2026-03-04T09:00:00.000Z",
                "startTime": "2026-03-04T09:00:00.000Z",
                "endTime": "2026-03-04T14:00:00.000Z",
                "isActive": False,
                "entries": 100,
                "totalTokens": 24000000,
            },
            {
                "id": "2026-03-04T14:00:00.000Z",
                "startTime": "2026-03-04T14:00:00.000Z",
                "endTime": "2026-03-04T19:00:00.000Z",
                "isActive": True,
                "entries": 200,
                "totalTokens": 50000000,
                "projection": {
                    "remainingMinutes": 96,
                    "totalTokens": 300000000,
                    "totalCost": 100.0,
                },
            },
        ],
        "totals": {},
    }
    collector = UsageCollector()
    result = collector.parse_block_stats(blocks_json)
    assert result is not None
    assert result.is_active is True
    assert result.remaining_minutes == 96
    assert result.start_time == "2026-03-04T14:00:00.000Z"
    assert result.end_time == "2026-03-04T19:00:00.000Z"
    assert 0 < result.elapsed_pct <= 100


def test_parse_block_stats_no_active():
    blocks_json = {
        "blocks": [
            {"isActive": False, "startTime": "x", "endTime": "y"},
        ],
        "totals": {},
    }
    collector = UsageCollector()
    result = collector.parse_block_stats(blocks_json)
    assert result.is_active is False


def test_parse_weekly_stats_from_json():
    weekly_json = {
        "weekly": [
            {"week": "2026-02-22", "totalTokens": 500000000},
            {"week": "2026-03-01", "totalTokens": 1200000000},
        ],
        "totals": {},
    }
    collector = UsageCollector()
    result = collector.parse_weekly_stats(weekly_json)
    assert result.total_tokens == 1200000000
    assert result.display == "1.2B"
