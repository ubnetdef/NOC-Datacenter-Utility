"""Persistent, validated rack inventory for the management surface."""

import json
import ipaddress
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

DEVICE_TYPES = {"hypervisor", "storage", "network", "controller", "other"}
MANAGEMENT_PROTOCOLS = {"redfish", "snmp", "ipmi"}
HARDWARE_DETAIL_FIELDS = {
    "hostname", "manufacturer", "model", "serial_number", "cpu", "memory",
    "storage", "power_supplies", "network_interfaces",
}


def empty_hardware_details():
    return {field: "" for field in HARDWARE_DETAIL_FIELDS}


def validate(state, catalog=None):
    if not isinstance(state, dict) or set(state) != {"racks", "devices", "cables"}:
        raise ValueError("Inventory must contain racks, devices and cables")
    for key in state:
        if not isinstance(state[key], list) or len(state[key]) > 500:
            raise ValueError(f"{key} must be an array of at most 500 entries")
    ids = set()

    def item_id(item):
        if not isinstance(item, dict):
            raise ValueError("Each inventory item must be an object")
        value = item.get("id")
        if not isinstance(value, str) or not value or len(value) > 80 or value in ids:
            raise ValueError("Inventory IDs must be unique nonempty strings")
        ids.add(value)

    def text(item, key, required=True):
        value = item.get(key)
        if not isinstance(value, str) or len(value) > 120 or (required and not value.strip()):
            raise ValueError(f"{key} must be {'a nonempty' if required else 'a'} string of at most 120 characters")

    def integer(value, low, high):
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

    racks = {}
    for rack in state["racks"]:
        item_id(rack)
        text(rack, "name")
        if set(rack) != {"id", "name", "units"} or not integer(rack["units"], 1, 60):
            raise ValueError("Rack height must be between 1 and 60 U")
        racks[rack["id"]] = rack
    devices = {}
    occupied = set()
    for device in state["devices"]:
        item_id(device)
        text(device, "name")
        device.setdefault("hardware_details", empty_hardware_details())
        if set(device) != {"id", "name", "type", "rack_id", "unit", "height",
                           "front_ports", "rear_ports", "drives", "model_id", "variant_id",
                           "port_config", "management_ip", "management_protocol",
                           "credential_ref", "discovery", "hardware_details"}:
            raise ValueError("Invalid device fields")
        hardware_details = device["hardware_details"]
        if (not isinstance(hardware_details, dict)
                or set(hardware_details) != HARDWARE_DETAIL_FIELDS
                or any(not isinstance(value, str) or len(value) > 1000 for value in hardware_details.values())):
            raise ValueError("Hardware details must contain valid text values")
        for key in ("management_ip", "credential_ref"):
            value = device[key]
            if value is not None and (not isinstance(value, str) or len(value) > 120):
                raise ValueError(f"{key} must be a string of at most 120 characters")
        if device["management_ip"]:
            try:
                address = ipaddress.ip_address(device["management_ip"])
            except ValueError as error:
                raise ValueError("Management address must be an IP address") from error
            if (not address.is_private or address.is_loopback or address.is_multicast
                    or address.is_unspecified or address.is_reserved):
                raise ValueError("Management address must be a private-network IP address")
        if device["management_protocol"] not in MANAGEMENT_PROTOCOLS:
            raise ValueError("Unsupported management protocol")
        if device["credential_ref"] is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", device["credential_ref"]):
            raise ValueError("Credential profile must be a 1-64 character name")
        port_config = device["port_config"]
        if not isinstance(port_config, dict) or len(port_config) > 128:
            raise ValueError("Port configuration must be an object with at most 128 entries")
        for key, port in port_config.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", key):
                raise ValueError("Invalid port configuration key")
            if not isinstance(port, dict) or set(port) != {"label", "mac", "ip", "vlan", "notes"}:
                raise ValueError("Invalid port configuration")
            for field in ("label", "mac", "ip", "vlan", "notes"):
                value = port[field]
                if not isinstance(value, str) or len(value) > (500 if field == "notes" else 120):
                    raise ValueError(f"Port {field} must be a string of valid length")
            if port["ip"]:
                try:
                    ipaddress.ip_address(port["ip"])
                except ValueError as error:
                    raise ValueError("Port IP address must be valid") from error
            if port["vlan"] and (not port["vlan"].isdigit() or not 1 <= int(port["vlan"]) <= 4094):
                raise ValueError("Port VLAN must be between 1 and 4094")
        discovery = device["discovery"]
        if discovery is not None:
            if (not isinstance(discovery, dict)
                    or set(discovery) not in (
                        {"protocol", "checked_at", "details"},
                        {"protocol", "checked_at", "details", "unavailable"},
                    )
                    or discovery["protocol"] not in MANAGEMENT_PROTOCOLS
                    or not isinstance(discovery["checked_at"], str)
                    or len(discovery["checked_at"]) > 40
                    or not isinstance(discovery["details"], dict)):
                raise ValueError("Invalid device discovery result")
            unavailable = discovery.get("unavailable", [])
            if (not isinstance(unavailable, list) or len(unavailable) > 32
                    or any(not isinstance(value, str) or not value or len(value) > 240 for value in unavailable)):
                raise ValueError("Discovery gaps must be a bounded list of text values")
        model_id, variant_id = device["model_id"], device["variant_id"]
        if model_id is None:
            if variant_id is not None:
                raise ValueError("A hardware configuration requires a catalog model")
        elif not isinstance(model_id, str) or not isinstance(variant_id, str):
            raise ValueError("Invalid hardware model selection")
        elif catalog is not None:
            model = catalog.get(model_id)
            if model is None or variant_id not in {variant["id"] for variant in model["variants"]}:
                raise ValueError("Hardware model or configuration is not in the catalog")
            if device["height"] != model["height"]:
                raise ValueError("Device rack height must match the selected hardware model")
        if device["type"] not in DEVICE_TYPES or not integer(device["height"], 1, 60):
            raise ValueError("Invalid device type or height")
        for key in ("front_ports", "rear_ports", "drives"):
            labels = device[key]
            if not isinstance(labels, list) or len(labels) > 48:
                raise ValueError(f"{key} must contain at most 48 labels")
            if any(not isinstance(label, str) or not label.strip() or len(label) > 40 for label in labels):
                raise ValueError(f"{key} labels must be nonempty strings of at most 40 characters")
        rack_id, unit = device["rack_id"], device["unit"]
        if rack_id is None:
            if unit is not None:
                raise ValueError("Unassigned devices cannot have a rack position")
        else:
            if rack_id not in racks or not integer(unit, 1, racks[rack_id]["units"]):
                raise ValueError("Invalid rack position")
            if unit + device["height"] - 1 > racks[rack_id]["units"]:
                raise ValueError("Device extends beyond rack height")
            for position in range(unit, unit + device["height"]):
                if (rack_id, position) in occupied:
                    raise ValueError("Devices cannot occupy the same rack units")
                occupied.add((rack_id, position))
        devices[device["id"]] = device
    used_ports = set()
    for cable in state["cables"]:
        item_id(cable)
        if set(cable) != {"id", "device_a", "port_a", "device_b", "port_b", "label"}:
            raise ValueError("Invalid cable fields")
        for key in ("port_a", "port_b", "label"):
            text(cable, key, required=key != "label")
        if cable["device_a"] not in devices or cable["device_b"] not in devices:
            raise ValueError("Cable endpoints must reference existing devices")
        if cable["device_a"] == cable["device_b"] and cable["port_a"] == cable["port_b"]:
            raise ValueError("A cable cannot connect a port to itself")
        for endpoint in ((cable["device_a"], cable["port_a"].strip().casefold()),
                         (cable["device_b"], cable["port_b"].strip().casefold())):
            if endpoint in used_ports:
                raise ValueError("A device port cannot be used by more than one cable")
            used_ports.add(endpoint)
    return state


