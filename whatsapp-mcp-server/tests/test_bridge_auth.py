"""The MCP server sends the bridge's API token on every call, never an Origin
header, and never goes through a proxy. Run from whatsapp-mcp-server/:

    uv run python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import whatsapp  # noqa: E402

TOKEN = "ab" * 32


class _FakeBridge(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _FakeBridge.requests.append((self.path, dict(self.headers), json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"success": True, "message": "ok"}).encode())

    def log_message(self, *args):
        pass


class BridgeAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeBridge)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _FakeBridge.requests = []
        self.dir = tempfile.TemporaryDirectory()
        self.token_path = os.path.join(self.dir.name, "api-token")
        with open(self.token_path, "w") as f:
            f.write(TOKEN + "\n")
        self.patches = [
            mock.patch.object(whatsapp, "WHATSAPP_API_BASE_URL", self.base_url),
            mock.patch.object(whatsapp, "BRIDGE_TOKEN_PATH", self.token_path),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def test_sends_token_json_and_no_origin(self):
        response = whatsapp._bridge_post("/download", {"message_id": "m", "chat_jid": "c"})
        self.assertEqual(response.status_code, 200)
        path, headers, body = _FakeBridge.requests[-1]
        self.assertEqual(path, "/api/download")
        self.assertEqual(headers.get("Authorization"), f"Bearer {TOKEN}")
        self.assertTrue(headers.get("Content-Type", "").startswith("application/json"))
        self.assertNotIn("Origin", headers)
        self.assertEqual(headers.get("Host"), f"127.0.0.1:{self.server.server_port}")
        self.assertEqual(body, {"message_id": "m", "chat_jid": "c"})

    def test_rotated_token_is_picked_up(self):
        whatsapp._bridge_post("/download", {})
        with open(self.token_path, "w") as f:
            f.write("cd" * 32)
        whatsapp._bridge_post("/download", {})
        self.assertEqual(_FakeBridge.requests[-1][1].get("Authorization"), "Bearer " + "cd" * 32)

    def test_missing_token_sends_nothing(self):
        os.remove(self.token_path)
        with self.assertRaises(whatsapp.BridgeTokenError) as ctx:
            whatsapp._bridge_post("/download", {})
        self.assertIn("start the bridge", str(ctx.exception))
        self.assertEqual(_FakeBridge.requests, [])

    def test_empty_token_sends_nothing(self):
        open(self.token_path, "w").close()
        with self.assertRaises(whatsapp.BridgeTokenError):
            whatsapp._bridge_post("/download", {})
        self.assertEqual(_FakeBridge.requests, [])

    def test_token_error_is_a_request_exception(self):
        # Callers already catch requests.RequestException and report it, so a
        # missing token becomes an error message rather than a crash.
        self.assertTrue(issubclass(whatsapp.BridgeTokenError, whatsapp.requests.RequestException))

    def test_proxy_settings_are_ignored(self):
        env = {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
               "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict(os.environ, env):
            response = whatsapp._bridge_post("/download", {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(_FakeBridge.requests), 1)


if __name__ == "__main__":
    unittest.main()
