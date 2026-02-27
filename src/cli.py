from __future__ import annotations

import argparse
import asyncio
import logging
import sys


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
        tools = IntercomTools(client, config.machine_id, "default")
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