class RackStore:
    def __init__(self, path, initial_devices, catalog=None):
        self.path = str(path)
        self.catalog = catalog
        state = {"racks": [], "devices": [
            {"id": uuid.uuid4().hex, "name": d["name"], "type": d["type"],
             "rack_id": None, "unit": None, "height": 1,
             "front_ports": [], "rear_ports": [], "drives": [],
             "model_id": None, "variant_id": None, "port_config": {},
             "hardware_details": empty_hardware_details(),
             "management_ip": None, "management_protocol": "redfish",
             "credential_ref": None, "discovery": None}
            for d in initial_devices
        ], "cables": []}
        validate(state, catalog)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, data TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS device_health (device_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS alarm_events ("
                    "event_id INTEGER PRIMARY KEY AUTOINCREMENT, alarm_key TEXT NOT NULL, "
                    "kind TEXT NOT NULL, severity TEXT NOT NULL, "
                    "title TEXT NOT NULL, detail TEXT NOT NULL, first_seen_at TEXT NOT NULL, "
                    "last_seen_at TEXT NOT NULL, resolved_at TEXT)"
                )
                db.execute("CREATE INDEX IF NOT EXISTS alarm_events_history ON alarm_events(resolved_at DESC, kind)")
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS alarm_events_one_active "
                    "ON alarm_events(alarm_key) WHERE resolved_at IS NULL"
                )
                db.execute("INSERT OR IGNORE INTO inventory VALUES (1, 0, ?)", (json.dumps(state),))

    def read(self):
        with closing(sqlite3.connect(self.path)) as db:
            revision, data = db.execute("SELECT revision, data FROM inventory WHERE id=1").fetchone()
        state = json.loads(data)
        # Older rack inventories predate the editable front/rear face layout.
        for device in state.get("devices", []):
            device.setdefault("front_ports", [])
            device.setdefault("rear_ports", [])
            device.setdefault("drives", [])
            device.setdefault("model_id", None)
            device.setdefault("variant_id", None)
            device.setdefault("port_config", {})
            device.setdefault("management_ip", None)
            device.setdefault("management_protocol", "redfish")
            device.setdefault("credential_ref", None)
            device.setdefault("discovery", None)
            device.setdefault("hardware_details", empty_hardware_details())
            for port in device["port_config"].values():
                port.pop("link_state", None)
        return {"revision": revision, "state": validate(state, getattr(self, "catalog", None))}

    def save(self, revision, state):
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("Invalid revision")
        validate(state, getattr(self, "catalog", None))
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                result = db.execute(
                    "UPDATE inventory SET data=?, revision=revision+1 WHERE id=1 AND revision=?",
                    (json.dumps(state), revision),
                )
                if result.rowcount != 1:
                    return None
        return revision + 1

    def read_health(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT device_id, data FROM device_health").fetchall()
        return {device_id: json.loads(data) for device_id, data in rows}

    def save_health(self, device_id, state):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute(
                    "INSERT INTO device_health (device_id, data) VALUES (?, ?) "
                    "ON CONFLICT(device_id) DO UPDATE SET data=excluded.data",
                    (device_id, json.dumps(state)),
                )

    def prune_health(self, device_ids):
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                if device_ids:
                    placeholders = ",".join("?" for _ in device_ids)
                    db.execute(f"DELETE FROM device_health WHERE device_id NOT IN ({placeholders})", tuple(device_ids))
                else:
                    db.execute("DELETE FROM device_health")

    def sync_alarms(self, alerts, now=None):
        timestamp = now or datetime.now(timezone.utc).isoformat()
        active_ids = set()
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                previous_ids = {
                    row[0] for row in db.execute("SELECT alarm_key FROM alarm_events WHERE resolved_at IS NULL")
                }
                for alert in alerts:
                    alarm_id = alert["id"]
                    active_ids.add(alarm_id)
                    values = (
                        alert["kind"], alert["severity"], alert["title"][:240],
                        alert["detail"][:1000], timestamp, alarm_id,
                    )
                    existing = db.execute(
                        "SELECT event_id FROM alarm_events WHERE alarm_key=? AND resolved_at IS NULL",
                        (alarm_id,),
                    ).fetchone()
                    if existing:
                        db.execute(
                            "UPDATE alarm_events SET kind=?, severity=?, title=?, detail=?, last_seen_at=? "
                            "WHERE event_id=?",
                            (*values[:-1], existing[0]),
                        )
                    else:
                        db.execute(
                            "INSERT INTO alarm_events "
                            "(alarm_key, kind, severity, title, detail, first_seen_at, last_seen_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (alarm_id, *values[:-1], timestamp),
                        )
                for alarm_id in previous_ids - active_ids:
                    db.execute(
                        "UPDATE alarm_events SET resolved_at=? WHERE alarm_key=? AND resolved_at IS NULL",
                        (timestamp, alarm_id),
                    )

    def read_alarm_snapshot(self, now=None):
        timestamp = now or datetime.now(timezone.utc).isoformat()
        cutoff = (datetime.fromisoformat(timestamp) - timedelta(seconds=60)).isoformat()
        fields = "event_id, alarm_key AS id, kind, severity, title, detail, first_seen_at, last_seen_at, resolved_at"
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            active = db.execute(
                f"SELECT {fields} FROM alarm_events WHERE resolved_at IS NULL "
                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 ELSE 2 END, first_seen_at"
            ).fetchall()
            recently_resolved = db.execute(
                f"SELECT {fields} FROM alarm_events WHERE resolved_at >= ? "
                "ORDER BY resolved_at DESC",
                (cutoff,),
            ).fetchall()
        return {
            "alerts": [dict(row) for row in active],
            "resolved_alerts": [dict(row) for row in recently_resolved],
            "resolved_auto_clear_seconds": 60,
        }

    def read_alarm_history(self, start=None, end=None, kind=None, limit=100, offset=0):
        clauses = ["resolved_at IS NOT NULL"]
        params = []
        if start is not None:
            clauses.append("resolved_at >= ?")
            params.append(start)
        if end is not None:
            clauses.append("resolved_at < ?")
            params.append(end)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        fields = "event_id, alarm_key AS id, kind, severity, title, detail, first_seen_at, last_seen_at, resolved_at"
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            total = db.execute(
                f"SELECT COUNT(*) FROM alarm_events WHERE {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT {fields} FROM alarm_events WHERE {where} "
                "ORDER BY resolved_at DESC, first_seen_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"alarms": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}
