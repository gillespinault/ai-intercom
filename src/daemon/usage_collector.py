"""Collects Claude Code usage statistics.

Reads JSONL transcripts to determine per-session context window usage,
and runs ``ccusage`` CLI to gather billing block and weekly token stats.

Usage::

    collector = UsageCollector()
    await collector.run()   # background loop every 60s
    collector.stop()        # signals the loop to exit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

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
    # ccusage CLI interaction
    # ------------------------------------------------------------------

    def _run_ccusage(self, *args: str) -> dict | None:
        """Run ``ccusage`` via npx with nvm sourced, return parsed JSON.

        Shells out to bash, sources nvm.sh, then runs
        ``npx -y ccusage@latest <args> --json``.  Returns the parsed
        JSON dict on success, ``None`` on failure.
        """
        nvm_dir = os.environ.get("NVM_DIR", os.path.expanduser("~/.nvm"))
        nvm_sh = os.path.join(nvm_dir, "nvm.sh")

        cmd_parts = ["npx", "-y", "ccusage@latest", *args, "--json"]
        shell_cmd = f'source "{nvm_sh}" 2>/dev/null; {" ".join(cmd_parts)}'

        try:
            result = subprocess.run(
                ["bash", "-c", shell_cmd],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning("ccusage %s failed (rc=%d): %s", args, result.returncode, result.stderr[:200])
                return None

            return json.loads(result.stdout)

        except subprocess.TimeoutExpired:
            logger.warning("ccusage %s timed out", args)
            return None
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("ccusage %s error: %s", args, exc)
            return None

    # ------------------------------------------------------------------
    # Block stats parsing
    # ------------------------------------------------------------------

    def parse_block_stats(self, blocks_json: dict) -> BlockStats:
        """Parse ccusage blocks JSON into a ``BlockStats`` model.

        Finds the active block (if any), computes elapsed percentage
        from start/end times, and extracts remaining minutes from the
        projection.  Also computes ``reset_time`` as local HH:MM of the
        block end.
        """
        blocks = blocks_json.get("blocks", [])

        # Find the active block
        active = None
        for block in blocks:
            if block.get("isActive"):
                active = block
                break

        if active is None:
            return BlockStats(is_active=False)

        start_str = active.get("startTime", "")
        end_str = active.get("endTime", "")

        # Compute elapsed percentage
        elapsed_pct = 0.0
        reset_time = ""
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)

            total_seconds = (end_dt - start_dt).total_seconds()
            elapsed_seconds = (now - start_dt).total_seconds()

            if total_seconds > 0:
                elapsed_pct = min(100.0, max(0.0, (elapsed_seconds / total_seconds) * 100.0))

            # Reset time as local HH:MM
            local_end = end_dt.astimezone()
            reset_time = local_end.strftime("%H:%M")
        except (ValueError, OverflowError) as exc:
            logger.debug("Could not compute block timing: %s", exc)

        # Extract remaining minutes from projection
        projection = active.get("projection", {})
        remaining_minutes = int(projection.get("remainingMinutes", 0))

        return BlockStats(
            start_time=start_str,
            end_time=end_str,
            elapsed_pct=round(elapsed_pct, 1),
            remaining_minutes=remaining_minutes,
            reset_time=reset_time,
            is_active=True,
        )

    # ------------------------------------------------------------------
    # Weekly stats parsing
    # ------------------------------------------------------------------

    def parse_weekly_stats(self, weekly_json: dict) -> WeeklyStats:
        """Parse ccusage weekly JSON into a ``WeeklyStats`` model.

        Takes the last entry in the weekly array and formats its total
        tokens using ``format_tokens``.
        """
        weeks = weekly_json.get("weekly", [])
        if not weeks:
            return WeeklyStats()

        last_week = weeks[-1]
        total = int(last_week.get("totalTokens", 0))

        return WeeklyStats(
            total_tokens=total,
            display=self.format_tokens(total),
        )

    # ------------------------------------------------------------------
    # Async collection
    # ------------------------------------------------------------------

    async def collect_ccusage_stats(self) -> tuple[BlockStats, WeeklyStats]:
        """Run ccusage CLI for blocks and weekly stats in a thread executor.

        Returns a tuple of ``(BlockStats, WeeklyStats)``.
        """
        loop = asyncio.get_running_loop()

        blocks_future = loop.run_in_executor(None, self._run_ccusage, "blocks")
        weekly_future = loop.run_in_executor(None, self._run_ccusage, "weekly")

        blocks_json, weekly_json = await asyncio.gather(blocks_future, weekly_future)

        block_stats = self.parse_block_stats(blocks_json) if blocks_json else BlockStats()
        weekly_stats = self.parse_weekly_stats(weekly_json) if weekly_json else WeeklyStats()

        return block_stats, weekly_stats

    def build_payload(
        self,
        session_contexts: dict[str, SessionContextStats],
    ) -> UsageStatsPayload:
        """Combine block + weekly + per-session context into a payload.

        ``session_contexts`` maps session ID to its context stats.
        Block and weekly stats come from the latest ``collect_ccusage_stats``
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
        """Background loop that collects ccusage stats every 60 seconds."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()

        logger.info("UsageCollector started (poll every %ds)", _POLL_INTERVAL)

        while not self._stop_event.is_set():
            try:
                block_stats, weekly_stats = await self.collect_ccusage_stats()
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
