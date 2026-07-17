import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from zozo_mcp_client import _configure


def test_configure_zozo_over_streamable_http_mcp():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _reply(self, status, payload=None, session=False):
            raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
            self.send_response(status)
            if session:
                self.send_header("Mcp-Session-Id", "test-session")
            if raw:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def do_DELETE(self):
            self._reply(202)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            if request["method"] == "initialize":
                self._reply(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"protocolVersion": "2025-06-18"},
                    },
                    session=True,
                )
                return
            if request["method"] == "notifications/initialized":
                self._reply(202)
                return
            assert self.headers.get("Mcp-Session-Id") == "test-session"
            assert request["method"] == "tools/call"
            tool = request["params"]["name"]
            arguments = request["params"]["arguments"]
            calls.append((tool, arguments))
            result = {"status": "success"}
            if tool == "get_active_groups":
                result.update(groups=[], group_count=0)
            elif tool == "create_group":
                result["group_uuid"] = arguments["type"].lower()
            elif tool == "get_static_deformation_status":
                result.update(is_deforming=False, has_cache=False, frame_count=0)
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}]
                },
            }
            self._reply(200, response)

    server = ThreadingHTTPServer(("localhost", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _configure(
            {
                "port": server.server_port,
                "cloth_object": "CLOTH",
                "body_object": "BODY",
                "cloth_group": "Yohsai Cloth",
                "body_group": "Yohsai Body",
                "scene_parameters": {"step_size": 0.005},
                "cloth_properties": {"contact_gap": 0.001},
                "body_properties": {"contact_gap": 0.001},
            }
        )
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()

    assert result["status"] == "success"
    assert result["cloth_group_uuid"] == "shell"
    assert result["body_group_uuid"] == "static"
    assert [name for name, _arguments in calls] == [
        "get_active_groups",
        "create_group",
        "add_objects_to_group",
        "set_group_material_properties",
        "create_group",
        "add_objects_to_group",
        "set_group_material_properties",
        "set_scene_parameters",
        "get_static_deformation_status",
    ]


if __name__ == "__main__":
    test_configure_zozo_over_streamable_http_mcp()
    print("ZOZO_MCP_CLIENT_OK")
