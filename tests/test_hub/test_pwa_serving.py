"""Tests for PWA static file serving."""

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.hub.attention_api import create_pwa_router


@pytest.fixture
def pwa_dir():
    with tempfile.TemporaryDirectory() as d:
        pwa = Path(d)
        (pwa / "index.html").write_text("<!DOCTYPE html><html><body>test</body></html>")
        (pwa / "styles.css").write_text("body { color: red; }")
        (pwa / "app.js").write_text("console.log('hello');")
        (pwa / "manifest.json").write_text('{"name": "test"}')
        yield pwa


def test_pwa_router_creation():
    """Verify the router creation doesn't error."""
    router = create_pwa_router()
    assert router is not None


def test_pwa_index_serves_html(pwa_dir):
    """Test that /attention serves index.html with correct media type."""
    app = FastAPI()
    router = create_pwa_router()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/attention")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_pwa_manifest_exists():
    """Verify manifest.json was created."""
    manifest = Path(__file__).parent.parent.parent / "pwa" / "manifest.json"
    assert manifest.exists(), "pwa/manifest.json should exist"


def test_pwa_sw_exists():
    """Verify sw.js was created."""
    sw = Path(__file__).parent.parent.parent / "pwa" / "sw.js"
    assert sw.exists(), "pwa/sw.js should exist"


def test_pwa_index_exists():
    """Verify index.html was created."""
    index = Path(__file__).parent.parent.parent / "pwa" / "index.html"
    assert index.exists(), "pwa/index.html should exist"


def test_pwa_styles_exists():
    """Verify styles.css was created."""
    styles = Path(__file__).parent.parent.parent / "pwa" / "styles.css"
    assert styles.exists(), "pwa/styles.css should exist"


def test_pwa_app_exists():
    """Verify app.js was created."""
    app_js = Path(__file__).parent.parent.parent / "pwa" / "app.js"
    assert app_js.exists(), "pwa/app.js should exist"


def test_pwa_static_css():
    """Test that /attention/styles.css serves CSS."""
    app = FastAPI()
    router = create_pwa_router()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/attention/styles.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")


def test_pwa_static_js():
    """Test that /attention/app.js serves JavaScript."""
    app = FastAPI()
    router = create_pwa_router()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/attention/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("content-type", "")


def test_pwa_static_manifest():
    """Test that /attention/manifest.json serves JSON."""
    app = FastAPI()
    router = create_pwa_router()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/attention/manifest.json")
    assert resp.status_code == 200
    assert "json" in resp.headers.get("content-type", "")


def test_pwa_static_not_found():
    """Test that missing files return 404."""
    app = FastAPI()
    router = create_pwa_router()
    app.include_router(router)
    client = TestClient(app)

    resp = client.get("/attention/nonexistent.xyz")
    assert resp.status_code == 404
