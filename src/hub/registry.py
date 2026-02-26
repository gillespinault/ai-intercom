"""Async SQLite-backed registry for machines and projects."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

_CREATE_MACHINES = """
CREATE TABLE IF NOT EXISTS machines (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tailscale_ip TEXT NOT NULL DEFAULT '',
    daemon_url TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_seen TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
    machine_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    path TEXT NOT NULL DEFAULT '',
    agent_command TEXT NOT NULL DEFAULT 'claude',
    PRIMARY KEY (machine_id, project_id),
    FOREIGN KEY (machine_id) REFERENCES machines(id)
)
"""


class Registry:
    """Async SQLite registry for machine and project management."""

    def __init__(self, db_path: str = "data/registry.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open database and create tables."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute(_CREATE_MACHINES)
        await self._db.execute(_CREATE_PROJECTS)
        await self._db.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def register_machine(
        self,
        machine_id: str,
        display_name: str,
        tailscale_ip: str,
        daemon_url: str,
        token: str,
    ) -> None:
        """Register or update a machine (upsert)."""
        assert self._db is not None
        await self._db.execute(
            """
            INSERT INTO machines (id, display_name, tailscale_ip, daemon_url, token, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'unknown', datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                tailscale_ip = excluded.tailscale_ip,
                daemon_url = excluded.daemon_url,
                token = excluded.token
            """,
            (machine_id, display_name, tailscale_ip, daemon_url, token),
        )
        await self._db.commit()

    async def get_machine(self, machine_id: str) -> dict | None:
        """Get a machine by ID. Returns dict or None."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM machines WHERE id = ?", (machine_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def get_machine_token(self, machine_id: str) -> str | None:
        """Get the token for a machine. Returns token string or None."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT token FROM machines WHERE id = ?", (machine_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return row["token"]

    async def register_project(
        self,
        machine_id: str,
        project_id: str,
        description: str,
        capabilities: list[str],
        path: str,
        agent_command: str = "claude",
    ) -> None:
        """Register or update a project on a machine (upsert)."""
        assert self._db is not None
        caps_json = json.dumps(capabilities)
        await self._db.execute(
            """
            INSERT INTO projects (machine_id, project_id, description, capabilities, path, agent_command)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_id, project_id) DO UPDATE SET
                description = excluded.description,
                capabilities = excluded.capabilities,
                path = excluded.path,
                agent_command = excluded.agent_command
            """,
            (machine_id, project_id, description, caps_json, path, agent_command),
        )
        await self._db.commit()

    async def update_heartbeat(
        self, machine_id: str, active_agents: list[str] | None = None
    ) -> None:
        """Update machine heartbeat: set last_seen to now and status to online."""
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE machines SET last_seen = ?, status = 'online' WHERE id = ?",
            (now, machine_id),
        )
        await self._db.commit()

    async def list_agents(
        self,
        filter_status: str | None = None,
        filter_machine: str | None = None,
    ) -> list[dict]:
        """List projects (agents) joined with their machines.

        Optionally filter by machine status or machine id.
        """
        assert self._db is not None
        query = """
            SELECT
                p.machine_id,
                p.project_id,
                p.description,
                p.capabilities,
                p.tags,
                p.path,
                p.agent_command,
                m.display_name AS machine_name,
                m.status AS machine_status,
                m.tailscale_ip,
                m.daemon_url
            FROM projects p
            JOIN machines m ON p.machine_id = m.id
        """
        conditions: list[str] = []
        params: list[str] = []

        if filter_status is not None:
            conditions.append("m.status = ?")
            params.append(filter_status)
        if filter_machine is not None:
            conditions.append("p.machine_id = ?")
            params.append(filter_machine)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY p.machine_id, p.project_id"

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["capabilities"] = json.loads(d["capabilities"])
                d["tags"] = json.loads(d["tags"])
                results.append(d)
            return results

    async def list_machines(self) -> list[dict]:
        """List all registered machines."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM machines ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def revoke_machine(self, machine_id: str) -> None:
        """Revoke a machine: set status to 'revoked' and clear token."""
        assert self._db is not None
        await self._db.execute(
            "UPDATE machines SET status = 'revoked', token = '' WHERE id = ?",
            (machine_id,),
        )
        await self._db.commit()

    async def update_project(
        self, machine_id: str, project_id: str, **kwargs: str
    ) -> None:
        """Update specific fields of a project."""
        assert self._db is not None
        if not kwargs:
            return
        allowed = {"description", "capabilities", "tags", "path", "agent_command"}
        set_clauses: list[str] = []
        params: list[str] = []
        for key, value in kwargs.items():
            if key not in allowed:
                raise ValueError(f"Cannot update field: {key}")
            if key in ("capabilities", "tags") and isinstance(value, list):
                value = json.dumps(value)
            set_clauses.append(f"{key} = ?")
            params.append(value)
        params.extend([machine_id, project_id])
        await self._db.execute(
            f"UPDATE projects SET {', '.join(set_clauses)} "
            f"WHERE machine_id = ? AND project_id = ?",
            params,
        )
        await self._db.commit()

    async def remove_project(self, machine_id: str, project_id: str) -> None:
        """Remove a project from the registry."""
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM projects WHERE machine_id = ? AND project_id = ?",
            (machine_id, project_id),
        )
        await self._db.commit()
