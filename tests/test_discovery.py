import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discovery


class DiscoveryTests(unittest.TestCase):
    def test_profile_is_loaded_from_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"profiles": {
                "server": {"protocol": "redfish", "username": "reader", "password": "secret"},
            }}), encoding="utf-8")
            profile = discovery.load_profile("server", "redfish", path)
        self.assertEqual(profile["username"], "reader")
        with self.assertRaises(discovery.DiscoveryError):
            discovery.load_profile("missing", "redfish", path)

    def test_discovery_refuses_public_targets(self):
        with self.assertRaisesRegex(discovery.DiscoveryError, "private-network"):
            discovery.discover("8.8.8.8", "snmp", {"community": "readonly"})

    def test_discovery_returns_reported_data_and_actionable_gaps(self):
        redfish_result = {
            "HostName": "rack-server",
            "Manufacturer": "Dell",
            "Model": "PowerEdge R720",
            "DiscoveryWarnings": ["Drive inventory: Redfish endpoint unavailable."],
        }
        with patch("discovery._discover_redfish", return_value=redfish_result):
            result = discovery.discover("10.0.0.20", "redfish", {"username": "reader"})
        self.assertEqual(result["details"]["HostName"], "rack-server")
        self.assertNotIn("DiscoveryWarnings", result["details"])
        self.assertIn("Serial number was not reported by REDFISH.", result["unavailable"])
        self.assertIn("Drive inventory: Redfish endpoint unavailable.", result["unavailable"])

    def test_empty_component_lists_mean_the_inventory_was_checked(self):
        redfish_result = {
            "HostName": "rack-server",
            "Manufacturer": "Dell",
            "Model": "PowerEdge R720",
            "SerialNumber": "ABC123",
            "ProcessorSummary": {"Count": 2, "Model": "CPU"},
            "MemorySummary": {"TotalSystemMemoryGiB": 256},
            "Drives": [],
            "PowerSupplies": [],
            "NetworkInterfaces": [],
        }
        with patch("discovery._discover_redfish", return_value=redfish_result):
            result = discovery.discover("10.0.0.20", "redfish", {"username": "reader"})
        self.assertEqual(result["unavailable"], [])

    def test_redfish_reads_only_bounded_system_inventory(self):
        resources = [
            {"Systems": {"@odata.id": "/redfish/v1/Systems"}},
            {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
            {
                "Manufacturer": "Example",
                "Model": "Rack Server",
                "SerialNumber": "ABC123",
                "ProcessorSummary": {"Count": 2, "Model": "CPU"},
                "MemorySummary": {"TotalSystemMemoryGiB": 256},
                "PowerState": "On",
            },
        ]

        class Response:
            status = 200

            def __init__(self, value):
                self.body = json.dumps(value).encode()

            def getheader(self, name, default=None):
                return str(len(self.body)) if name == "Content-Length" else default

            def read(self, size=-1):
                return self.body[:size]

            def close(self):
                pass

        class Connection:
            def __init__(self, *args, **kwargs):
                self.paths = []

            def request(self, method, path, headers):
                self.paths.append(path)
                self.authorization = headers["Authorization"]

            def getresponse(self):
                return Response(resources.pop(0))

            def close(self):
                pass

        with patch("discovery.http.client.HTTPSConnection", Connection):
            result = discovery._discover_redfish(
                discovery._management_address("10.0.0.20"),
                {"username": "readonly", "password": "secret"},
            )
        self.assertEqual(result["Manufacturer"], "Example")
        self.assertEqual(result["SerialNumber"], "ABC123")
        self.assertEqual(result["MemorySummary"]["TotalSystemMemoryGiB"], 256)

    def test_redfish_health_poll_reads_system_psu_drive_and_link_status(self):
        resources = {
            "/redfish/v1/": {
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
                "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
            },
            "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
            "/redfish/v1/Systems/1": {
                "PowerState": "On",
                "Status": {"Health": "OK"},
                "Storage": {"@odata.id": "/redfish/v1/Systems/1/Storage"},
                "EthernetInterfaces": {"@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces"},
            },
            "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
            "/redfish/v1/Chassis/1": {"Power": {"@odata.id": "/redfish/v1/Chassis/1/Power"}},
            "/redfish/v1/Chassis/1/Power": {
                "PowerSupplies": [{"Name": "PSU 1", "State": "Enabled", "Status": {"Health": "OK"}}],
            },
            "/redfish/v1/Systems/1/Storage": {"Members": [{"@odata.id": "/redfish/v1/Storage/1"}]},
            "/redfish/v1/Storage/1": {"Drives": {"@odata.id": "/redfish/v1/Storage/1/Drives"}},
            "/redfish/v1/Storage/1/Drives": {"Members": [{"@odata.id": "/redfish/v1/Drives/1"}]},
            "/redfish/v1/Drives/1": {
                "Name": "Drive Bay 1", "State": "Enabled", "Status": {"Health": "OK"},
            },
            "/redfish/v1/Systems/1/EthernetInterfaces": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces/1"}],
            },
            "/redfish/v1/Systems/1/EthernetInterfaces/1": {
                "Name": "Ethernet 1", "LinkStatus": "LinkUp", "MACAddress": "00:11:22:33:44:55",
            },
        }

        class Response:
            status = 200

            def __init__(self, value):
                self.body = json.dumps(value).encode()

            def getheader(self, name, default=None):
                return str(len(self.body)) if name == "Content-Length" else default

            def read(self, size=-1):
                return self.body[:size]

            def close(self):
                pass

        class Connection:
            def __init__(self, *args, **kwargs):
                self.path = None

            def request(self, method, path, headers):
                self.path = path

            def getresponse(self):
                return Response(resources[self.path])

            def close(self):
                pass

        with patch("discovery.http.client.HTTPSConnection", Connection):
            result = discovery._monitor_redfish(
                discovery._management_address("10.0.0.20"),
                {"username": "readonly", "password": "secret"},
            )
        self.assertEqual(result["system"]["state"], "On")
        self.assertTrue(result["component_coverage"]["drives"])
        self.assertTrue(result["component_coverage"]["power_supplies"])
        self.assertEqual(result["drives"][0]["health"], "OK")
        self.assertEqual(result["power_supplies"][0]["name"], "PSU 1")
        self.assertEqual(result["network_ports"][0]["link"], "LinkUp")

    def test_snmp_queries_only_standard_system_oids(self):
        class Socket:
            def __init__(self, *args):
                self.target = None
                self.packet = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, value):
                self.timeout = value

            def connect(self, target):
                self.target = target

            def send(self, packet):
                self.packet = packet
                _, message, _ = discovery._read_tlv(packet, 0)
                fields = discovery._read_children(message)
                if fields[2][0] != 0xa0:
                    raise AssertionError("SNMP request did not use a GET PDU")
                request_fields = discovery._read_children(fields[2][1])
                self.request_id = int.from_bytes(request_fields[0][1], "big", signed=True)

            def recv(self, size):
                values = [
                    ("1.3.6.1.2.1.1.1.0", "Switch OS"),
                    ("1.3.6.1.2.1.1.2.0", "1.3.6.1.4.1.674"),
                    ("1.3.6.1.2.1.1.5.0", "core-switch"),
                ]
                bindings = b"".join(
                    discovery._tlv(0x30, discovery._ber_oid(oid) + discovery._tlv(0x04, value.encode()))
                    for oid, value in values
                )
                varbinds = discovery._tlv(0x30, bindings)
                pdu = discovery._tlv(
                    0xa2,
                    discovery._ber_integer(self.request_id)
                    + discovery._ber_integer(0)
                    + discovery._ber_integer(0)
                    + varbinds,
                )
                return discovery._tlv(
                    0x30,
                    discovery._ber_integer(1) + discovery._tlv(0x04, b"readonly") + pdu,
                )

        client = Socket()
        with patch("discovery.socket.socket", return_value=client):
            result = discovery._discover_snmp(
                discovery._management_address("10.0.0.30"),
                {"community": "readonly"},
            )
        self.assertEqual(client.target, ("10.0.0.30", 161))
        self.assertEqual(client.timeout, 3)
        self.assertEqual(result["System description"], "Switch OS")
        self.assertEqual(result["System name"], "core-switch")

    def test_snmp_health_poll_uses_bounded_read_only_getbulk(self):
        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def settimeout(self, value):
                self.timeout = value

            def connect(self, target):
                self.target = target

            def send(self, packet):
                _, message, _ = discovery._read_tlv(packet, 0)
                fields = discovery._read_children(message)
                self.assertion = fields[2][0] == 0xa5
                self.request_id = int.from_bytes(discovery._read_children(fields[2][1])[0][1], "big", signed=True)

            def recv(self, size):
                binding = discovery._tlv(
                    0x30,
                    discovery._ber_oid("1.3.6.1.2.1.2.2.1.8.1")
                    + discovery._tlv(0x02, b"\x02"),
                )
                varbinds = discovery._tlv(0x30, binding)
                pdu = discovery._tlv(
                    0xa2,
                    discovery._ber_integer(self.request_id)
                    + discovery._ber_integer(0)
                    + discovery._ber_integer(0)
                    + varbinds,
                )
                return discovery._tlv(
                    0x30,
                    discovery._ber_integer(1) + discovery._tlv(0x04, b"readonly") + pdu,
                )

        client = Socket()
        with patch("discovery.socket.socket", return_value=client):
            values = discovery._snmp_get_bulk(
                discovery._management_address("10.0.0.30"),
                "readonly",
                "1.3.6.1.2.1.2.2.1.8",
                17,
            )
        self.assertTrue(client.assertion)
        self.assertEqual(client.target, ("10.0.0.30", 161))
        self.assertEqual(values, [("1.3.6.1.2.1.2.2.1.8.1", 2)])

    def test_ipmi_password_is_not_in_process_arguments(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="Device ID : 32\nFirmware Revision : 2.1\n",
        )
        with patch("discovery.subprocess.run", return_value=completed) as run:
            result = discovery._discover_ipmi(
                discovery._management_address("10.0.0.40"),
                {"username": "reader", "password": "not-an-argument"},
            )
        arguments = run.call_args.args[0]
        self.assertNotIn("not-an-argument", arguments)
        self.assertEqual(run.call_args.kwargs["env"]["IPMI_PASSWORD"], "not-an-argument")
        self.assertEqual(result["Device ID"], "32")

    def test_ipmi_sensor_health_is_read_without_exposing_password(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                "PS1 Status | 0x00 | discrete | ok | 0x00 | 0x00\n"
                "Drive Bay 1 Status | 0x00 | discrete | cr | 0x00 | 0x00\n"
                "CPU Usage | 95 | percent | unc | 0 | 0\n"
            ),
        )
        with patch("discovery.subprocess.run", return_value=completed) as run:
            supplies, drives, resources = discovery._monitor_ipmi_sensors(
                discovery._management_address("10.0.0.40"),
                {"username": "reader", "password": "not-an-argument"},
            )
        self.assertNotIn("not-an-argument", run.call_args.args[0])
        self.assertEqual(supplies[0]["health"], "ok")
        self.assertEqual(drives[0]["health"], "critical")
        self.assertTrue(resources[0]["threshold_exceeded"])
        self.assertEqual(resources[0]["name"], "CPU")


if __name__ == "__main__":
    unittest.main()
