"""Configure offline test defaults and block real HTTP requests."""
import os

os.environ["OPENROUTER_API_KEY"] = "offline-test-key"

import httpx
import pytest


@pytest.fixture(autouse=True)
def prevent_external_http(monkeypatch):
    async def blocked_async(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in offline backend tests")
    def blocked(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in offline backend tests")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    try:
        import requests
        monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    except ImportError:
        pass
