"""Tests for the dispatcher conversation memory store."""

import sqlite3
import time

import pytest

from src.hub.conversation_store import ConversationStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "conversations.db")
    s = ConversationStore(db_path=db_path)
    s.init()
    return s


class TestAddAndGetHistory:
    def test_add_and_retrieve(self, store):
        store.add_message(user_id=123, role="user", content="Salut")
        store.add_message(user_id=123, role="assistant", content="Bonjour!")
        history = store.get_history(user_id=123, limit=10)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Salut"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "Bonjour!"

    def test_history_ordered_by_timestamp(self, store):
        store.add_message(user_id=123, role="user", content="First")
        store.add_message(user_id=123, role="assistant", content="Second")
        store.add_message(user_id=123, role="user", content="Third")
        history = store.get_history(user_id=123, limit=10)
        assert [h["content"] for h in history] == ["First", "Second", "Third"]

    def test_history_respects_limit(self, store):
        for i in range(20):
            store.add_message(user_id=123, role="user", content=f"msg-{i}")
        history = store.get_history(user_id=123, limit=5)
        assert len(history) == 5
        assert history[0]["content"] == "msg-15"

    def test_history_per_user(self, store):
        store.add_message(user_id=100, role="user", content="User 100")
        store.add_message(user_id=200, role="user", content="User 200")
        h100 = store.get_history(user_id=100, limit=10)
        h200 = store.get_history(user_id=200, limit=10)
        assert len(h100) == 1
        assert len(h200) == 1
        assert h100[0]["content"] == "User 100"


class TestCleanup:
    def test_cleanup_removes_old_messages(self, store):
        old_ts = time.time() - 3600 * 72  # 72 hours ago
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "INSERT INTO conversations (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (123, "user", "old", old_ts),
        )
        conn.commit()
        conn.close()
        store.add_message(user_id=123, role="user", content="recent")
        removed = store.cleanup(max_age_hours=48)
        assert removed == 1
        history = store.get_history(user_id=123, limit=10)
        assert len(history) == 1
        assert history[0]["content"] == "recent"


class TestSearch:
    def test_search_finds_matching_messages(self, store):
        store.add_message(user_id=123, role="user", content="deploy the backup script")
        store.add_message(user_id=123, role="assistant", content="Done, backup deployed")
        store.add_message(user_id=123, role="user", content="check disk space")
        results = store.search(user_id=123, query="backup", limit=5)
        assert len(results) == 2
        assert all("backup" in r["content"].lower() for r in results)

    def test_search_empty_returns_empty(self, store):
        results = store.search(user_id=123, query="nonexistent", limit=5)
        assert results == []


class TestBuildPromptContext:
    def test_builds_formatted_history(self, store):
        store.add_message(user_id=123, role="user", content="What services are running?")
        store.add_message(user_id=123, role="assistant", content="PostgreSQL, n8n, Neo4j are running.")
        context = store.build_prompt_context(user_id=123, limit=10, max_content_length=500)
        assert "User:" in context
        assert "Assistant:" in context
        assert "PostgreSQL" in context

    def test_truncates_long_content(self, store):
        long_msg = "x" * 1000
        store.add_message(user_id=123, role="user", content=long_msg)
        context = store.build_prompt_context(user_id=123, limit=10, max_content_length=100)
        lines = [l for l in context.split("\n") if "User:" in l]
        assert len(lines) == 1
        assert len(lines[0]) < 150

    def test_empty_history_returns_empty_string(self, store):
        context = store.build_prompt_context(user_id=123, limit=10, max_content_length=500)
        assert context == ""
