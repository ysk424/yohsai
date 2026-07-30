# SPDX-License-Identifier: GPL-3.0-or-later
"""Configure ZOZO through its localhost MCP server from a child process.

This must not run on Blender's main thread: ZOZO MCP queues every mutation
back to that thread.  A synchronous request from a Blender operator would
therefore wait on itself.

The sequence matches the official Contact Solver MCP setup path:
  delete owned groups → create SHELL/STATIC → add_objects_to_group →
  set material / scene parameters → optional static deformation capture.
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
                    "clientInfo": {"name": "yohsai", "version": "0.9.3"},
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


def _group_uuid(payload: dict) -> str:
    uuid = payload.get("group_uuid") or (payload.get("group") or {}).get("uuid")
    if not uuid:
        raise RuntimeError(f"ZOZO MCP create_group returned no group_uuid: {payload!r}")
    return str(uuid)


def _delete_owned_groups(client: MCPClient, owned_names: set[str]) -> None:
    groups = client.call_tool("get_active_groups").get("groups", [])
    for group in groups:
        name = group.get("name")
        uuid = group.get("uuid")
        if name in owned_names and uuid:
            client.call_tool("delete_group", {"group_uuid": uuid})


def _assign_object(
    client: MCPClient,
    *,
    group_uuid: str,
    object_name: str,
    role: str,
) -> None:
    """Add one Blender object to a ZOZO dynamics group and verify membership."""
    result = client.call_tool(
        "add_objects_to_group",
        {"group_uuid": group_uuid, "object_names": [object_name]},
    )
    warnings = result.get("warnings") or []
    added = {
        item.get("name")
        for item in (result.get("added_objects") or [])
        if isinstance(item, dict)
    }
    if object_name not in added:
        detail = "; ".join(str(w) for w in warnings) if warnings else "object was not added"
        raise RuntimeError(
            f"Failed to assign {role} object '{object_name}' to ZOZO group {group_uuid}: {detail}"
        )
    if warnings:
        # Soft warnings after a successful add (e.g. name renames) stay informative only.
        pass

    membership = client.call_tool("get_group_objects", {"group_uuid": group_uuid})
    member_names = {
        item.get("name")
        for item in (membership.get("objects") or membership.get("assigned_objects") or [])
        if isinstance(item, dict)
    }
    # Some handler versions only return the serialized group.
    if not member_names and isinstance(membership.get("group"), dict):
        member_names = {
            item.get("name")
            for item in (membership["group"].get("assigned_objects") or [])
            if isinstance(item, dict)
        }
    if member_names and object_name not in member_names:
        raise RuntimeError(
            f"ZOZO group {group_uuid} does not list {role} object '{object_name}' after assignment."
        )


def _connection_summary(client: MCPClient) -> str:
    """Best-effort backend status for the status line; never fails setup."""
    try:
        info = client.call_tool("get_connection_info")
    except Exception as exc:
        return f"connection unknown ({type(exc).__name__})"
    status = info.get("connection_status") or {}
    connected = bool(status.get("connected"))
    running = bool(status.get("server_running"))
    ctype = status.get("type") or "unknown"
    project = (info.get("project_info") or {}).get("project_name") or ""
    if connected and running:
        base = f"solver connected ({ctype}"
    elif connected:
        base = f"solver connected, server not running ({ctype}"
    else:
        base = f"solver not connected ({ctype}"
    if project:
        base += f", project={project}"
    return base + ")"


def _capture_body_deformation(
    client: MCPClient,
    *,
    body_uuid: str,
    body_object: str,
    timeout_seconds: float,
) -> str:
    deformation = client.call_tool(
        "get_static_deformation_status",
        {"group_uuid": body_uuid, "object_name": body_object},
    )
    if not deformation.get("is_deforming"):
        return "not needed"
    if not deformation.get("has_cache"):
        client.call_tool(
            "capture_static_deformation",
            {"group_uuid": body_uuid, "object_name": body_object},
        )
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        deformation = client.call_tool(
            "get_static_deformation_status",
            {"group_uuid": body_uuid, "object_name": body_object},
        )
        if deformation.get("has_cache") and int(deformation.get("frame_count", 0)) > 0:
            return f"captured {int(deformation['frame_count'])} body frames"
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ZOZO Body deformation capture did not finish within {timeout_seconds:.0f} seconds."
            )
        time.sleep(0.25)


def _configure(config: dict) -> dict:
    client = MCPClient(int(config["port"]))
    try:
        client.initialize()
        connection = _connection_summary(client)

        cloth_group = str(config["cloth_group"])
        body_group = str(config["body_group"])
        cloth_object = str(config["cloth_object"])
        body_object = str(config["body_object"])

        _delete_owned_groups(client, {cloth_group, body_group})

        cloth = client.call_tool(
            "create_group", {"name": cloth_group, "type": "SHELL"}
        )
        cloth_uuid = _group_uuid(cloth)
        _assign_object(
            client,
            group_uuid=cloth_uuid,
            object_name=cloth_object,
            role="cloth",
        )
        client.call_tool(
            "set_group_material_properties",
            {"group_uuid": cloth_uuid, "properties": config["cloth_properties"]},
        )

        body = client.call_tool(
            "create_group", {"name": body_group, "type": "STATIC"}
        )
        body_uuid = _group_uuid(body)
        _assign_object(
            client,
            group_uuid=body_uuid,
            object_name=body_object,
            role="body",
        )
        client.call_tool(
            "set_group_material_properties",
            {"group_uuid": body_uuid, "properties": config["body_properties"]},
        )
        client.call_tool("set_scene_parameters", config["scene_parameters"])

        capture = _capture_body_deformation(
            client,
            body_uuid=body_uuid,
            body_object=body_object,
            timeout_seconds=float(config.get("capture_timeout_seconds", 300.0)),
        )

        return {
            "status": "success",
            "message": (
                "ZOZO groups are ready with cloth and body assigned; "
                "inspect them, then use Transfer and Run Simulation."
            ),
            "cloth_group_uuid": cloth_uuid,
            "body_group_uuid": body_uuid,
            "cloth_object": cloth_object,
            "body_object": body_object,
            "capture": capture,
            "connection": connection,
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
