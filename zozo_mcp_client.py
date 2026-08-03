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


def _fix_windows_mojibake(text: str) -> str:
    """Repair common Windows Japanese mojibake in exception strings."""
    if not text:
        return text

    def _kana_kanji(s: str) -> int:
        return sum(
            1 for c in s if "\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff"
        )

    def _hiragana(s: str) -> int:
        return sum(1 for c in s if "\u3040" <= c <= "\u309f")

    candidates: list[tuple[int, str]] = []
    for enc_from, enc_to, weight in (
        ("latin-1", "utf-8", 100),
        ("cp1252", "utf-8", 100),
        ("latin-1", "cp932", 10),
        ("cp1252", "cp932", 10),
    ):
        try:
            fixed = text.encode(enc_from, errors="strict").decode(enc_to, errors="strict")
        except (UnicodeError, LookupError):
            continue
        if "\ufffd" in fixed or fixed == text:
            continue
        score = _kana_kanji(fixed) * weight + _hiragana(fixed) * 50
        if score > 0:
            candidates.append((score, fixed))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    if _kana_kanji(text) > 0:
        return text
    if "10061" in text:
        return (
            "WinError 10061: connection refused "
            "(ZOZO MCP is not listening; start it from the ZOZO panel or Yohsai Prepare)"
        )
    return text


def _format_exception(exc: BaseException) -> str:
    """Stable, readable message for JSON status (no mojibake)."""
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, OSError):
            winerr = getattr(reason, "winerror", None)
            if winerr == 10061 or getattr(reason, "errno", None) in (111, 61):
                return (
                    "ZOZO MCP connection refused (nothing listening on the MCP port). "
                    "Start MCP in ZOZO Contact Solver, or re-run Prepare after the add-on loads."
                )
            return _fix_windows_mojibake(f"{type(reason).__name__}: {reason}")
        return _fix_windows_mojibake(str(exc))
    if isinstance(exc, OSError):
        winerr = getattr(exc, "winerror", None)
        if winerr == 10061 or getattr(exc, "errno", None) in (111, 61):
            return "ZOZO MCP connection refused (WinError 10061 / nothing listening)"
        return _fix_windows_mojibake(f"{type(exc).__name__}: {exc}")
    return _fix_windows_mojibake(str(exc).strip() or type(exc).__name__)


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
                    "clientInfo": {"name": "yohsai", "version": "0.10.2"},
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


def _list_groups(client: MCPClient) -> list[dict]:
    payload = client.call_tool("get_active_groups")
    groups = payload.get("groups") or []
    return [group for group in groups if isinstance(group, dict)]


def _clear_group_objects(client: MCPClient, group_uuid: str, assigned: list | None) -> None:
    """Empty a dynamics group without using the broken delete_group operator path."""
    try:
        client.call_tool("remove_all_objects_from_group", {"group_uuid": group_uuid})
        return
    except Exception:
        pass
    for item in assigned or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        try:
            client.call_tool(
                "remove_object_from_group",
                {"group_uuid": group_uuid, "object_name": str(name)},
            )
        except Exception:
            continue


def _find_group_by_name(groups: list[dict], name: str) -> dict | None:
    for group in groups:
        if group.get("name") == name and group.get("uuid"):
            return group
    return None


def _ensure_group(client: MCPClient, *, name: str, group_type: str) -> str:
    """Create or reuse a named SHELL/STATIC group.

    ZOZO MCP ``delete_group`` currently calls
    ``bpy.ops.object.delete_group(group_uuid=...)``, but the operator only
    accepts ``group_index``. That aborts Prepare after the first successful
    run (when Yohsai-owned groups already exist) and leaves Dynamics empty.
    Reuse + clear avoids that broken delete path entirely.
    """
    existing = _find_group_by_name(_list_groups(client), name)
    if existing is not None:
        group_uuid = str(existing["uuid"])
        _clear_group_objects(client, group_uuid, existing.get("assigned_objects"))
        if existing.get("object_type") != group_type:
            client.call_tool(
                "set_group_type",
                {"group_uuid": group_uuid, "type": group_type},
            )
        return group_uuid

    created = client.call_tool("create_group", {"name": name, "type": group_type})
    created_uuid = _group_uuid(created)

    # create_group historically returns the last active slot, which can be a
    # pre-existing group when lower slots are free. Prefer the named match.
    matched = _find_group_by_name(_list_groups(client), name)
    if matched is not None:
        group_uuid = str(matched["uuid"])
        if matched.get("object_type") != group_type:
            client.call_tool(
                "set_group_type",
                {"group_uuid": group_uuid, "type": group_type},
            )
        return group_uuid
    return created_uuid


def _member_names(membership: dict) -> set[str]:
    names = {
        item.get("name")
        for item in (membership.get("objects") or membership.get("assigned_objects") or [])
        if isinstance(item, dict)
    }
    if not names and isinstance(membership.get("group"), dict):
        names = {
            item.get("name")
            for item in (membership["group"].get("assigned_objects") or [])
            if isinstance(item, dict)
        }
    return {str(name) for name in names if name}


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
    membership = client.call_tool("get_group_objects", {"group_uuid": group_uuid})
    members = _member_names(membership)
    # MCP may report added_objects even when the Blender operator skipped the
    # mesh (linked-duplicate / duplicate-face guards). Always require the
    # post-assign membership list to contain the object.
    if object_name not in members:
        detail = "; ".join(str(w) for w in warnings) if warnings else "object was not added"
        if object_name not in added:
            detail = f"{detail}; add_objects_to_group did not list it"
        raise RuntimeError(
            f"Failed to assign {role} object '{object_name}' to ZOZO group "
            f"{group_uuid}: {detail}"
        )


def _assert_simulatable(client: MCPClient, *, cloth_uuid: str, cloth_object: str) -> None:
    """Fail closed if ZOZO would still show 'No dynamics to simulate'."""
    groups = _list_groups(client)
    cloth = next((group for group in groups if group.get("uuid") == cloth_uuid), None)
    if cloth is None:
        raise RuntimeError(f"Cloth group {cloth_uuid} missing after ZOZO MCP setup.")
    if cloth.get("object_type") != "SHELL":
        raise RuntimeError(
            f"Cloth group is {cloth.get('object_type')!r}, expected SHELL."
        )
    assigned = {
        item.get("name")
        for item in (cloth.get("assigned_objects") or [])
        if isinstance(item, dict)
    }
    if cloth_object not in assigned:
        raise RuntimeError(
            f"Cloth object '{cloth_object}' is not assigned to the SHELL group; "
            "ZOZO would report no dynamics to simulate."
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

        cloth_uuid = _ensure_group(client, name=cloth_group, group_type="SHELL")
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

        body_uuid = _ensure_group(client, name=body_group, group_type="STATIC")
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
        _assert_simulatable(
            client, cloth_uuid=cloth_uuid, cloth_object=cloth_object
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
            "message": _format_exception(exc),
            "exception": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
