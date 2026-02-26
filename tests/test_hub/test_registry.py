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
