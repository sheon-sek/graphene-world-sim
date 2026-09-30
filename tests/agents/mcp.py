"""The only door an evaluated agent has into the plant: one MCP tool call against the Runtime MCP
server that ignition-mcp puts on the Ignition gateway.

    python tests/agents/mcp.py tools
    python tests/agents/mcp.py call tag_browse '{"path": "[DemoTwin]Chiller", "maxResults": 50}'

The endpoint and token come from the file `GWS_AGENT_MCP` names (default
`.agents/mcp.json`: `{"url": ..., "token": ...}`), which the evaluation harness writes. It
speaks MCP Streamable HTTP (JSON-RPC over POST; a reply may be plain JSON or an event stream)
and prints the tool's structured result as JSON.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL = "2025-06-18"
CONFIG = Path(
    os.environ.get("GWS_AGENT_MCP", Path(__file__).resolve().parents[2] / ".agents" / "mcp.json")
)
MAX_BYTES = 4 * 1024 * 1024


class Session:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self.session: str | None = None
        self.ids = 0

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        req = urllib.request.Request(self.url, data=json.dumps(message).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("X-Ignition-API-Token", self.token)
        if self.session:
            req.add_header("Mcp-Session-Id", self.session)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                self.session = r.headers.get("Mcp-Session-Id") or self.session
                kind, body = r.headers.get("Content-Type", ""), r.read(MAX_BYTES).decode()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"MCP endpoint answered HTTP {e.code}: {e.read(800).decode()}") from e
        if "text/event-stream" in kind:
            events = [json.loads(x[5:]) for x in body.splitlines() if x.startswith("data:")]
            return events[-1] if events else None
        return json.loads(body) if body.strip() else None

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.ids += 1
        reply = self._post({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params})
        if reply is None or "error" in reply:
            raise SystemExit(f"{method} failed: {reply}")
        return reply["result"]

    def open(self) -> Session:
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "gws-agent-eval", "version": "1"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self


def main(argv: list[str]) -> None:
    config = json.loads(CONFIG.read_text())
    mcp = Session(config["url"], config["token"]).open()
    if argv[:1] == ["tools"]:
        for tool in mcp.request("tools/list", {})["tools"]:
            params = sorted(tool.get("inputSchema", {}).get("properties", {}))
            print(f"{tool['name']}({', '.join(params)}): {tool.get('description', '')[:300]}")
        return
    if len(argv) < 2 or argv[0] != "call":
        raise SystemExit(__doc__)
    arguments = json.loads(argv[2]) if len(argv) > 2 else {}
    result = mcp.request("tools/call", {"name": argv[1], "arguments": arguments})
    out = result.get("structuredContent")
    if out is None:
        out = [c.get("text", c) for c in result.get("content", [])]
    if result.get("isError"):
        print("TOOL ERROR:", end=" ")
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
