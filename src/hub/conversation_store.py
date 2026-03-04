"""SQLite-backed conversation memory for the Telegram dispatcher."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class ConversationStore:
    """Stores dispatcher conversation history per Telegram user.

    Provides methods to add messages, retrieve recent history,
    search past messages, and build formatted prompt context.
    """

    def __init__(self, db_path: str = "data/conversations.db") -> None:
        self.db_path = db_path

    def init(self) -> None:
        """Create the database schema if it doesn't exist."""
        from pathlib import Path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                mission_id TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv_user_ts
            ON conversations(user_id, timestamp DESC)
        """)
        conn.commit()
        conn.close()
        logger.info("ConversationStore initialized at %s", self.db_path)

    def add_message(
        self, user_id: int, role: str, content: str, mission_id: str | None = None,
    ) -> None:
        """Store a conversation message."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO conversations (user_id, role, content, timestamp, mission_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, role, content, time.time(), mission_id),
        )
        conn.commit()
        conn.close()

    def get_history(self, user_id: int, limit: int = 10) -> list[dict]:
        """Return the last N messages for a user, oldest first."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in reversed(rows)]

    def search(self, user_id: int, query: str, limit: int = 5) -> list[dict]:
        """Search conversation history for messages containing query."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE user_id = ? AND content LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, f"%{query}%", limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def cleanup(self, max_age_hours: int = 48) -> int:
        """Remove messages older than max_age_hours. Returns count removed."""
        cutoff = time.time() - (max_age_hours * 3600)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("DELETE FROM conversations WHERE timestamp < ?", (cutoff,))
        removed = cursor.rowcount
        conn.commit()
        conn.close()
        if removed:
            logger.info("Cleaned up %d old conversation messages", removed)
        return removed

    def build_prompt_context(
        self, user_id: int, limit: int = 10, max_content_length: int = 500,
    ) -> str:
        """Build a formatted conversation history string for prompt injection."""
        history = self.get_history(user_id, limit)
        if not history:
            return ""
        lines: list[str] = []
        for msg in history:
            ts = datetime.fromtimestamp(msg["timestamp"]).strftime("%H:%M")
            role = "User" if msg["role"] == "user" else "Assistant"
            content = msg["content"]
            if len(content) > max_content_length:
                content = content[:max_content_length] + "..."
            lines.append(f"[{ts}] {role}: {content}")
        return "\n".join(lines)
