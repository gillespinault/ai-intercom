"""Tests for active conversation tracking."""

import time

from src.hub.active_conversations import ActiveConversation, ActiveConversationManager


def test_start_conversation():
    mgr = ActiveConversationManager()
    mgr.start(user_id=123, mission_id="m1", daemon_url="http://d:7701")
    active = mgr.get_active(123)
    assert active is not None
    assert active.mission_id == "m1"
    assert active.status == "active"


def test_get_active_none():
    mgr = ActiveConversationManager()
    assert mgr.get_active(999) is None


def test_touch_updates_activity():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    t1 = mgr.get_active(123).last_activity
    time.sleep(0.01)
    mgr.touch(123)
    t2 = mgr.get_active(123).last_activity
    assert t2 > t1


def test_close_conversation():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr.close(123)
    assert mgr.get_active(123) is None


def test_start_replaces_existing():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr.start(123, "m2", "http://d:7701")
    assert mgr.get_active(123).mission_id == "m2"


def test_cleanup_stale():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr._active[123].last_activity = time.time() - 700
    mgr.cleanup_stale(ttl=600)
    assert mgr.get_active(123) is None


def test_cleanup_keeps_fresh():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr.cleanup_stale(ttl=600)
    assert mgr.get_active(123) is not None


def test_is_injectable_recent():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    assert mgr.is_injectable(123) is True


def test_is_injectable_stale():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr._active[123].started_at = time.time() - 700
    mgr._active[123].last_activity = time.time() - 700
    assert mgr.is_injectable(123) is False
