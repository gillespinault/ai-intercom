import pytest
from src.daemon.hub_client import HubClient


@pytest.fixture
def client():
    return HubClient(
        hub_url="http://localhost:7700",
        token="test-token",
        machine_id="test-machine",
    )


def test_client_initialization(client):
    assert client.hub_url == "http://localhost:7700"
    assert client.machine_id == "test-machine"


def test_client_builds_auth_headers(client):
    headers = client._auth_headers(b"test body")
    assert "X-Intercom-Machine" in headers
    assert "X-Intercom-Signature" in headers
