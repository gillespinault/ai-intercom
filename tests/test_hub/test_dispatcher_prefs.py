"""Tests for dispatcher preferences in AttentionStore."""

import tempfile
from pathlib import Path

from src.hub.attention_store import AttentionStore


def _make_store():
    """Create an AttentionStore with isolated temp directory."""
    td = tempfile.mkdtemp()
    return AttentionStore(prefs_path=str(Path(td) / "notification_prefs.json"))


def test_dispatcher_prefs_defaults():
    store = _make_store()
    prefs = store.get_dispatcher_prefs()
    assert prefs["conversation_active"] is True
    assert prefs["show_agent_exchanges"] is True
    assert prefs["voice_response"] is True
    assert prefs["auto_print_pos"] is False
    assert prefs["hear_agents"] is False


def test_dispatcher_prefs_update():
    store = _make_store()
    updated = store.update_dispatcher_prefs({"hear_agents": True})
    assert updated["hear_agents"] is True
    assert updated["conversation_active"] is True  # unchanged


def test_dispatcher_prefs_persist():
    with tempfile.TemporaryDirectory() as td:
        prefs_path = str(Path(td) / "notification_prefs.json")
        store = AttentionStore(prefs_path=prefs_path)
        store.update_dispatcher_prefs({"auto_print_pos": True})

        # Reload
        store2 = AttentionStore(prefs_path=prefs_path)
        prefs = store2.get_dispatcher_prefs()
        assert prefs["auto_print_pos"] is True


def test_dispatcher_prefs_ignores_unknown_keys():
    store = _make_store()
    updated = store.update_dispatcher_prefs({"unknown_key": True})
    assert "unknown_key" not in updated
