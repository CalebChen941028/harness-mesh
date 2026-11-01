"""Minimal stdio MCP server used by the real-machine environment probe.

It records, at startup, the working directory and the environment variables it
received, then serves a single ``ping`` tool.  The harness under test only has
to start it once; no model turn is needed for the observation itself.

Usage: ``python mcp_probe_server.py <harness> <output-dir>``

The output directory may also be supplied through ``PROBE_OUT``; some hosts
(the very ones this probe investigates) do not forward undeclared environment
variables into the MCP child, so the positional argument is the reliable path.
"""

from __future__ import annotations

import json
import os
import sys
import time

name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
out = os.environ.get("PROBE_OUT") or (sys.argv[2] if len(sys.argv) > 2 else "")
if not out:
    raise SystemExit("usage: mcp_probe_server.py <harness> <output-dir>")
os.makedirs(out, exist_ok=True)
payload = {
    "name": name,
    "time": time.time(),
    "cwd": os.getcwd(),
    "ppid": os.getppid(),
    "values": {key: value for key, value in os.environ.items() if key.startswith(("HARNESS_MESH_", "PROBE_"))},
    "env_key_count": len(os.environ),
    "env_key_names": sorted(os.environ),
}
with open(os.path.join(out, f"{name}.json"), "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=1)

from mcp.server.fastmcp import FastMCP  # noqa: E402 - write the probe file before importing mcp

server = FastMCP("harness-mesh-probe")


@server.tool()
def ping() -> str:
    """Return pong; proves the stdio transport is alive."""

    return "pong"


server.run()
