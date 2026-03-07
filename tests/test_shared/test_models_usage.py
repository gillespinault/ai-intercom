"""Tests for usage stats models."""
from src.shared.models import BlockStats, WeeklyStats, SessionContextStats, UsageStatsPayload


def test_block_stats_defaults():
    b = BlockStats(
        start_time="2026-03-04T14:00:00Z",
        end_time="2026-03-04T19:00:00Z",
        elapsed_pct=68.0,
        remaining_minutes=96,
        reset_time="19:00",
        is_active=True,
    )
    assert b.elapsed_pct == 68.0
    assert b.is_active is True


def test_weekly_stats():
    w = WeeklyStats(total_tokens=1_200_000_000, display="1.2B")
    assert w.display == "1.2B"


def test_session_context_stats():
    s = SessionContextStats(context_percent=45.7, context_tokens=91398)
    assert 0 <= s.context_percent <= 100


def test_usage_stats_payload():
    payload = UsageStatsPayload(
        block=BlockStats(
            start_time="2026-03-04T14:00:00Z",
            end_time="2026-03-04T19:00:00Z",
            elapsed_pct=50.0,
            remaining_minutes=150,
            reset_time="19:00",
            is_active=True,
        ),
        weekly=WeeklyStats(total_tokens=500_000_000, display="500M"),
        sessions={"sess-1": SessionContextStats(context_percent=33.0, context_tokens=66000)},
    )
    assert payload.sessions["sess-1"].context_percent == 33.0
