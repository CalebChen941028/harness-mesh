"""Suite-wide guards.

The default test run must not touch the network.  Real-machine suites opt out
with ``HARNESS_MESH_REAL_MACHINE=1`` because they launch installed harnesses
and the live market checks.
"""

from __future__ import annotations

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def block_network_by_default(monkeypatch):
    if os.environ.get("HARNESS_MESH_REAL_MACHINE") == "1":
        yield
        return

    def blocked(*args, **kwargs):
        raise AssertionError(
            "network access is forbidden in the default test suite; "
            "use httpx.MockTransport or set HARNESS_MESH_REAL_MACHINE=1"
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    yield
