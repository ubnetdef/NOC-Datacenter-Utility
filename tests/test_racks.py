import copy
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

from device_catalog import load_catalog
from discovery import DiscoveryError
from noc_dashboard import DeviceHealthMonitor, Monitor, build_alerts, make_handler
from rack_store import RackStore, empty_hardware_details, validate


def sample():
    return {
        "racks": [{"id": "rack", "name": "Rack A", "units": 10}],
        "devices": [
            {"id": "server", "name": "Server", "type": "hypervisor", "rack_id": "rack", "unit": 1, "height": 2, "front_ports": ["USB"], "rear_ports": ["eth0"], "drives": ["Bay 1"], "model_id": None, "variant_id": None, "port_config": {}, "hardware_details": empty_hardware_details(), "management_ip": None, "management_protocol": "redfish", "credential_ref": None, "discovery": None},
            {"id": "switch", "name": "Switch", "type": "network", "rack_id": "rack", "unit": 4, "height": 1, "front_ports": [], "rear_ports": ["1"], "drives": [], "model_id": None, "variant_id": None, "port_config": {}, "hardware_details": empty_hardware_details(), "management_ip": None, "management_protocol": "redfish", "credential_ref": None, "discovery": None},
        ],
        "cables": [{"id": "cable", "device_a": "server", "port_a": "eth0", "device_b": "switch", "port_b": "1", "label": ""}],
    }


