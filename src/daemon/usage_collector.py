"""Collects Claude Code usage statistics.

Reads JSONL transcripts to determine per-session context window usage,
billing block timing, and weekly token totals — all from local files,
with no external subprocess dependencies.

Usage::

    collector = UsageCollector()
    await collector.run()   # background loop every 60s
    collector.stop()        # signals the loop to exit
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from src.shared.models import (
    BlockStats,
    SessionContextStats,
    UsageStatsPayload,
    WeeklyStats,
)

logger = logging.getLogger(__name__)

# Maximum context window size for Claude (tokens).
_MAX_CONTEXT_TOKENS = 200_000

# How many bytes to read from the end of a JSONL transcript to find the
# last assistant message.  64 KB is generous for a single turn.
_TAIL_BYTES = 64 * 1024

# Polling interval for the background collection loop (seconds).
_POLL_INTERVAL = 60

# Billing block window duration (Anthropic's 5-hour rolling window).
_BLOCK_HOURS = 5

# Default JSONL projects directory.
_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


class UsageCollector:
    """Collects and formats Claude Code usage statistics."""

    def __init__(self) -> None:
        self._stop_event: asyncio.Event | None = None
        self._latest: UsageStatsPayload | None = None

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def format_tokens(n: int) -> str:
        """Format a token count into a compact human-readable string.

        Examples: 0 -> "0", 999 -> "999", 1500 -> "1.5K",
        1200000 -> "1.2M", 1200000000 -> "1.2B".
        """
        if n < 1_000:
            return str(n)
        if n < 1_000_000:
            value = n / 1_000
            return f"{value:.1f}K".replace(".0K", "K")
        if n < 1_000_000_000:
            value = n / 1_000_000
            return f"{value:.1f}M".replace(".0M", "M")
        value = n / 1_000_000_000
        return f"{value:.1f}B".replace(".0B", "B")

    # ------------------------------------------------------------------
    # JSONL transcript parsing (per-session context %)
    # ------------------------------------------------------------------

    def get_context_percent(self, transcript_path: str) -> SessionContextStats | None:
        """Parse a JSONL transcript and return context window usage.

        Reads the last ``_TAIL_BYTES`` of the file, finds the last
        assistant message with ``usage.cache_read_input_tokens``, and
        computes the percentage of the 200K context window used.

        Returns ``None`` if the file is empty or no usage data is found.
        """
        try:
            file_size = os.path.getsize(transcript_path)
            if file_size == 0:
                return None

            with open(transcript_path, "rb") as fh:
                # Seek to tail region
                offset = max(0, file_size - _TAIL_BYTES)
                fh.seek(offset)
                tail = fh.read().decode("utf-8", errors="replace")

            # Parse all complete lines from the tail
            lines = tail.strip().split("\n")
            # If we seeked into the middle of a line, drop the first partial one
            if offset > 0:
                lines = lines[1:]

            last_cache_tokens: int | None = None

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "assistant":
                    continue

                msg = entry.get("message")
                if not isinstance(msg, dict):
                    continue

                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                cache_read = usage.get("cache_read_input_tokens")
                if cache_read is not None:
                    last_cache_tokens = int(cache_read)

            if last_cache_tokens is None:
                return None

            pct = (last_cache_tokens / _MAX_CONTEXT_TOKENS) * 100.0
            return SessionContextStats(
                context_tokens=last_cache_tokens,
                context_percent=round(pct, 1),
            )

        except (OSError, ValueError) as exc:
            logger.warning("Failed to parse transcript %s: %s", transcript_path, exc)
            return None

    # ------------------------------------------------------------------
    # JSONL scanning for usage entries
    # ------------------------------------------------------------------

    @staticmethod
    def _scan_usage_entries(since: datetime) -> list[tuple[datetime, int, int]]:
        """Scan all JSONL transcripts for assistant usage entries since *since*.

        Returns a list of ``(timestamp, input_tokens, output_tokens)`` tuples
        sorted by timestamp ascending.  Only files modified after *since* are
        read, and within each file only the relevant entries are kept.
        """
        since_ts = since.timestamp()
        results: list[tuple[datetime, int, int]] = []

        for path in glob.glob(os.path.join(_PROJECTS_DIR, "*", "*.jsonl")):
            try:
                if os.path.getmtime(path) < since_ts:
                    continue
            except OSError:
                continue

            try:
                with open(path) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if entry.get("type") != "assistant":
                            continue

                        ts_str = entry.get("timestamp")
                        if not ts_str:
                            continue

                        msg = entry.get("message")
                        if not isinstance(msg, dict):
                            continue
                        usage = msg.get("usage")
                        if not isinstance(usage, dict):
                            continue

                        try:
                            ts = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            )
                        except ValueError:
                            continue

                        if ts < since:
                            continue

                        inp = int(usage.get("input_tokens", 0))
                        out = int(usage.get("output_tokens", 0))
                        results.append((ts, inp, out))
            except OSError as exc:
                logger.debug("Cannot read transcript %s: %s", path, exc)

        results.sort(key=lambda x: x[0])
        return results

    # ------------------------------------------------------------------
    # Block stats (5-hour billing window)
    # ------------------------------------------------------------------

    def compute_block_stats(self) -> BlockStats:
        """Compute the current billing block from JSONL transcripts.

        A block starts at the timestamp of the first assistant usage entry
        within the last 5 hours.  If no entries exist, no block is active.
        """
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=_BLOCK_HOURS)

        entries = self._scan_usage_entries(since=window_start)
        if not entries:
            return BlockStats(is_active=False)

        block_start = entries[0][0]
        block_end = block_start + timedelta(hours=_BLOCK_HOURS)

        total_seconds = (block_end - block_start).total_seconds()
        elapsed_seconds = (now - block_start).total_seconds()
        elapsed_pct = min(100.0, max(0.0, (elapsed_seconds / total_seconds) * 100.0))

        remaining_seconds = max(0, (block_end - now).total_seconds())
        remaining_minutes = int(remaining_seconds / 60)

        local_end = block_end.astimezone()
        reset_time = local_end.strftime("%H:%M")

        return BlockStats(
            start_time=block_start.isoformat(),
            end_time=block_end.isoformat(),
            elapsed_pct=round(elapsed_pct, 1),
            remaining_minutes=remaining_minutes,
            reset_time=reset_time,
            is_active=True,
        )

    # ------------------------------------------------------------------
    # Weekly stats (7-day token totals)
    # ------------------------------------------------------------------

    def compute_weekly_stats(self) -> WeeklyStats:
        """Compute total tokens used in the last 7 days from JSONL transcripts."""
        now = datetime.now(timezone.utc)
        week_start = now - timedelta(days=7)

        entries = self._scan_usage_entries(since=week_start)
        total = sum(inp + out for _, inp, out in entries)

        return WeeklyStats(
            total_tokens=total,
            display=self.format_tokens(total),
        )

    # ------------------------------------------------------------------
    # Async collection
    # ------------------------------------------------------------------

    async def collect_stats(self) -> tuple[BlockStats, WeeklyStats]:
        """Compute block and weekly stats in a thread executor.

        Returns a tuple of ``(BlockStats, WeeklyStats)``.
        The JSONL scan is done once for 7 days; block stats filter from that.
        """
        loop = asyncio.get_running_loop()

        def _collect() -> tuple[BlockStats, WeeklyStats]:
            # Single scan for 7 days covers both block (5h) and weekly needs
            now = datetime.now(timezone.utc)
            week_start = now - timedelta(days=7)
            entries = self._scan_usage_entries(since=week_start)

            # Block stats: filter to last 5 hours
            window_start = now - timedelta(hours=_BLOCK_HOURS)
            recent = [e for e in entries if e[0] >= window_start]

            if recent:
                block_start = recent[0][0]
                block_end = block_start + timedelta(hours=_BLOCK_HOURS)
                total_s = (block_end - block_start).total_seconds()
                elapsed_s = (now - block_start).total_seconds()
                elapsed_pct = min(100.0, max(0.0, (elapsed_s / total_s) * 100.0))
                remaining_s = max(0, (block_end - now).total_seconds())
                local_end = block_end.astimezone()
                block = BlockStats(
                    start_time=block_start.isoformat(),
                    end_time=block_end.isoformat(),
                    elapsed_pct=round(elapsed_pct, 1),
                    remaining_minutes=int(remaining_s / 60),
                    reset_time=local_end.strftime("%H:%M"),
                    is_active=True,
                )
            else:
                block = BlockStats(is_active=False)

            # Weekly stats: sum all entries
            total_tokens = sum(inp + out for _, inp, out in entries)
            weekly = WeeklyStats(
                total_tokens=total_tokens,
                display=UsageCollector.format_tokens(total_tokens),
            )

            return block, weekly

        return await loop.run_in_executor(None, _collect)

    def build_payload(
        self,
        session_contexts: dict[str, SessionContextStats],
    ) -> UsageStatsPayload:
        """Combine block + weekly + per-session context into a payload.

        ``session_contexts`` maps session ID to its context stats.
        Block and weekly stats come from the latest ``collect_stats``
        results cached in ``self._latest``.
        """
        block = BlockStats()
        weekly = WeeklyStats()

        if self._latest:
            block = self._latest.block
            weekly = self._latest.weekly

        return UsageStatsPayload(
            block=block,
            weekly=weekly,
            sessions=session_contexts,
        )

    async def run(self) -> None:
        """Background loop that collects usage stats every 60 seconds."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()

        logger.info("UsageCollector started (poll every %ds)", _POLL_INTERVAL)

        while not self._stop_event.is_set():
            try:
                block_stats, weekly_stats = await self.collect_stats()
                self._latest = UsageStatsPayload(
                    block=block_stats,
                    weekly=weekly_stats,
                )
                logger.debug(
                    "Usage stats collected: block_active=%s weekly=%s",
                    block_stats.is_active,
                    weekly_stats.display,
                )
            except Exception:
                logger.exception("Error collecting usage stats")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=_POLL_INTERVAL)
                break  # stop event was set
            except asyncio.TimeoutError:
                pass  # normal timeout, loop again

        logger.info("UsageCollector stopped")

    def stop(self) -> None:
        """Signal the background loop to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
