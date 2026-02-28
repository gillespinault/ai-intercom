"""Hub HTTP API: registration, heartbeat, join/approve, routing, and discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from src.hub.registry import Registry
from src.shared.auth import normalize_headers, verify_request
from src.shared.config import IntercomConfig
from src.shared.models import Message

logger = logging.getLogger(__name__)


def _format_elapsed(seconds: int) -> str:
    """Format seconds into a human-readable string."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if secs:
        return f"{minutes}m{secs:02d}s"
    return f"{minutes}m"


def create_hub_api(
    registry: Registry,
    router: Any,
    config: IntercomConfig,
    telegram_bot: Any = None,
    launcher: Any = None,
    project_paths: dict[str, str] | None = None,
) -> FastAPI:
    """Create the Hub FastAPI application."""
    app = FastAPI(title="AI-Intercom Hub")
    app.state.registry = registry
    app.state.router = router
    app.state.telegram_bot = telegram_bot
    app.state.launcher = launcher
    app.state.project_paths = project_paths or {}
    app.state.pending_joins: dict[str, dict] = {}
    app.state.mission_store: dict[str, list[dict]] = {}
    app.state.thread_store: dict[str, dict] = {}
    app.state.machine_sessions: dict[str, list] = {}

    # --- Auth helper ---

    async def _verify_machine(request: Request, body: bytes, machine_id: str) -> bool:
        """Verify HMAC authentication for a known machine."""
        token = await registry.get_machine_token(machine_id)
        if not token:
            return True  # Unknown machine, no token to check
        headers = normalize_headers(dict(request.headers))
        return verify_request(body, headers, token)

    # --- Discovery ---

    @app.get("/api/discover")
    async def discover():
        return {"hub": True, "name": "AI-Intercom Hub", "version": "0.1.0"}

    # --- Join flow ---

    @app.post("/api/join")
    async def join(request: Request):
        data = await request.json()
        machine_id = data.get("machine_id", "")
        display_name = data.get("display_name", machine_id)
        tailscale_ip = data.get("tailscale_ip", "")
        request_ip = request.client.host if request.client else "unknown"
        ip = tailscale_ip or request_ip

        app.state.pending_joins[machine_id] = {
            "machine_id": machine_id,
            "display_name": display_name,
            "projects": data.get("projects", []),
            "ip": ip,
            "status": "pending_approval",
        }

        # Notify via Telegram with approve/deny buttons
        if telegram_bot:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "\u2705 Approve", callback_data=f"join:{machine_id}:approve"
                    ),
                    InlineKeyboardButton(
                        "\u274c Deny", callback_data=f"join:{machine_id}:deny"
                    ),
                ]
            ])
            await telegram_bot.app.bot.send_message(
                chat_id=telegram_bot.supergroup_id,
                text=(
                    f"\U0001f6aa *Join Request*\n\n"
                    f"*Machine:* `{machine_id}`\n"
                    f"*Name:* {display_name}\n"
                    f"*IP:* `{ip}`\n\n"
                    f"Approve this machine to join the network?"
                ),
                reply_markup=keyboard,
                parse_mode="Markdown",
            )

        return {"status": "pending_approval", "machine_id": machine_id}

    @app.get("/api/join/status/{machine_id}")
    async def join_status(machine_id: str):
        """Check join request status (used by install.sh polling)."""
        pending = app.state.pending_joins.get(machine_id)
        if pending:
            return {"status": pending.get("status", "pending_approval")}
        # Check if already registered
        machine = await registry.get_machine(machine_id)
        if machine:
            return {"status": "approved", "token": machine["token"]}
        return Response(status_code=404, content="No join request found")

    @app.post("/api/join/approve/{machine_id}")
    async def approve_join(machine_id: str):
        pending = app.state.pending_joins.pop(machine_id, None)
        if not pending:
            return Response(status_code=404, content="No pending join")

        token = f"ict_{machine_id}_{secrets.token_hex(16)}"
        await registry.register_machine(
            machine_id=machine_id,
            display_name=pending["display_name"],
            tailscale_ip=pending.get("ip", ""),
            daemon_url=f"http://{pending.get('ip', 'unknown')}:7700",
            token=token,
        )
        return {"status": "approved", "token": token}

    # --- Registration ---

    @app.post("/api/register")
    async def register(request: Request):
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")

        if not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        # Prefer Tailscale IP from body (daemon-detected), fall back to request IP
        display_name = data.get("display_name", machine_id)
        tailscale_ip = data.get("tailscale_ip", "")
        request_ip = request.client.host if request.client else ""
        ip = tailscale_ip or request_ip

        # Use daemon_url from body if provided, otherwise construct from IP
        daemon_url = data.get("daemon_url") or f"http://{ip}:7700"

        existing = await registry.get_machine(machine_id)
        if not existing:
            await registry.register_machine(
                machine_id=machine_id,
                display_name=display_name,
                tailscale_ip=ip,
                daemon_url=daemon_url,
                token="",
            )
        elif tailscale_ip:
            # Update IP if daemon provided a Tailscale IP
            await registry.register_machine(
                machine_id=machine_id,
                display_name=display_name,
                tailscale_ip=ip,
                daemon_url=daemon_url,
                token=existing.get("token", ""),
            )

        for project in data.get("projects", []):
            await registry.register_project(
                machine_id=machine_id,
                project_id=project.get("id", ""),
                description=project.get("description", ""),
                capabilities=project.get("capabilities", []),
                path=project.get("path", ""),
                agent_command=project.get("agent_command", "claude"),
            )

        return {"status": "registered", "machine_id": machine_id}

    @app.post("/api/register/update")
    async def register_update(request: Request):
        """Update registration for a specific machine/project."""
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")

        if not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        action = data.get("action", "update")
        project_id = data.get("project_id", "")

        if action == "update" and data.get("machine"):
            machine_data = data["machine"]
            await registry.register_machine(
                machine_id=machine_id,
                display_name=machine_data.get("display_name", machine_id),
                tailscale_ip=machine_data.get("tailscale_ip", ""),
                daemon_url=machine_data.get("daemon_url", ""),
                token=machine_data.get("token", ""),
            )

        if action == "update" and data.get("project"):
            proj = data["project"]
            await registry.register_project(
                machine_id=machine_id,
                project_id=project_id or proj.get("id", ""),
                description=proj.get("description", ""),
                capabilities=proj.get("capabilities", []),
                path=proj.get("path", ""),
                agent_command=proj.get("agent_command", "claude"),
            )
        elif action == "remove" and project_id:
            await registry.remove_project(machine_id, project_id)

        return {"status": "updated", "machine_id": machine_id}

    # --- Heartbeat ---

    @app.post("/api/heartbeat")
    async def heartbeat(request: Request):
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")

        if not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        await registry.update_heartbeat(
            machine_id, active_agents=data.get("active_agents", [])
        )

        # Store active sessions from daemon
        active_sessions = data.get("active_sessions", [])
        app.state.machine_sessions[machine_id] = active_sessions

        # Update IP/daemon_url if provided (keeps registry in sync)
        tailscale_ip = data.get("tailscale_ip", "")
        daemon_url = data.get("daemon_url", "")
        if tailscale_ip and daemon_url:
            existing = await registry.get_machine(machine_id)
            if existing and existing.get("tailscale_ip") != tailscale_ip:
                logger.info("Machine %s IP changed: %s -> %s", machine_id, existing.get("tailscale_ip"), tailscale_ip)
                await registry.register_machine(
                    machine_id=machine_id,
                    display_name=existing.get("display_name", machine_id),
                    tailscale_ip=tailscale_ip,
                    daemon_url=daemon_url,
                    token=existing.get("token", ""),
                )

        return {"status": "ok"}

    # --- Message routing ---

    @app.post("/api/route")
    async def route_message(request: Request):
        """Route a message between agents via the hub router."""
        body = await request.body()
        data = json.loads(body)

        from_agent = data.get("from_agent", "")
        machine_id = from_agent.split("/")[0] if "/" in from_agent else ""
        if machine_id and not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        mission_id = data.get("mission_id") or str(uuid.uuid4())
        msg_type = data.get("type", "send")
        to_agent_raw = data.get("to_agent", "")

        # Handle chat messages: deliver to active session, don't launch agent
        if msg_type == "chat":
            to_agent = to_agent_raw
            thread_id = data.get("payload", {}).get("thread_id", "")

            # Resolve recipient from thread_store when to_agent is empty (reply)
            if not to_agent and thread_id:
                thread_info = app.state.thread_store.get(thread_id)
                if thread_info:
                    participants = thread_info.get("participants", [])
                    to_agent = next(
                        (p for p in participants if p != from_agent), ""
                    )
                if not to_agent:
                    return {"status": "error", "error": f"Cannot resolve recipient for thread {thread_id}"}

            # Store in mission history
            if mission_id not in app.state.mission_store:
                app.state.mission_store[mission_id] = []
            app.state.mission_store[mission_id].append({
                "from_agent": from_agent,
                "to_agent": to_agent,
                "type": "chat",
                "payload": data.get("payload", {}),
                "mission_id": mission_id,
            })

            target_machine = to_agent.split("/")[0] if "/" in to_agent else to_agent
            target_project = to_agent.split("/", 1)[1] if "/" in to_agent else ""
            machine = await registry.get_machine(target_machine)

            # Store thread mapping for replies
            if thread_id:
                if thread_id not in app.state.thread_store:
                    app.state.thread_store[thread_id] = {
                        "participants": [from_agent, to_agent],
                    }

            if not machine:
                return {"status": "error", "error": f"Machine {target_machine} not found"}

            chat_message = data.get("payload", {}).get("message", "")

            # Post to Telegram for human visibility
            if telegram_bot:
                is_reply = not data.get("to_agent", "")
                emoji = "\u21a9\ufe0f Reply" if is_reply else "\U0001f4e8 Chat"
                tg_text = (
                    f"{emoji} [{thread_id}]\n"
                    f"{from_agent} \u2192 {to_agent}\n"
                    f"\"{chat_message}\""
                )
                try:
                    await telegram_bot.app.bot.send_message(
                        chat_id=telegram_bot.supergroup_id,
                        text=tg_text,
                    )
                except Exception as e:
                    logger.warning("Failed to send chat to Telegram: %s", e)

            # Try to deliver to active session on daemon
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{machine['daemon_url']}/api/session/deliver",
                        json={
                            "project": target_project,
                            "thread_id": thread_id,
                            "from_agent": from_agent,
                            "message": chat_message,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    if resp.status_code == 404:
                        if telegram_bot:
                            try:
                                await telegram_bot.app.bot.send_message(
                                    chat_id=telegram_bot.supergroup_id,
                                    text=f"\u26a0\ufe0f Session: pas de session active pour `{to_agent}`",
                                    parse_mode="Markdown",
                                )
                            except Exception:
                                pass
                        return {"status": "no_active_session", "thread_id": thread_id}

                    result = resp.json()
                    return {"status": "delivered", "thread_id": thread_id, "mission_id": mission_id}
            except Exception as e:
                logger.error("Chat delivery failed: %s", e)
                return {"status": "error", "error": str(e), "thread_id": thread_id}

        # Build Message for non-chat types (ask, send, start_agent, etc.)
        msg = Message(
            id=str(uuid.uuid4()),
            from_agent=from_agent,
            to_agent=to_agent_raw,
            type=msg_type,
            payload=data.get("payload", {}),
            mission_id=mission_id,
        )

        # Store in mission history
        if mission_id not in app.state.mission_store:
            app.state.mission_store[mission_id] = []
        app.state.mission_store[mission_id].append(msg.model_dump())

        result = await app.state.router.route(msg)

        # Track launched missions in background for Telegram feedback
        resp_mission_id = result.get("mission_id", mission_id)
        if result.get("status") == "launched" and telegram_bot:
            to_agent = data.get("to_agent", "")
            target_machine = to_agent.split("/")[0] if "/" in to_agent else to_agent
            machine = await registry.get_machine(target_machine)
            if machine:
                asyncio.create_task(
                    _track_mission_for_telegram(
                        telegram_bot=telegram_bot,
                        registry=registry,
                        mission_id=resp_mission_id,
                        daemon_url=machine["daemon_url"],
                        target=to_agent,
                        from_agent=from_agent,
                    )
                )

        return result

    async def _track_mission_for_telegram(
        telegram_bot,
        registry,
        mission_id: str,
        daemon_url: str,
        target: str,
        from_agent: str,
    ) -> None:
        """Background task: poll daemon for mission result, post to Telegram + mission_store."""
        t0 = time.monotonic()
        poll_timeout = 600  # 10 minutes max for inter-agent missions
        poll_interval = 10
        fallback_interval = 60  # Generic progress if no feedback for 60s
        elapsed = 0
        feedback_cursor = 0
        last_feedback_time = time.monotonic()
        last_posted_summary = ""

        # Post initial tracking message
        await telegram_bot.post_text_to_mission(
            mission_id,
            f"\U0001f680 _Agent lance sur_ `{target}`",
        )

        consecutive_not_found = 0
        max_not_found = 6  # Give up after ~60s of 404s

        while elapsed < poll_timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{daemon_url}/api/missions/{mission_id}",
                        params={"feedback_since": feedback_cursor},
                    )

                    # Handle 404: mission not found on daemon
                    if resp.status_code == 404:
                        consecutive_not_found += 1
                        if consecutive_not_found >= max_not_found:
                            logger.warning(
                                "Mission %s: %d consecutive 404s from %s, giving up",
                                mission_id, consecutive_not_found, daemon_url,
                            )
                            await telegram_bot.post_text_to_mission(
                                mission_id,
                                f"\u26a0\ufe0f _Mission introuvable sur_ `{target}` "
                                f"_(daemon ne connait pas cette mission)_",
                            )
                            return
                        continue

                    # Got a valid response, reset 404 counter
                    consecutive_not_found = 0

                    try:
                        status_data = resp.json()
                    except Exception:
                        logger.warning(
                            "Mission %s: invalid JSON from daemon (HTTP %d)",
                            mission_id, resp.status_code,
                        )
                        continue

                    # Process new feedback items
                    new_feedback = status_data.get("feedback", [])
                    turn_count = status_data.get("turn_count", 0)
                    if new_feedback:
                        feedback_cursor = status_data.get("feedback_total", feedback_cursor)
                        last_feedback_time = time.monotonic()
                        # Deduplicate consecutive identical summaries
                        unique = []
                        for fb in new_feedback:
                            s = fb.get("summary", "")
                            if s != last_posted_summary:
                                unique.append(s)
                                last_posted_summary = s
                        if unique:
                            # Post last 5 activities + elapsed + turn count
                            elapsed_str = _format_elapsed(elapsed)
                            activities = "\n".join(unique[-5:])
                            await telegram_bot.post_text_to_mission(
                                mission_id,
                                f"{activities}\n_({elapsed_str} \u2022 tour {turn_count})_",
                            )
                    elif time.monotonic() - last_feedback_time >= fallback_interval:
                        # Fallback: no feedback for a while
                        last_feedback_time = time.monotonic()
                        elapsed_str = _format_elapsed(elapsed)
                        await telegram_bot.post_text_to_mission(
                            mission_id,
                            f"\u2699\ufe0f _Agent en cours..._ ({elapsed_str})",
                        )

                    if status_data.get("status") in ("completed", "failed"):
                        total = _format_elapsed(int(time.monotonic() - t0))
                        status = status_data["status"]
                        output = status_data.get("output", "")

                        # Parse JSON output if present
                        try:
                            parsed = json.loads(output)
                            output = parsed.get("result", output)
                        except (json.JSONDecodeError, TypeError):
                            pass

                        # Store result in mission_store for intercom_history
                        if mission_id in app.state.mission_store:
                            app.state.mission_store[mission_id].append({
                                "from_agent": target,
                                "to_agent": from_agent,
                                "type": "result",
                                "payload": {
                                    "status": status,
                                    "message": output[:10000] if output else "",
                                },
                                "mission_id": mission_id,
                            })

                        if status == "completed":
                            header = f"\u2705 *Termine* ({total})"
                        else:
                            header = f"\u274c *Echec* ({total})"

                        # Truncate if too long for Telegram
                        tg_output = output
                        if len(tg_output) > 3500:
                            tg_output = tg_output[:3500] + "\n\n_... (tronque)_"

                        await telegram_bot.post_text_to_mission(
                            mission_id,
                            f"{header}\n\n{tg_output}" if tg_output else header,
                        )
                        return
            except Exception as e:
                logger.debug("Mission %s: poll error: %s", mission_id, e)

        # Timeout
        total = _format_elapsed(int(time.monotonic() - t0))
        await telegram_bot.post_text_to_mission(
            mission_id,
            f"\u23f0 _Agent toujours en cours apres {total}_\n"
            f"Mission ID: `{mission_id}`",
        )

    # --- Mission queries ---

    @app.get("/api/missions/{mission_id}")
    async def get_mission(mission_id: str):
        """Get mission status and message count."""
        history = app.state.mission_store.get(mission_id)
        if history is None:
            return Response(status_code=404, content="Mission not found")
        return {
            "mission_id": mission_id,
            "message_count": len(history),
            "last_message": history[-1] if history else None,
        }

    @app.get("/api/missions/{mission_id}/history")
    async def get_mission_history(mission_id: str, limit: int = 50):
        """Get message history for a mission."""
        history = app.state.mission_store.get(mission_id)
        if history is None:
            return Response(status_code=404, content="Mission not found")
        return {"mission_id": mission_id, "messages": history[-limit:]}

    # --- Agent/machine listing ---

    @app.get("/api/agents")
    async def list_agents(filter: str = "all"):
        agents = await registry.list_agents(
            filter_status=filter if filter != "all" else None
        )

        # Enrich with active session info
        machine_sessions = app.state.machine_sessions
        for agent in agents:
            mid = agent.get("machine_id", "")
            pid = agent.get("project_id", "")
            sessions = machine_sessions.get(mid, [])
            session = next((s for s in sessions if s["project"] == pid), None)
            agent["session"] = session

        return {"agents": agents}

    @app.get("/api/machines")
    async def list_machines():
        machines = await registry.list_machines()
        return {"machines": machines}

    @app.delete("/api/machines/{machine_id}")
    async def delete_machine(machine_id: str):
        """Remove a machine and all its projects from the registry."""
        await registry.remove_machine(machine_id)
        return {"status": "deleted", "machine_id": machine_id}

    # --- Daemon-compatible endpoint (for standalone mode) ---

    @app.post("/api/message")
    async def receive_message(request: Request):
        """Handle messages routed to this machine (standalone mode).

        In standalone mode the hub also acts as a daemon, so it needs
        to accept /api/message for messages routed to itself.
        For actionable message types (ask, start_agent), launches a
        local agent via AgentLauncher if available.
        Note: mission history is already stored by /api/route, so we
        don't duplicate it here.
        """
        body = await request.body()
        data = json.loads(body)
        mission_id = data.get("mission_id", "unknown")
        msg_type = data.get("type", "send")

        # Launch agent for actionable message types (non-blocking)
        if msg_type in ("ask", "start_agent") and app.state.launcher:
            to_agent = data.get("to_agent", "")
            project = to_agent.split("/", 1)[1] if "/" in to_agent else to_agent
            payload = data.get("payload", {})
            mission = payload.get("mission") or payload.get("message", "")
            agent_command = payload.get("agent_command")

            project_path = app.state.project_paths.get(project, ".")
            logger.info(
                "Standalone launching agent for %s in %s (mission=%s)",
                project, project_path, mission_id,
            )

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
                return {
                    "status": "launch_failed",
                    "mission_id": mission_id,
                    "error": str(e),
                }

        return {"status": "received", "mission_id": mission_id}

    @app.get("/api/missions/{mission_id}/daemon-status")
    async def get_daemon_mission_status(mission_id: str):
        """Proxy mission status from the daemon running the mission.

        First checks the local launcher (standalone mode), then looks up
        the mission in mission_store to find the target daemon.
        """
        # Check local launcher first (standalone mode)
        if app.state.launcher:
            result = app.state.launcher.get_status(mission_id)
            if result:
                return {
                    "mission_id": mission_id,
                    "status": result.status,
                    "output": result.output,
                    "started_at": result.started_at,
                    "finished_at": result.finished_at,
                    "feedback": [
                        {"timestamp": f.timestamp, "kind": f.kind, "summary": f.summary}
                        for f in result.feedback
                    ],
                    "feedback_total": len(result.feedback),
                    "turn_count": result.turn_count,
                }

        # Look up mission in store to find target machine
        history = app.state.mission_store.get(mission_id)
        if not history:
            return Response(status_code=404, content="Mission not found")

        # Find target machine from the last message
        last_msg = history[-1]
        to_agent = last_msg.get("to_agent", "")
        machine_id = to_agent.split("/")[0] if "/" in to_agent else to_agent

        machine = await registry.get_machine(machine_id)
        if not machine:
            return Response(status_code=404, content=f"Machine {machine_id} not found")

        # Forward status request to the daemon
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{machine['daemon_url']}/api/missions/{mission_id}"
                )
                if resp.status_code == 404:
                    return {
                        "mission_id": mission_id,
                        "status": "not_found",
                        "error": f"Mission not found on {machine_id} daemon (may have expired)",
                    }
                try:
                    return resp.json()
                except Exception:
                    return {
                        "mission_id": mission_id,
                        "status": "parse_error",
                        "error": f"Invalid response from daemon (HTTP {resp.status_code})",
                    }
        except Exception as e:
            return {"mission_id": mission_id, "status": "unreachable", "error": str(e)}

    # --- Feedback ---

    @app.post("/api/feedback")
    async def submit_feedback(request: Request):
        """Store structured feedback from agents."""
        data = await request.json()
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "from_agent": data.get("from_agent", "unknown"),
            "type": data.get("type", "note"),
            "description": data.get("description", ""),
            "context": data.get("context", ""),
        }
        feedback_path = Path("data/feedback.jsonl")
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        with open(feedback_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        # Notify via Telegram
        if telegram_bot:
            type_emoji = {"bug": "\U0001f41b", "improvement": "\U0001f4a1", "note": "\U0001f4ac"}.get(
                entry["type"], "\U0001f4ac"
            )
            tg_text = (
                f"{type_emoji} *Feedback* ({entry['type']})\n\n"
                f"*From:* `{entry['from_agent']}`\n"
                f"{entry['description']}"
            )
            if entry["context"]:
                tg_text += f"\n\n*Context:* {entry['context']}"
            try:
                await telegram_bot.app.bot.send_message(
                    chat_id=telegram_bot.supergroup_id,
                    text=tg_text,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning("Failed to send feedback to Telegram: %s", e)

        return {"status": "stored", "timestamp": entry["timestamp"]}

    @app.get("/api/feedback")
    async def list_feedback(limit: int = 50):
        """List recent feedback entries."""
        feedback_path = Path("data/feedback.jsonl")
        if not feedback_path.exists():
            return {"feedback": []}
        lines = feedback_path.read_text().strip().split("\n")
        entries = [json.loads(line) for line in lines[-limit:] if line.strip()]
        return {"feedback": entries}

    # --- Skill distribution ---

    @app.get("/api/skill/intercom")
    async def get_intercom_skill():
        """Serve the /intercom skill file for remote installation."""
        skill_path = Path(__file__).parent.parent.parent / ".claude" / "commands" / "intercom.md"
        if not skill_path.exists():
            return Response(content="Skill not found", status_code=404)
        return Response(content=skill_path.read_text(), media_type="text/markdown")

    return app