class RackTests(unittest.TestCase):
    def test_local_test_auth_rejects_anonymous_requests_and_logs_out(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = load_catalog(Path(__file__).resolve().parents[1] / "hardware_catalog.json")
            model_map = {model["id"]: model for model in catalog["models"]}
            store = RackStore(Path(directory) / "racks.db", [], model_map)
            with patch.dict(os.environ, {
                "NOC_TEST_AUTH": "1",
                "NOC_TEST_AUTH_USERNAME": "admin",
                "NOC_TEST_AUTH_PASSWORD": "admin",
            }):
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    make_handler(Monitor({}), "management", store, catalog),
                )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with opener.open(base + "/") as response:
                    self.assertEqual(response.geturl(), base + "/login")
                    self.assertIn(b"PRIVATE NETWORK ONLY", response.read())
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(base + "/api/racks")
                self.assertEqual(error.exception.code, 401)

                def login(username, password):
                    body = urllib.parse.urlencode({
                        "httpd_username": username,
                        "httpd_password": password,
                        "login": "Sign in",
                    }).encode()
                    request = urllib.request.Request(
                        base + "/dologin",
                        data=body,
                        headers={"Origin": base, "Content-Type": "application/x-www-form-urlencoded"},
                    )
                    return opener.open(request)

                with login("admin", "wrong") as response:
                    self.assertEqual(response.geturl(), base + "/login?failed=1")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(base + "/api/racks")
                self.assertEqual(error.exception.code, 401)

                with login("admin", "admin") as response:
                    self.assertEqual(response.geturl(), base + "/")
                with opener.open(base + "/api/racks") as response:
                    self.assertEqual(response.status, 200)
                with opener.open(base + "/logout") as response:
                    self.assertEqual(response.geturl(), base + "/login?logged_out=1")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(base + "/api/racks")
                self.assertEqual(error.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()

    def test_validation(self):
        state = sample()
        validate(state)
        invalid = []
        overlap = copy.deepcopy(state)
        overlap["devices"][1]["unit"] = 2
        invalid.append(overlap)
        outside = copy.deepcopy(state)
        outside["devices"][0]["unit"] = 10
        invalid.append(outside)
        orphan = copy.deepcopy(state)
        orphan["cables"][0]["device_b"] = "missing"
        invalid.append(orphan)
        duplicate_port = copy.deepcopy(state)
        duplicate_port["cables"].append({"id": "other", "device_a": "server", "port_a": "ETH0", "device_b": "switch", "port_b": "2", "label": ""})
        invalid.append(duplicate_port)
        too_many_ports = copy.deepcopy(state)
        too_many_ports["devices"][0]["front_ports"] = [f"Port {index}" for index in range(49)]
        invalid.append(too_many_ports)
        invalid_label = copy.deepcopy(state)
        invalid_label["devices"][0]["drives"] = ["x" * 41]
        invalid.append(invalid_label)
        invalid_management_address = copy.deepcopy(state)
        invalid_management_address["devices"][0]["management_ip"] = "8.8.8.8"
        invalid.append(invalid_management_address)
        invalid_hardware_details = copy.deepcopy(state)
        invalid_hardware_details["devices"][0]["hardware_details"]["serial_number"] = "x" * 1001
        invalid.append(invalid_hardware_details)
        invalid_discovery_gaps = copy.deepcopy(state)
        invalid_discovery_gaps["devices"][0]["discovery"] = {
            "protocol": "snmp", "checked_at": "now", "details": {}, "unavailable": [""] ,
        }
        invalid.append(invalid_discovery_gaps)
        invalid_port = copy.deepcopy(state)
        invalid_port["devices"][0]["port_config"] = {"rear-nic-1": {
            "label": "Uplink", "mac": "", "ip": "", "vlan": "4095", "notes": "",
        }}
        invalid.append(invalid_port)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate(candidate)

    def test_network_alerts_require_attached_link_telemetry(self):
        state = sample()
        validate(state)
        health = {"switch": {"status": "online", "telemetry": {"network_ports": [
            {"id": "1", "name": "Ethernet1", "link": "down"},
        ]}}}
        alerts = build_alerts([], state, health)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "network")
        health["switch"]["telemetry"]["network_ports"][0]["link"] = "up"
        self.assertEqual(build_alerts([], state, health), [])

    def test_degraded_network_and_resource_alarms_use_reported_thresholds(self):
        state = sample()
        health = {"switch": {"status": "online", "telemetry": {
            "network_ports": [{"id": "1", "name": "Ethernet 1", "link": "up", "health": "warning"}],
            "resources": [{"id": "cpu", "name": "CPU", "health": "warning", "threshold_exceeded": True}],
        }}}
        alerts = build_alerts([], state, health)
        self.assertEqual({alarm["kind"] for alarm in alerts}, {"network", "resource"})
        self.assertTrue(any("Network port 1 has entered a degraded state" in alarm["detail"] for alarm in alerts))
        self.assertTrue(any("High CPU threshold counter detected" in alarm["detail"] for alarm in alerts))
        health["switch"]["telemetry"]["network_ports"][0]["link"] = "down"
        failed = build_alerts([], state, health)
        self.assertTrue(any("connection failed" in alarm["detail"] for alarm in failed))
        self.assertTrue(any("possible short" in alarm["detail"] for alarm in failed))

    def test_discovered_devices_are_monitored_and_component_removal_alarms(self):
        state = sample()
        state["devices"][1].update({
            "management_ip": "10.10.0.2",
            "management_protocol": "snmp",
            "credential_ref": "switch",
            "discovery": {"protocol": "snmp", "checked_at": "now", "details": {}},
        })
        healthy = {
            "system": {"state": "reachable", "health": "ok"},
            "drives": [{"id": "disk-1", "name": "Disk 1", "state": "enabled", "health": "ok"}],
            "power_supplies": [{"id": "psu-1", "name": "PSU 1", "state": "enabled", "health": "ok"}],
            "network_ports": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = RackStore(Path(directory) / "racks.db", [])
            store.save(0, state)
            monitor = DeviceHealthMonitor(store)
            with patch("noc_dashboard.load_profile", return_value={}), patch(
                "noc_dashboard.monitor_device",
                side_effect=[healthy, DiscoveryError("timeout"), DiscoveryError("timeout"), healthy],
            ):
                monitor.check_once()
                self.assertEqual(monitor.snapshot()["switch"]["status"], "online")
                restarted = DeviceHealthMonitor(store)
                self.assertEqual(restarted.snapshot()["switch"]["status"], "online")
                monitor.check_once()
                self.assertEqual(monitor.snapshot()["switch"]["status"], "online")
                monitor.check_once()
                self.assertEqual(monitor.snapshot()["switch"]["status"], "offline")
                self.assertTrue(any(alert["kind"] == "hardware" for alert in build_alerts([], state, monitor.snapshot())))
                monitor.check_once()
                self.assertEqual(monitor.snapshot()["switch"]["status"], "online")

            monitor._record_success(state["devices"][1], healthy, "one")
            missing_components = {**healthy, "drives": [], "power_supplies": []}
            monitor._record_success(state["devices"][1], missing_components, "two")
            alarms = build_alerts([], state, monitor.snapshot())
            self.assertEqual(sum(alert["kind"] == "hardware" for alert in alarms), 2)
            self.assertTrue(any("Drive-Bay 1 device connection failed" in alarm["detail"] for alarm in alarms))
            self.assertTrue(any("PSU-1 device power loss detected" in alarm["detail"] for alarm in alarms))
            unavailable = {
                **healthy,
                "drives": [],
                "power_supplies": [],
                "component_coverage": {"drives": False, "power_supplies": False},
            }
            monitor._record_success(state["devices"][1], unavailable, datetime.now(timezone.utc).isoformat())
            self.assertEqual(sum(alert["kind"] == "hardware" for alert in build_alerts([], state, monitor.snapshot())), 2)
            monitor._record_success(state["devices"][1], healthy, datetime.now(timezone.utc).isoformat())
            recovered = build_alerts([], state, monitor.snapshot())
            self.assertFalse(any(alert["kind"] == "hardware" for alert in recovered))

    def test_alarm_lifecycle_persists_resolution_and_filters_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RackStore(Path(directory) / "racks.db", [])
            start = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
            alarm = {
                "id": "network:switch:1",
                "kind": "network",
                "severity": "error",
                "title": "Switch - Network alarm",
                "detail": "Network port 1 has entered a degraded state.",
            }
            store.sync_alarms([alarm], start.isoformat())
            self.assertEqual(len(store.read_alarm_snapshot(start.isoformat())["alerts"]), 1)
            resolved_at = start + timedelta(seconds=15)
            store.sync_alarms([], resolved_at.isoformat())
            snapshot = store.read_alarm_snapshot((resolved_at + timedelta(seconds=20)).isoformat())
            self.assertEqual(snapshot["alerts"], [])
            self.assertEqual(len(snapshot["resolved_alerts"]), 1)
            history = store.read_alarm_history(
                start.isoformat(), (start + timedelta(days=1)).isoformat(), "network",
            )
            self.assertEqual(history["total"], 1)
            self.assertEqual(history["alarms"][0]["resolved_at"], resolved_at.isoformat())
            expired = store.read_alarm_snapshot((resolved_at + timedelta(seconds=61)).isoformat())
            self.assertEqual(expired["resolved_alerts"], [])
            self.assertEqual(store.read_alarm_history()["total"], 1)
            store.sync_alarms([alarm], (resolved_at + timedelta(seconds=70)).isoformat())
            store.sync_alarms([], (resolved_at + timedelta(seconds=80)).isoformat())
            self.assertEqual(store.read_alarm_history()["total"], 2)

    def test_link_alarm_stays_active_until_a_successful_poll_reports_up(self):
        state = sample()
        device = state["devices"][1]
        with tempfile.TemporaryDirectory() as directory:
            store = RackStore(Path(directory) / "racks.db", [])
            monitor = DeviceHealthMonitor(store)
            now = datetime.now(timezone.utc).isoformat()
            monitor._record_success(device, {
                "system": {"state": "reachable", "health": "ok"},
                "network_ports": [{"id": "1", "name": "Port 1", "link": "down"}],
            }, now)
            self.assertTrue(any(alert["kind"] == "network" for alert in build_alerts([], state, monitor.snapshot())))
            monitor._record_success(device, {
                "system": {"state": "reachable", "health": "ok"},
                "network_ports": [],
                "network_telemetry_unavailable": True,
            }, datetime.now(timezone.utc).isoformat())
            self.assertTrue(any(alert["kind"] == "network" for alert in build_alerts([], state, monitor.snapshot())))
            monitor._record_success(device, {
                "system": {"state": "reachable", "health": "ok"},
                "network_ports": [{"id": "1", "name": "Port 1", "link": "up"}],
            }, datetime.now(timezone.utc).isoformat())
            self.assertFalse(any(alert["kind"] == "network" for alert in build_alerts([], state, monitor.snapshot())))

    def test_concurrent_missed_polls_are_serialized(self):
        state = sample()
        device = state["devices"][1]
        device.update({
            "management_ip": "10.10.0.2",
            "management_protocol": "snmp",
            "credential_ref": "switch",
            "discovery": {"protocol": "snmp", "checked_at": "now", "details": {}},
        })
        with tempfile.TemporaryDirectory() as directory:
            store = RackStore(Path(directory) / "racks.db", [])
            store.save(0, state)
            monitor = DeviceHealthMonitor(store)
            with patch("noc_dashboard.load_profile", return_value={}), patch(
                "noc_dashboard.monitor_device",
                side_effect=[DiscoveryError("timeout"), DiscoveryError("timeout")],
            ):
                workers = [threading.Thread(target=monitor.check_device, args=(device,)) for _ in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()
            self.assertEqual(monitor.snapshot()["switch"]["missed_checks"], 2)
            self.assertEqual(monitor.snapshot()["switch"]["status"], "offline")

    def test_bundled_hardware_catalog_and_model_height(self):
        catalog = load_catalog(Path(__file__).resolve().parents[1] / "hardware_catalog.json")
        self.assertEqual(len(catalog["models"]), 9)
        upright_models = {"dell-r720", "dell-r730", "dellemc-r730"}
        for model in catalog["models"]:
            self.assertEqual(model["rear"]["psu_side"], "right", model["id"])
            for variant in model["variants"]:
                if variant["front_bays"]:
                    expected_orientation = (
                        "vertical"
                        if variant["bay_size"] == "2.5 in" or model["id"] in upright_models
                        else "horizontal"
                    )
                    self.assertEqual(variant["drive_orientation"], expected_orientation, (model["id"], variant["id"]))
                    self.assertIn(variant["drive_rows"], (1, 2), (model["id"], variant["id"]))
        expected_drive_rows = {
            "dell-r720": {"8lff": 1, "16sff": 1},
            "dell-r730": {"8lff": 1, "16sff": 1},
            "dell-emc-r730": {"8lff": 1, "16sff": 1},
            "dell-r720xd": {"12lff": 2, "24sff": 2},
        }
        for model in catalog["models"]:
            for variant_id, rows in expected_drive_rows.get(model["id"], {}).items():
                variant = next(item for item in model["variants"] if item["id"] == variant_id)
                self.assertEqual(variant["drive_rows"], rows, (model["id"], variant_id))
        for model_id in ("dell-r720", "dell-r730", "dellemc-r730"):
            model = next(item for item in catalog["models"] if item["id"] == model_id)
            eight_bay = next(item for item in model["variants"] if item["id"] == "8lff")
            self.assertEqual(eight_bay["drive_slot_width"], 72, model_id)
        r720xd = next(model for model in catalog["models"] if model["id"] == "dell-r720xd")
        self.assertEqual(r720xd["height"], 2)
        hpe = next(model for model in catalog["models"] if model["id"] == "hpe-dl360-gen9")
        self.assertEqual(hpe["front"]["control_side"], "right")
        self.assertEqual(hpe["front"]["control_width"], 170)
        r720 = next(model for model in catalog["models"] if model["id"] == "dell-r720")
        state = sample()
        state["devices"][0].update({"model_id": r720["id"], "variant_id": "8lff"})
        validate(state, {model["id"]: model for model in catalog["models"]})
        state["devices"][0]["height"] = 1
        with self.assertRaisesRegex(ValueError, "rack height"):
            validate(state, {model["id"]: model for model in catalog["models"]})

    def test_persistence_import_and_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "racks.db"
            store = RackStore(path, [{"name": "Existing", "type": "other"}])
            self.assertEqual(store.read()["state"]["devices"][0]["name"], "Existing")
            self.assertIsNone(store.read()["state"]["devices"][0]["rack_id"])
            self.assertEqual(store.save(0, sample()), 1)
            self.assertIsNone(store.save(0, sample()))
            reloaded = RackStore(path, [{"name": "Ignored", "type": "other"}])
            self.assertEqual(reloaded.read(), {"revision": 1, "state": sample()})

    def test_port_settings_and_management_address_persist(self):
        state = sample()
        state["devices"][0].update({
            "management_ip": "10.10.0.25",
            "management_protocol": "redfish",
            "credential_ref": "server-bmc",
            "hardware_details": {
                **empty_hardware_details(),
                "manufacturer": "Dell",
                "serial_number": "ABC123",
                "memory": "256 GiB",
            },
            "port_config": {
                "rear-nic-1": {
                    "label": "Uplink",
                    "mac": "00:11:22:33:44:55",
                    "ip": "10.10.1.25",
                    "vlan": "120",
                    "notes": "Core switch port 12",
                },
            },
        })
        validate(state)
        with tempfile.TemporaryDirectory() as directory:
            store = RackStore(Path(directory) / "racks.db", [])
            self.assertEqual(store.save(0, state), 1)
            saved = store.read()["state"]["devices"][0]
            self.assertEqual(saved["management_ip"], "10.10.0.25")
            self.assertEqual(saved["credential_ref"], "server-bmc")
            self.assertEqual(saved["hardware_details"]["serial_number"], "ABC123")
            self.assertEqual(saved["hardware_details"]["memory"], "256 GiB")
            self.assertEqual(saved["port_config"]["rear-nic-1"]["vlan"], "120")

    def test_existing_inventory_gets_empty_face_layouts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            legacy = {
                "racks": [{"id": "rack", "name": "Rack A", "units": 10}],
                "devices": [{"id": "server", "name": "Server", "type": "hypervisor", "rack_id": "rack", "unit": 1, "height": 2}],
                "cables": [],
            }
            with closing(sqlite3.connect(path)) as db:
                with db:
                    db.execute("CREATE TABLE inventory (id INTEGER PRIMARY KEY, revision INTEGER, data TEXT)")
                    db.execute("INSERT INTO inventory VALUES (1, 4, ?)", (json.dumps(legacy),))
            migrated = RackStore(path, []).read()
            self.assertEqual(migrated["revision"], 4)
            self.assertEqual(migrated["state"]["devices"][0]["front_ports"], [])
            self.assertEqual(migrated["state"]["devices"][0]["rear_ports"], [])
            self.assertEqual(migrated["state"]["devices"][0]["drives"], [])
            self.assertEqual(migrated["state"]["devices"][0]["port_config"], {})
            self.assertEqual(migrated["state"]["devices"][0]["hardware_details"], empty_hardware_details())
            self.assertIsNone(migrated["state"]["devices"][0]["management_ip"])
            self.assertIsNone(migrated["state"]["devices"][0]["discovery"])

    def test_http_access_and_same_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = load_catalog(Path(__file__).resolve().parents[1] / "hardware_catalog.json")
            model_map = {model["id"]: model for model in catalog["models"]}
            store = RackStore(Path(directory) / "racks.db", [], model_map)
            servers = [
                ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Monitor({}), surface, store if surface == "management" else None, catalog))
                for surface in ("status", "management")
            ]
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                public, private = [f"http://127.0.0.1:{server.server_port}" for server in servers]
                body = json.dumps({"revision": 0, "state": sample()}).encode()

                def put(base, origin, revision_body=body):
                    request = urllib.request.Request(base + "/api/racks", data=revision_body, method="PUT",
                        headers={"Origin": origin, "Content-Type": "application/json", "X-Requested-With": "RackEditor"})
                    return urllib.request.urlopen(request)

                for base, origin, status in ((public, public, 404), (private, "https://evil.example", 403)):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        put(base, origin)
                    self.assertEqual(error.exception.code, status)
                with put(private, private) as response:
                    self.assertEqual(json.load(response)["revision"], 1)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    put(private, private)
                self.assertEqual(error.exception.code, 409)
                with urllib.request.urlopen(private + "/api/racks") as response:
                    self.assertEqual(json.load(response)["state"], sample())
                with urllib.request.urlopen(private + "/api/catalog") as response:
                    self.assertEqual(len(json.load(response)["models"]), 9)
                store.sync_alarms([{
                    "id": "network:switch:1", "kind": "network", "severity": "error",
                    "title": "Switch - Network alarm", "detail": "Network port 1 is degraded.",
                }])
                store.sync_alarms([])
                with urllib.request.urlopen(private + "/api/alarm-history?type=network") as response:
                    self.assertEqual(json.load(response)["total"], 1)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(public + "/api/alarm-history")
                self.assertEqual(error.exception.code, 404)
                with urllib.request.urlopen(private + "/rack") as response:
                    self.assertIn(b"Rack designer", response.read())
                with urllib.request.urlopen(private + "/rack.js") as response:
                    self.assertIn(b"drawFront", response.read())
                with urllib.request.urlopen(private + "/login") as response:
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                    login_page = response.read()
                    self.assertIn(b"PRIVATE NETWORK ONLY", login_page)
                    self.assertIn(b'name="httpd_username"', login_page)
                    self.assertIn(b'action="/dologin"', login_page)
                with urllib.request.urlopen(private + "/login.js") as response:
                    self.assertIn(b"query.get(\"failed\")", response.read())
                for path in ("/login", "/login.js"):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(public + path)
                    self.assertEqual(error.exception.code, 404)
                with urllib.request.urlopen(private + "/live.js") as response:
                    self.assertIn(b"scheduleLiveRefresh", response.read())
                with urllib.request.urlopen(public + "/live.js") as response:
                    self.assertIn(b"scheduleLiveRefresh", response.read())
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(public + "/api/racks")
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(public + "/api/catalog")
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(public + "/rack")
                self.assertEqual(error.exception.code, 404)
                device = sample()["devices"][0]
                device.update({
                    "management_ip": "10.0.0.11",
                    "management_protocol": "redfish",
                    "credential_ref": "server-redfish",
                })
                state = sample()
                state["devices"][0] = device
                self.assertEqual(store.save(1, state), 2)
                discover_body = json.dumps({
                    "revision": 2,
                    "protocol": "redfish",
                    "credential_ref": "server-redfish",
                }).encode()
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request = urllib.request.Request(
                        private + "/api/devices/server/discover",
                        data=discover_body,
                        method="POST",
                        headers={"Origin": "https://evil.example", "Content-Type": "application/json",
                                 "X-Requested-With": "RackEditor"},
                    )
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
                request = urllib.request.Request(
                    private + "/api/devices/server/discover",
                    data=discover_body,
                    method="POST",
                    headers={"Origin": private, "Content-Type": "application/json",
                             "X-Requested-With": "RackEditor"},
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 503)
                with patch("noc_dashboard.load_profile", return_value={}), patch(
                    "noc_dashboard.discover",
                    return_value={
                        "details": {"HostName": "server", "Manufacturer": "Dell"},
                        "unavailable": ["Model was not reported by REDFISH."],
                    },
                ):
                    request = urllib.request.Request(
                        private + "/api/devices/server/discover",
                        data=discover_body,
                        method="POST",
                        headers={"Origin": private, "Content-Type": "application/json",
                                 "X-Requested-With": "RackEditor"},
                    )
                    with urllib.request.urlopen(request) as response:
                        result = json.loads(response.read())
                self.assertEqual(result["discovery"]["details"]["HostName"], "server")
                self.assertEqual(result["discovery"]["unavailable"], ["Model was not reported by REDFISH."])
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
