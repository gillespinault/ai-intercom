import pytest

from src.hub.registry import Registry


@pytest.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "registry.db")
    reg = Registry(db_path)
    await reg.init()
    yield reg
    await reg.close()


async def test_register_machine(registry):
    await registry.register_machine(
        "vps", "VPS Hostinger", "100.75.129.81", "http://100.75.129.81:7700", "ict_vps_abc123"
    )
    machine = await registry.get_machine("vps")
    assert machine is not None
    assert machine["display_name"] == "VPS Hostinger"


async def test_register_project(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.register_project("vps", "nginx", "Reverse proxy", ["nginx", "ssl"], "/etc/nginx")
    agents = await registry.list_agents(filter_machine="vps")
    assert len(agents) == 1
    assert agents[0]["project_id"] == "nginx"


async def test_update_heartbeat(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.update_heartbeat("vps", active_agents=["nginx"])
    machine = await registry.get_machine("vps")
    assert machine["status"] == "online"


async def test_list_agents_online_only(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.register_project("vps", "nginx", "proxy", ["nginx"], "/etc/nginx")
    await registry.update_heartbeat("vps")
    agents = await registry.list_agents(filter_status="online")
    assert len(agents) == 1


async def test_get_machine_token(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "my-token")
    token = await registry.get_machine_token("vps")
    assert token == "my-token"


async def test_revoke_machine(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.revoke_machine("vps")
    machine = await registry.get_machine("vps")
    assert machine["status"] == "revoked"


async def test_remove_project(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.register_project("vps", "nginx", "proxy", ["nginx"], "/etc/nginx")
    await registry.remove_project("vps", "nginx")
    agents = await registry.list_agents(filter_machine="vps")
    assert len(agents) == 0


async def test_heartbeat_stores_version(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.update_heartbeat("vps", version="0.4.0")
    machine = await registry.get_machine("vps")
    assert machine["status"] == "online"
    assert machine["version"] == "0.4.0"


async def test_heartbeat_without_version_preserves_existing(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.update_heartbeat("vps", version="0.3.0")
    await registry.update_heartbeat("vps")  # No version
    machine = await registry.get_machine("vps")
    assert machine["version"] == "0.3.0"


async def test_list_agents_includes_version(registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    await registry.register_project("vps", "nginx", "proxy", ["nginx"], "/etc/nginx")
    await registry.update_heartbeat("vps", version="0.4.0")
    agents = await registry.list_agents(filter_machine="vps")
    assert len(agents) == 1
    assert agents[0]["machine_version"] == "0.4.0"


async def test_migration_adds_version_column(tmp_path):
    """Existing DB without version column gets migrated."""
    import aiosqlite

    db_path = str(tmp_path / "old.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE machines (
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
        """)
        await db.execute("""
            CREATE TABLE projects (
                machine_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]',
                path TEXT NOT NULL DEFAULT '',
                agent_command TEXT NOT NULL DEFAULT 'claude',
                PRIMARY KEY (machine_id, project_id)
            )
        """)
        await db.execute(
            "INSERT INTO machines (id, display_name) VALUES ('old', 'Old Machine')"
        )
        await db.commit()

    reg = Registry(db_path)
    await reg.init()
    machine = await reg.get_machine("old")
    assert machine is not None
    assert machine["version"] == ""
    await reg.update_heartbeat("old", version="0.4.0")
    machine = await reg.get_machine("old")
    assert machine["version"] == "0.4.0"
    await reg.close()
