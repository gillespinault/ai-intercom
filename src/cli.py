from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def _detect_current_project(config) -> str:
    """Detect which project the MCP server is running in based on CWD.

    Walks up from the current working directory looking for CLAUDE.md or
    .claude/ markers.  If a match is found among discovered projects, return
    that project ID.  Otherwise return "home" (general admin agent).
    """
    import os
    from pathlib import Path

    cwd = Path(os.getcwd()).resolve()

    # Build a mapping of resolved paths -> project IDs from config
    project_map: dict[Path, str] = {}
    projects = config.projects
    if not projects:
        scan_paths = config.discovery.get("scan_paths", [])
        if scan_paths:
            from src.daemon.main import _discover_projects
            projects = _discover_projects(scan_paths)

    for proj in projects:
        proj_path = Path(proj.get("path", ".")).resolve()
        project_map[proj_path] = proj["id"]

    # Check if CWD is inside any known project (walk up)
    path = cwd
    while True:
        if path in project_map:
            return project_map[path]
        if path.parent == path:
            break
        path = path.parent

    return "home"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="AI-Intercom")
    sub = parser.add_subparsers(dest="command")

    # Hub
    hub_parser = sub.add_parser("hub", help="Run the central hub")
    hub_parser.add_argument("--config", default="~/.config/ai-intercom/config.yml")

    # Daemon
    daemon_parser = sub.add_parser("daemon", help="Run a machine daemon")
    daemon_parser.add_argument("--config", default="~/.config/ai-intercom/config.yml")

    # Standalone (hub + daemon)
    standalone_parser = sub.add_parser("standalone", help="Run hub + daemon")
    standalone_parser.add_argument("--config", default="~/.config/ai-intercom/config.yml")

    # MCP server
    mcp_parser = sub.add_parser("mcp-server", help="Run MCP server for local agents")
    mcp_parser.add_argument("--config", default="~/.config/ai-intercom/config.yml")

    args = parser.parse_args()

    if args.command == "mcp-server":
        from src.daemon.mcp_server import create_mcp_server, IntercomTools
        from src.daemon.hub_client import HubClient
        from src.shared.config import load_config
        import os

        config = load_config(os.path.expanduser(args.config))
        client = HubClient(config.hub.get("url", ""), config.auth.get("token", ""), config.machine_id)

        # Auto-detect current project from working directory
        current_project = _detect_current_project(config)

        tools = IntercomTools(client, config.machine_id, current_project)

        # --- Session registration with local daemon ---
        import uuid as _uuid
        import atexit
        from datetime import datetime, timezone
        from pathlib import Path
        import httpx

        session_id = f"s-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{_uuid.uuid4().hex[:6]}"
        inbox_dir = Path(os.path.expanduser("~/.config/ai-intercom/inbox"))
        inbox_dir.mkdir(parents=True, exist_ok=True)
        inbox_path = str(inbox_dir / f"{session_id}.jsonl")

        tools._inbox_path = inbox_path
        tools._session_id = session_id

        # Register with local daemon (best-effort)
        daemon_port = config.hub.get("daemon_port", 7700)
        _reg_data = {
            "session_id": session_id,
            "project": current_project,
            "pid": os.getpid(),
            "inbox_path": inbox_path,
        }

        try:
            with httpx.Client(timeout=5) as http:
                http.post(f"http://localhost:{daemon_port}/api/session/register", json=_reg_data)
        except Exception:
            pass  # Daemon might not be running

        def _cleanup():
            try:
                with httpx.Client(timeout=2) as http:
                    http.post(
                        f"http://localhost:{daemon_port}/api/session/unregister",
                        json={"session_id": session_id},
                    )
            except Exception:
                pass

        atexit.register(_cleanup)

        mcp = create_mcp_server(tools)
        mcp.run()
    elif args.command in ("hub", "daemon", "standalone"):
        from src.shared.config import load_config
        import os

        config = load_config(os.path.expanduser(args.config))
        if args.command == "standalone":
            config.mode = "standalone"
        elif args.command == "hub":
            config.mode = "hub"
        else:
            config.mode = "daemon"

        if config.is_hub:
            from src.hub.main import run_hub
            asyncio.run(run_hub(config))
        else:
            from src.daemon.main import run_daemon
            asyncio.run(run_daemon(config))
    else:
        parser.print_help()
        sys.exit(1)
