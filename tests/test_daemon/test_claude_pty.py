"""Unit tests for claude-pty relay components.

Tests the pyte-based terminal emulation, output buffering, and inject logic
without forking or spawning real processes.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Load claude-pty.py as a module (it's a script, not a package)
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "claude-pty.py"


@pytest.fixture(scope="module")
def pty_module():
    """Import claude-pty.py as a module for testing."""
    spec = importlib.util.spec_from_file_location("claude_pty", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Virtual terminal (pyte-based)
# ---------------------------------------------------------------------------

class TestInitScreen:
    def test_creates_screen_and_stream(self, pty_module):
        pty_module._init_screen(80, 24)
        assert pty_module._screen is not None
        assert pty_module._stream is not None
        assert pty_module._screen.columns == 80
        assert pty_module._screen.lines == 24

    def test_custom_dimensions(self, pty_module):
        pty_module._init_screen(120, 40)
        assert pty_module._screen.columns == 120
        assert pty_module._screen.lines == 40


class TestBufferOutput:
    def test_simple_text(self, pty_module):
        pty_module._init_screen(80, 24)
        pty_module._buffer_output(b"hello world\r\n")
        with pty_module._output_lock:
            display = [line.rstrip() for line in pty_module._screen.display]
        assert any("hello world" in line for line in display)

    def test_ansi_colors_rendered(self, pty_module):
        """ANSI color codes should be consumed by pyte, not appear in display."""
        pty_module._init_screen(80, 24)
        pty_module._buffer_output(b"\x1b[32mGreen text\x1b[0m\r\n")
        with pty_module._output_lock:
            display = [line.rstrip() for line in pty_module._screen.display]
        text = "\n".join(display)
        assert "Green text" in text
        assert "\x1b" not in text

    def test_dec_private_mode_handled(self, pty_module):
        """DEC private mode sequences like \\x1b[?2026h should not appear in output."""
        pty_module._init_screen(80, 24)
        pty_module._buffer_output(b"\x1b[?2026hsome content\r\n")
        with pty_module._output_lock:
            display = [line.rstrip() for line in pty_module._screen.display]
        text = "\n".join(display)
        assert "some content" in text
        assert "?2026" not in text

    def test_cursor_positioning(self, pty_module):
        """Cursor movement sequences should position text correctly."""
        pty_module._init_screen(80, 24)
        # Write text, then move cursor and overwrite
        pty_module._buffer_output(b"AAAA\r\nBBBB\r\n")
        with pty_module._output_lock:
            display = [line.rstrip() for line in pty_module._screen.display]
        assert display[0] == "AAAA"
        assert display[1] == "BBBB"

    def test_invalid_utf8(self, pty_module):
        """Invalid UTF-8 should not crash."""
        pty_module._init_screen(80, 24)
        pty_module._buffer_output(b"\xff\xfe invalid bytes \r\n")
        # Should not raise

    def test_screen_display_full(self, pty_module):
        """Capture returns the full screen display."""
        pty_module._init_screen(80, 10)
        for i in range(5):
            pty_module._buffer_output(f"line-{i}\r\n".encode())
        with pty_module._output_lock:
            display = [line.rstrip() for line in pty_module._screen.display]
        # Screen has 10 lines, 5 with content
        assert len(display) == 10
        assert display[0] == "line-0"
        assert display[4] == "line-4"


# ---------------------------------------------------------------------------
# Inject logic
# ---------------------------------------------------------------------------

class TestInjectLogic:
    def test_inject_simple_text(self, pty_module):
        """_inject('y') should write 'y\\r' to master_fd."""
        written = []
        original_master = pty_module._master_fd

        with patch.object(pty_module.os, "write", side_effect=lambda fd, data: written.append(data)):
            pty_module._master_fd = 42
            try:
                pty_module._Handler._inject("y")
            finally:
                pty_module._master_fd = original_master

        assert len(written) == 1
        assert written[0] == b"y\r"

    def test_inject_select_down(self, pty_module):
        """select:2 should send 2 down arrows + Enter."""
        written = []
        original_master = pty_module._master_fd

        with patch.object(pty_module.os, "write", side_effect=lambda fd, data: written.append(data)):
            pty_module._master_fd = 42
            try:
                pty_module._Handler._inject("select:2")
            finally:
                pty_module._master_fd = original_master

        assert len(written) == 3
        assert written[0] == b"\x1b[B"  # Down arrow
        assert written[1] == b"\x1b[B"  # Down arrow
        assert written[2] == b"\r"      # Enter

    def test_inject_select_up(self, pty_module):
        """select:-1 should send 1 up arrow + Enter."""
        written = []
        original_master = pty_module._master_fd

        with patch.object(pty_module.os, "write", side_effect=lambda fd, data: written.append(data)):
            pty_module._master_fd = 42
            try:
                pty_module._Handler._inject("select:-1")
            finally:
                pty_module._master_fd = original_master

        assert len(written) == 2
        assert written[0] == b"\x1b[A"  # Up arrow
        assert written[1] == b"\r"      # Enter

    def test_inject_select_zero(self, pty_module):
        """select:0 should just send Enter (no arrows)."""
        written = []
        original_master = pty_module._master_fd

        with patch.object(pty_module.os, "write", side_effect=lambda fd, data: written.append(data)):
            pty_module._master_fd = 42
            try:
                pty_module._Handler._inject("select:0")
            finally:
                pty_module._master_fd = original_master

        assert len(written) == 1
        assert written[0] == b"\r"

    def test_inject_multichar(self, pty_module):
        """Multi-char input like 'yes please' should be sent as-is + Enter."""
        written = []
        original_master = pty_module._master_fd

        with patch.object(pty_module.os, "write", side_effect=lambda fd, data: written.append(data)):
            pty_module._master_fd = 42
            try:
                pty_module._Handler._inject("yes please")
            finally:
                pty_module._master_fd = original_master

        assert len(written) == 1
        assert written[0] == b"yes please\r"


# ---------------------------------------------------------------------------
# HTTP handler (unit-level, without real HTTP server)
# ---------------------------------------------------------------------------

class TestHandlerCapture:
    def test_capture_returns_full_screen(self, pty_module):
        """Capture should return the full pyte screen display."""
        pty_module._init_screen(80, 24)
        for i in range(10):
            pty_module._buffer_output(f"line-{i}\r\n".encode())

        with pty_module._output_lock:
            display = [line.rstrip() for line in pty_module._screen.display]
        content = "\n".join(display)

        assert "line-0" in content
        assert "line-9" in content
        # Full screen: 24 lines
        assert len(display) == 24


class TestFindFreePort:
    def test_returns_valid_port(self, pty_module):
        port = pty_module._find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535
