import socket
from pathlib import Path

import pytest

LEGACY_CSV = Path(__file__).resolve().parent.parent / "steamproducts2020.csv"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail any test that tries to open a network connection."""

    def guard(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)


@pytest.fixture(scope="session")
def legacy_csv() -> Path:
    if not LEGACY_CSV.exists():
        pytest.skip(f"legacy CSV not found at {LEGACY_CSV}")
    return LEGACY_CSV
