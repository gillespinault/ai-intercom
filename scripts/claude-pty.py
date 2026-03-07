#!/usr/bin/env python3
"""claude-pty — Transparent PTY relay for Claude Code.

Replaces claude-tmux with zero terminal UX impact. Provides:
- HTTP API for terminal capture (replaces tmux capture-pane)
- HTTP API for keystroke injection (replaces tmux send-keys)
- Full transparency: user sees no difference from running claude directly

Usage:
    claude-pty [claude args...]

The HTTP API listens on a random localhost port. The port is written to
/tmp/cc-sessions/pty-{PID}.port for discovery by the AttentionMonitor.

Architecture:
    User terminal <---> claude-pty (I/O relay) <---> Claude Code (child PTY)
                               |
                          HTTP API (:random_port)
                               |
                    AttentionMonitor / Daemon
"""

import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import threading
import tty
from http.server import HTTPServer, BaseHTTPRequestHandler

import pyte

# ---------------------------------------------------------------------------
# Virtual terminal screen (pyte-based)
# ---------------------------------------------------------------------------

_output_lock = threading.Lock()
_screen: pyte.Screen | None = None
_stream: pyte.Stream | None = None
_master_fd: int = -1


def _init_screen(cols: int, rows: int) -> None:
    """Initialize the virtual terminal with the given dimensions."""
    global _screen, _stream
    _screen = pyte.Screen(cols, rows)
    _screen.set_mode(pyte.modes.LNM)  # line feed = newline
    _stream = pyte.Stream(_screen)


def _buffer_output(data: bytes) -> None:
    """Feed raw child output through the virtual terminal."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return
    with _output_lock:
        if _stream is not None:
            _stream.feed(text)


# ---------------------------------------------------------------------------
# HTTP API server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler for capture and inject."""

    def do_GET(self):
        if self.path == "/capture":
            with _output_lock:
                if _screen is not None:
                    display = [line.rstrip() for line in _screen.display]
                else:
                    display = []
            content = "\n".join(display)
            self._json(200, {"content": content, "lines": len(display)})
        elif self.path == "/health":
            self._json(200, {"status": "ok", "pid": os.getpid()})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path == "/inject":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            keys = body.get("keys", "")
            if not keys:
                self._json(400, {"error": "keys required"})
                return
            try:
                self._inject(keys)
                self._json(200, {"status": "sent"})
            except OSError as e:
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "not_found"})

    @staticmethod
    def _inject(keys: str) -> None:
        """Write keystrokes to the PTY master fd."""
        if keys.startswith("select:"):
            # SelectInput: arrow navigation then Enter
            try:
                offset = int(keys.split(":", 1)[1])
            except (ValueError, IndexError):
                offset = 0
            for _ in range(abs(offset)):
                arrow = b"\x1b[B" if offset > 0 else b"\x1b[A"
                os.write(_master_fd, arrow)
            os.write(_master_fd, b"\r")
        else:
            # Direct keystroke + Enter
            os.write(_master_fd, keys.encode("utf-8") + b"\r")

    def _json(self, status: int, body: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass  # Suppress access logs


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Main relay
# ---------------------------------------------------------------------------


def main() -> None:
    global _master_fd

    # If not in a terminal, just exec claude directly
    if not os.isatty(sys.stdin.fileno()):
        os.execvp("claude", ["claude"] + sys.argv[1:])

    # Find free port for HTTP API
    port = _find_free_port()

    # Create PTY pair
    master_fd, slave_fd = pty.openpty()
    _master_fd = master_fd

    # Copy terminal attributes and window size to slave
    stdin_fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(stdin_fd)
    termios.tcsetattr(slave_fd, termios.TCSANOW, old_attrs)

    winsize = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\x00" * 8)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    # Start HTTP server in background
    server = HTTPServer(("127.0.0.1", port), _Handler)
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()

    # Fork child process
    child_pid = os.fork()

    if child_pid == 0:
        # Child: become Claude Code connected to slave PTY
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvp("claude", ["claude"] + sys.argv[1:])
        sys.exit(127)

    # Parent: relay I/O between user terminal and child PTY
    os.close(slave_fd)

    # Write port file for discovery by attention monitor
    sessions_dir = "/tmp/cc-sessions"
    os.makedirs(sessions_dir, exist_ok=True)
    port_file = os.path.join(sessions_dir, f"pty-{child_pid}.port")
    with open(port_file, "w") as f:
        f.write(str(port))

    # Set stdin to raw mode
    try:
        tty.setraw(stdin_fd)
    except termios.error:
        pass

    # Initialize virtual terminal with current dimensions
    ws_rows, ws_cols = struct.unpack("HH", winsize[:4])
    _init_screen(ws_cols or 80, ws_rows or 24)

    # Forward SIGWINCH (terminal resize) to child + resize virtual screen
    def _on_winch(sig, frame):
        try:
            ws = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\x00" * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
            os.kill(child_pid, signal.SIGWINCH)
            r, c = struct.unpack("HH", ws[:4])
            with _output_lock:
                if _screen is not None:
                    _screen.resize(r or 24, c or 80)
        except (OSError, ValueError):
            pass

    signal.signal(signal.SIGWINCH, _on_winch)

    # Forward SIGINT to child (don't kill relay)
    def _on_int(sig, frame):
        try:
            os.kill(child_pid, signal.SIGINT)
        except OSError:
            pass

    signal.signal(signal.SIGINT, _on_int)

    exit_code = 0
    try:
        fds = [stdin_fd, master_fd]
        while True:
            try:
                readable, _, _ = select.select(fds, [], [], 0.25)
            except (ValueError, OSError):
                break

            if not readable:
                # Check child liveness on timeout
                pid, status = os.waitpid(child_pid, os.WNOHANG)
                if pid != 0:
                    exit_code = (
                        os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
                    )
                    break
                continue

            for fd in readable:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    data = b""

                if not data:
                    # EOF — child exited or terminal closed
                    pid, status = os.waitpid(child_pid, os.WNOHANG)
                    if pid != 0:
                        exit_code = (
                            os.WEXITSTATUS(status)
                            if os.WIFEXITED(status)
                            else 1
                        )
                    break

                if fd == stdin_fd:
                    # User typing → forward to child
                    os.write(master_fd, data)
                else:
                    # Child output → forward to user + buffer
                    os.write(sys.stdout.fileno(), data)
                    _buffer_output(data)
            else:
                continue
            break  # Inner break propagates

    finally:
        # Restore terminal
        try:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        except (termios.error, ValueError):
            pass

        # Shutdown HTTP server
        server.shutdown()

        # Clean up port file
        try:
            os.unlink(port_file)
        except OSError:
            pass

        # Close master PTY
        try:
            os.close(master_fd)
        except OSError:
            pass

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
