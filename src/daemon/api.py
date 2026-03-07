"""Daemon HTTP API: receives messages from the hub and exposes health/status."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response

from src.shared.auth import normalize_headers, verify_request

logger = logging.getLogger(__name__)


def _get_version() -> str:
    try:
        from importlib.metadata import version
        return version("ai-intercom")
    except Exception:
        return "unknown"


def create_app(machine_id: str, token: str) -> FastAPI:
    """Create the daemon FastAPI application."""
    app = FastAPI(title=f"AI-Intercom Daemon ({machine_id})")
    app.state.machine_id = machine_id
    app.state.token = token
    app.state.active_missions: dict[str, dict] = {}
    app.state.launcher = None  # Set by daemon main
    app.state.active_sessions: dict[str, dict] = {}
    app.state.hub_client = None

    @app.get("/health")
    async def health():
        return {
            "machine_id": machine_id,
            "status": "ok",
            "active_missions": len(app.state.active_missions),
        }

    @app.get("/api/discover")
    async def discover():
        return {
            "hub": False,
            "machine_id": machine_id,
            "version": _get_version(),
        }

    @app.get("/api/status")
    async def status():
        return {
            "machine_id": machine_id,
            "active_missions": list(app.state.active_missions.keys()),
        }

    @app.post("/api/message")
    async def receive_message(request: Request):
        body = await request.body()
        headers = normalize_headers(dict(request.headers))
        if not verify_request(body, headers, token):
            return Response(status_code=401, content="Unauthorized")

        data = json.loads(body)
        mission_id = data.get("mission_id", "unknown")
        msg_type = data.get("type", "send")
        app.state.active_missions[mission_id] = data

        # Launch agent for actionable message types (non-blocking)
        if msg_type in ("ask", "start_agent") and app.state.launcher:
            to_agent = data.get("to_agent", "")
            project = to_agent.split("/", 1)[1] if "/" in to_agent else to_agent
            payload = data.get("payload", {})
            mission = payload.get("mission") or payload.get("message", "")
            agent_command = payload.get("agent_command")

            # Resolve project path from registry or use working directory
            project_path = "."
            if hasattr(app.state, "project_paths"):
                project_path = app.state.project_paths.get(project, ".")

            try:
                await app.state.launcher.launch_background(
                    mission=mission,
                    context_messages=[],
                    mission_id=mission_id,
                    project_path=project_path,
                    agent_command=agent_command,
                )
                return {"status": "launched", "mission_id": mission_id}
            except Exception as e:
                logger.error("Failed to launch agent: %s", e)
                return {"status": "launch_failed", "mission_id": mission_id, "error": str(e)}

        return {"status": "received", "mission_id": mission_id}

    @app.get("/api/missions/{mission_id}")
    async def mission_status(mission_id: str, feedback_since: int = 0):
        """Get the status of a mission running on this daemon."""
        if not app.state.launcher:
            return Response(status_code=404, content="No launcher configured")
        result = app.state.launcher.get_status(mission_id)
        if not result:
            return Response(status_code=404, content="Mission not found")
        return {
            "mission_id": mission_id,
            "status": result.status,
            "output": result.output,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "feedback": [
                {"timestamp": f.timestamp, "kind": f.kind, "summary": f.summary}
                for f in result.feedback[feedback_since:]
            ],
            "feedback_total": len(result.feedback),
            "turn_count": result.turn_count,
        }

    # --- Session management endpoints ---

    @app.post("/api/session/register")
    async def session_register(request: Request):
        """Register an active Claude Code session."""
        data = await request.json()
        session_id = data["session_id"]
        inbox_path = data["inbox_path"]

        # Create inbox directory if needed
        inbox_dir = Path(inbox_path).parent
        inbox_dir.mkdir(parents=True, exist_ok=True)

        app.state.active_sessions[session_id] = {
            "session_id": session_id,
            "project": data["project"],
            "pid": data["pid"],
            "inbox_path": inbox_path,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Session registered: %s (project=%s, pid=%s)", session_id, data["project"], data["pid"])
        return {"status": "registered", "session_id": session_id}

    @app.post("/api/session/unregister")
    async def session_unregister(request: Request):
        """Unregister a session and clean up its inbox file."""
        data = await request.json()
        session_id = data["session_id"]

        session = app.state.active_sessions.pop(session_id, None)
        if session:
            inbox_path = Path(session["inbox_path"])
            if inbox_path.exists():
                inbox_path.unlink()
            logger.info("Session unregistered: %s", session_id)

        return {"status": "unregistered"}

    @app.get("/api/sessions")
    async def list_sessions():
        """List all active sessions."""
        return {"sessions": list(app.state.active_sessions.values())}

    @app.post("/api/session/deliver")
    async def session_deliver(request: Request):
        """Deliver a chat message to a session's inbox."""
        data = await request.json()
        session_id = data.get("session_id")
        project = data.get("project")

        # Find session by session_id or project
        session = None
        if session_id and session_id in app.state.active_sessions:
            session = app.state.active_sessions[session_id]
        elif project:
            for s in app.state.active_sessions.values():
                if s["project"] == project:
                    session = s
                    break

        if not session:
            return Response(
                status_code=404,
                content=json.dumps({"status": "no_active_session"}),
                media_type="application/json",
            )

        # Verify PID is alive
        pid = session["pid"]
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # PID is dead — clean up session
            dead_id = session["session_id"]
            app.state.active_sessions.pop(dead_id, None)
            logger.warning("Session %s has dead PID %d, removing", dead_id, pid)
            return Response(
                status_code=404,
                content=json.dumps({"status": "no_active_session"}),
                media_type="application/json",
            )
        except PermissionError:
            # PID exists but owned by different user — treat as alive
            pass

        # Append JSONL line to inbox file (with file lock to prevent TOCTOU)
        import fcntl

        inbox_path = Path(session["inbox_path"])
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "thread_id": data.get("thread_id", ""),
            "from_agent": data.get("from_agent", ""),
            "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "message": data.get("message", ""),
            "read": False,
        }
        with open(inbox_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(entry) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        logger.info("Delivered message to session %s (thread=%s)", session["session_id"], entry["thread_id"])
        return {"status": "delivered"}

    @app.get("/api/session/{session_id}/status")
    async def session_status(session_id: str):
        """Get session info and inbox pending count."""
        session = app.state.active_sessions.get(session_id)
        if not session:
            return Response(
                status_code=404,
                content=json.dumps({"status": "not_found"}),
                media_type="application/json",
            )

        # Count unread lines in inbox
        inbox_pending = 0
        inbox_path = Path(session["inbox_path"])
        if inbox_path.exists():
            with open(inbox_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = json.loads(line)
                            if not entry.get("read", False):
                                inbox_pending += 1
                        except json.JSONDecodeError:
                            pass

        return {
            "session_id": session["session_id"],
            "project": session["project"],
            "pid": session["pid"],
            "inbox_path": session["inbox_path"],
            "registered_at": session["registered_at"],
            "inbox_pending": inbox_pending,
        }

    # --- Self-upgrade endpoint ---

    @app.post("/api/upgrade")
    async def self_upgrade(request: Request):
        """Trigger self-upgrade on this daemon."""
        data = await request.json()
        target_version = data.get("version", "")

        from src.daemon.upgrade import run_self_upgrade
        result = run_self_upgrade(target_version=target_version)
        return result

    # --- Attention Hub endpoints ---

    @app.get("/api/attention/sessions")
    async def attention_sessions():
        """List attention-tracked sessions on this machine."""
        monitor = getattr(app.state, "attention_monitor", None)
        if not monitor:
            return {"sessions": []}
        return {"sessions": [s.model_dump() for s in monitor.get_sessions()]}

    @app.post("/api/attention/respond")
    async def attention_respond(request: Request):
        """Inject a response into a Claude Code session.

        Supports two backends:
        - PTY relay (claude-pty): via ``pty_port`` field — preferred, zero tmux dependency
        - tmux send-keys: via ``tmux_session`` field — legacy fallback
        """
        data = await request.json()
        pty_port = data.get("pty_port")
        tmux_session = data.get("tmux_session", "")
        keys = data.get("keys", "")

        if not keys:
            return Response(status_code=400, content="keys required")
        if not pty_port and not tmux_session:
            return Response(status_code=400, content="pty_port or tmux_session required")

        monitor = getattr(app.state, "attention_monitor", None)
        if not monitor:
            return Response(status_code=503, content="Attention monitor not running")

        # Prefer PTY relay over tmux
        if pty_port:
            logger.info("Injecting response via PTY port=%d keys=%r", pty_port, keys)
            success = monitor._inject_response_pty(pty_port, keys)
        else:
            logger.info("Injecting response via tmux=%s keys=%r", tmux_session, keys)
            success = monitor._inject_response(tmux_session, keys)

        if not success:
            logger.warning("Response injection failed (pty_port=%s, tmux=%s)", pty_port, tmux_session)
        return {"status": "sent" if success else "failed"}

    @app.get("/api/attention/terminal/{tmux_session:path}")
    async def attention_terminal(tmux_session: str):
        """Capture terminal content from a tmux session."""
        monitor = getattr(app.state, "attention_monitor", None)
        if not monitor:
            return Response(status_code=503, content="Attention monitor not running")

        content = monitor._capture_terminal(tmux_session)
        if content is None:
            return Response(status_code=404, content="tmux session not found")
        return {"tmux_session": tmux_session, "content": content}

    @app.get("/api/attention/terminal-pty/{pty_port}")
    async def attention_terminal_pty(pty_port: int):
        """Capture terminal content via claude-pty HTTP relay."""
        monitor = getattr(app.state, "attention_monitor", None)
        if not monitor:
            return Response(status_code=503, content="Attention monitor not running")

        content = monitor._capture_terminal_pty(pty_port)
        if content is None:
            return Response(status_code=404, content="PTY relay not reachable")
        return {"pty_port": pty_port, "content": content}

    # --- Permission hook endpoints ---

    @app.post("/hook/permission")
    async def hook_permission(request: Request):
        """Receive PermissionRequest hook from Claude Code.

        Non-blocking: forwards to hub for PWA display, returns {} immediately
        so Claude Code shows the terminal prompt without delay.
        """
        from src.shared.models import PermissionRequest

        data = await request.json()
        sid = data.get("session_id", "")

        # Resolve project: active_sessions first, then attention_monitor heartbeats
        session_info = app.state.active_sessions.get(sid, {})
        project = session_info.get("project", "")
        if not project:
            monitor = getattr(app.state, "attention_monitor", None)
            if monitor:
                for s in monitor.get_sessions():
                    if s.session_id == sid:
                        project = s.project
                        break

        req = PermissionRequest(
            session_id=sid,
            tool_name=data.get("tool_name", ""),
            tool_input=data.get("tool_input", {}),
            permission_suggestions=data.get("permission_suggestions", []),
            machine=machine_id,
            project=project,
        )

        hub = getattr(app.state, "hub_client", None)
        if not hub:
            logger.warning("Permission hook: no hub_client, falling back to terminal")
            return {}

        # Non-blocking: forward to hub for PWA display, then return {}
        # immediately so Claude Code shows the terminal prompt without delay.
        # The PWA can still respond via PTY/tmux injection.
        try:
            await hub.push_permission_request(req)
        except Exception as e:
            logger.error("Failed to forward permission to hub: %s", e)

        return {}

    return app
