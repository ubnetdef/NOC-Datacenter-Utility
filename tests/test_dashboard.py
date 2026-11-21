import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from noc_dashboard import Monitor, check_website, load_config, make_handler


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503 if self.path == "/down" else 200)
        self.end_headers()

    def log_message(self, *args):
        pass


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        threading.Thread(target=cls.fixture.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.fixture.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.fixture.shutdown()
        cls.fixture.server_close()

    def test_check_success_and_http_failure(self):
        status, latency = check_website(self.base, 2)
        self.assertEqual(status, "operational")
        self.assertIsInstance(latency, int)
        self.assertEqual(check_website(self.base + "/down", 2), ("outage", None))

    def test_monitor_transitions_and_response_shape(self):
        monitor = Monitor({"services": [{"name": "Website", "url": self.base + "/down"}]})
        self.assertEqual(monitor.snapshot()[0]["status"], "unknown")
        monitor.check_once()
        result = monitor.snapshot()[0]
        self.assertEqual(result["status"], "outage")
        self.assertIsNotNone(result["checked_at"])
        self.assertNotIn("url", result)
        monitor.config["services"][0]["url"] = self.base
        monitor.check_once()
        self.assertEqual(monitor.snapshot()[0]["status"], "operational")

    def test_public_and_management_surfaces_are_separate(self):
        monitor = Monitor({
            "services": [{"name": "Public site", "url": self.base}],
            "devices": [{"name": "Private hardware", "type": "hypervisor"}],
        })
        servers = [
            ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor, surface))
            for surface in ("status", "management")
        ]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, management = [f"http://127.0.0.1:{server.server_port}" for server in servers]
            with urllib.request.urlopen(status + "/api/status") as response:
                body = response.read().decode()
                self.assertNotIn("Private hardware", body)
                self.assertNotIn(self.base, body)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(status + "/api/management")
            self.assertEqual(error.exception.code, 404)
            with urllib.request.urlopen(management + "/api/management") as response:
                self.assertEqual(json.load(response)["devices"][0]["name"], "Private hardware")
            with urllib.request.urlopen(status + "/") as response:
                self.assertIn(b"Service status", response.read())
            with urllib.request.urlopen(management + "/") as response:
                self.assertIn(b"Infrastructure overview", response.read())
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(status + "/noc_dashboard.py")
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()

    def test_invalid_configuration_fails(self):
        invalid = [
            {"services": [{"name": "site", "url": "file:///etc/passwd"}]},
            {"services": [{"name": "site", "url": "http://user:password@example.org"}]},
            {"services": [{"name": "site", "url": "https://example.org:nope"}]},
            {"services": [{"name": "site", "url": "https://example.org"}, {"name": "site", "url": "https://example.org"}]},
            {"devices": [{"name": "server", "type": "not-a-type"}]},
            {"check_timeout_seconds": 0},
            {"check_interval_seconds": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for config in invalid:
                with self.subTest(config=config):
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)


if __name__ == "__main__":
    unittest.main()
