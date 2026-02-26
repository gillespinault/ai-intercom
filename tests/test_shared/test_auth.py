import time
import pytest
from src.shared.auth import sign_request, verify_request


def test_sign_and_verify():
    token = "test-secret-token"
    body = b'{"hello": "world"}'
    machine_id = "vps"
    headers = sign_request(body, machine_id, token)
    assert headers["X-Intercom-Machine"] == "vps"
    assert "X-Intercom-Timestamp" in headers
    assert headers["X-Intercom-Signature"].startswith("sha256=")
    assert verify_request(body, headers, token) is True


def test_verify_wrong_token():
    token = "correct-token"
    body = b'{"hello": "world"}'
    headers = sign_request(body, "vps", token)
    assert verify_request(body, headers, "wrong-token") is False


def test_verify_replay_attack():
    token = "test-secret"
    body = b'{"hello": "world"}'
    headers = sign_request(body, "vps", token)
    headers["X-Intercom-Timestamp"] = str(int(time.time()) - 120)
    assert verify_request(body, headers, token) is False


def test_verify_tampered_body():
    token = "test-secret"
    body = b'{"hello": "world"}'
    headers = sign_request(body, "vps", token)
    tampered = b'{"hello": "hacker"}'
    assert verify_request(tampered, headers, token) is False
