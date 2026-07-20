# SPDX-License-Identifier: GPL-3.0-or-later
"""Configure ZOZO through its localhost MCP server from a child process.

This must not run on Blender's main thread: ZOZO MCP queues every mutation
back to that thread.  A synchronous request from a Blender operator would
therefore wait on itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


MCP_PROTOCOL_VERSION = "2025-06-18"


class MCPClient:
    def __init__(self, port: int, timeout: float = 15.0):
        self.url = f"http://localhost:{port}/mcp"
        self.timeout = timeout
        self.session_id: str | None = None
        self.next_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
        return headers

    def _post(self, payload: dict, timeout: float | None = None) -> dict | None:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            if not self.session_id:
                self.session_id = response.headers.get("Mcp-Session-Id")
            if response.status == 202:
                return None
            raw = response.read()
            return json.loads(raw) if raw else None

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def initialize(self) -> None:
        reply = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "yohsai", "version": "0.8.0"},
                },
            },
            timeout=5.0,
        )
        if not reply or "error" in reply or not self.session_id:
            raise RuntimeError("ZOZO MCP initialize did not return a usable session.")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float | None = None) -> dict:
        reply = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            timeout=timeout,
        )
        if not reply:
            raise RuntimeError(f"ZOZO MCP returned no response for {name}.")
        if "error" in reply:
            error = reply["error"]
            raise RuntimeError(str(error.get("message", error)))
        result = reply.get("result", {})
        content = result.get("content", [])
        text = next(
            (item.get("text", "") for item in content if item.get("type") == "text"),
            "",
        )
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ZOZO MCP returned invalid data for {name}.") from exc
        if result.get("isError") or payload.get("status") == "error":
            raise RuntimeError(str(payload.get("message", f"ZOZO tool {name} failed.")))
        return payload

    def close(self) -> None:
        if not self.session_id:
            return
        request = urllib.request.Request(
            self.url,
            headers={"Mcp-Session-Id": self.session_id},
            method="DELETE",
        )
        try:
            urllib.request.urlopen(request, timeout=2.0).close()
        except (urllib.error.URLError, OSError):
            pass
        self.session_id = None


def _configure(config: dict) -> dict:
    client = MCPClient(int(config["port"]))
    capture = "not needed"
    try:
        client.initialize()
        groups = client.call_tool("get_active_groups").get("groups", [])
        owned_names = {config["cloth_group"], config["body_group"]}
        for group in groups:
            if group.get("name") in owned_names and group.get("uuid"):
                client.call_tool("delete_group", {"group_uuid": group["uuid"]})

        cloth = client.call_tool(
            "create_group", {"name": config["cloth_group"], "type": "SHELL"}
        )
        cloth_uuid = str(cloth["group_uuid"])
        client.call_tool(
            "add_objects_to_group",
            {"group_uuid": cloth_uuid, "object_names": [config["cloth_object"]]},
        )
        client.call_tool(
            "set_group_material_properties",
            {"group_uuid": cloth_uuid, "properties": config["cloth_properties"]},
        )

        body = client.call_tool(
            "create_group", {"name": config["body_group"], "type": "STATIC"}
        )
        body_uuid = str(body["group_uuid"])
        client.call_tool(
            "add_objects_to_group",
            {"group_uuid": body_uuid, "object_names": [config["body_object"]]},
        )
        client.call_tool(
            "set_group_material_properties",
            {"group_uuid": body_uuid, "properties": config["body_properties"]},
        )
        client.call_tool("set_scene_parameters", config["scene_parameters"])

        deformation = client.call_tool(
            "get_static_deformation_status",
            {"group_uuid": body_uuid, "object_name": config["body_object"]},
        )
        if deformation.get("is_deforming"):
            if not deformation.get("has_cache"):
                client.call_tool(
                    "capture_static_deformation",
                    {"group_uuid": body_uuid, "object_name": config["body_object"]},
                )
            deadline = time.monotonic() + float(config.get("capture_timeout_seconds", 300.0))
            while True:
                deformation = client.call_tool(
                    "get_static_deformation_status",
                    {"group_uuid": body_uuid, "object_name": config["body_object"]},
                )
                if deformation.get("has_cache") and int(deformation.get("frame_count", 0)) > 0:
                    capture = f"captured {int(deformation['frame_count'])} body frames"
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("ZOZO Body deformation capture did not finish within 300 seconds.")
                time.sleep(0.25)

        return {
            "status": "success",
            "message": "ZOZO groups are ready; inspect them, then use Transfer and Run Simulation.",
            "cloth_group_uuid": cloth_uuid,
            "body_group_uuid": body_uuid,
            "capture": capture,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        result = _configure(config)
    except Exception as exc:
        result = {
            "status": "error",
            "message": str(exc).strip() or type(exc).__name__,
            "exception": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
