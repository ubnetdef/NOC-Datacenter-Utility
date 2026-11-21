#!/usr/bin/env python3
"""Loopback-only status and management dashboards."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import http.client
from http.cookies import CookieError, SimpleCookie
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from device_catalog import load_catalog
from discovery import DiscoveryError, discover, load_profile, monitor_device
from rack_store import RackStore


ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("noc")
DEVICE_TYPES = {"hypervisor", "storage", "network", "controller", "other"}


def _within_seconds(timestamp, seconds):
    try:
        recorded = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return False
    if recorded.tzinfo is None:
        return False
    age = (datetime.now(timezone.utc) - recorded).total_seconds()
    return 0 <= age <= seconds


def load_config(path):
    with open(path, encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("configuration must be a JSON object")
    interval = config.get("check_interval_seconds", 60)
    timeout = config.get("check_timeout_seconds", 5)
    device_interval = config.get("device_check_interval_seconds", 30)
    for name, value in (("check_interval_seconds", interval), ("check_timeout_seconds", timeout)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 3600:
            raise ValueError(f"{name} must be an integer between 1 and 3600")
    if not isinstance(device_interval, int) or isinstance(device_interval, bool) or not 10 <= device_interval <= 3600:
        raise ValueError("device_check_interval_seconds must be an integer between 10 and 3600")
    services = config.get("services", [])
    devices = config.get("devices", [])
    if not isinstance(services, list) or not isinstance(devices, list):
        raise ValueError("services and devices must be arrays")
    names = set()
    for service in services:
        if not isinstance(service, dict):
            raise ValueError("each service must be an object")
        name, url = service.get("name"), service.get("url")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("service names must be nonempty and unique")
        if not isinstance(url, str):
            raise ValueError(f"service {name} must have a URL")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.username or parsed.password or any(ord(char) < 33 for char in url)):
            raise ValueError(f"service {name} must have an HTTP(S) URL without credentials")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError(f"service {name} has an invalid URL port") from error
        names.add(name)
    for device in devices:
        if not isinstance(device, dict) or not isinstance(device.get("name"), str) or not device["name"].strip():
            raise ValueError("each device must have a nonempty name")
        if device.get("type") not in DEVICE_TYPES:
            raise ValueError(f"device {device['name']} has an unsupported type")
    return config


def check_website(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "NOC-Status-Monitor/1.0"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 400:
                return "operational", round((time.monotonic() - started) * 1000)
            return "outage", None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
        LOG.warning("Website check failed: %s", error)
        return "outage", None


class Monitor:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.services = [
            {"name": service["name"], "status": "unknown", "checked_at": None, "latency_ms": None}
            for service in config.get("services", [])
        ]

    def check_once(self):
        for index, service in enumerate(self.config.get("services", [])):
            status, latency = check_website(service["url"], self.config.get("check_timeout_seconds", 5))
            result = {
                "name": service["name"],
                "status": status,
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "latency_ms": latency,
            }
            with self.lock:
                self.services[index] = result

    def run(self):
        while True:
            self.check_once()
            time.sleep(self.config.get("check_interval_seconds", 60))

    def snapshot(self):
        with self.lock:
            return [service.copy() for service in self.services]


class DeviceHealthMonitor:
    def __init__(self, rack_store, interval=30, credential_file=None):
        self.rack_store = rack_store
        self.interval = interval
        self.credential_file = credential_file
        self.lock = threading.Lock()
        self.device_locks = {}
        self.states = rack_store.read_health()

    def snapshot(self, device_ids=None):
        with self.lock:
            return {
                device_id: json.loads(json.dumps(state))
                for device_id, state in self.states.items()
                if device_ids is None or device_id in device_ids
            }

    def _poll(self, device):
        profile = load_profile(device["credential_ref"], device["management_protocol"], self.credential_file)
        return monitor_device(device["management_ip"], device["management_protocol"], profile)

    def _record_success(self, device, telemetry, checked_at):
        previous = self.states.get(device["id"], {})
        if (telemetry.get("network_telemetry_unavailable")
                and previous.get("telemetry", {}).get("network_ports")):
            telemetry["network_ports"] = previous["telemetry"]["network_ports"]
            telemetry["network_telemetry_stale"] = True
        expected = previous.get("expected_components")
        if not isinstance(expected, dict):
            expected = {"drives": [], "power_supplies": []}
        component_names = previous.get("component_names")
        if not isinstance(component_names, dict):
            component_names = {"drives": {}, "power_supplies": {}}
        missing = {}
        observed_components = {}
        coverage = telemetry.get("component_coverage", {})
        for component_type in ("drives", "power_supplies"):
            is_covered = coverage.get(component_type, component_type in telemetry)
            if not is_covered:
                if previous.get("missing_components", {}).get(component_type):
                    missing[component_type] = previous["missing_components"][component_type]
                continue
            observed = telemetry.get(component_type, [])
            observed_components[component_type] = {
                item["id"]: item.get("name", item["id"])
                for item in observed if isinstance(item, dict) and item.get("id")
            }
            known_names = component_names.setdefault(component_type, {})
            known_names.update(observed_components[component_type])
            observed_ids = set(observed_components[component_type])
            expected_ids = set(expected.get(component_type, []))
            expected_ids.update(observed_ids)
            absent = sorted(expected_ids - observed_ids)
            if absent:
                missing[component_type] = absent
            expected[component_type] = sorted(expected_ids)
        recovered = []
        for component_type, previous_ids in previous.get("missing_components", {}).items():
            for component_id in previous_ids:
                if component_id in observed_components.get(component_type, {}):
                    recovered.append({
                        "type": "drive" if component_type == "drives" else "power supply",
                        "id": component_id,
                        "name": observed_components[component_type][component_id],
                    })
        recovery_events = [
            item for item in previous.get("recovery_events", [])
            if _within_seconds(item.get("recovered_at"), 60)
        ]
        recovery_events.extend({**item, "recovered_at": checked_at} for item in recovered)
        state = {
            "device_id": device["id"],
            "device_name": device["name"],
            "status": "online",
            "missed_checks": 0,
            "checked_at": checked_at,
            "last_success": checked_at,
            "telemetry": telemetry,
            "expected_components": expected,
            "component_names": component_names,
            "missing_components": missing,
            "recovery_events": recovery_events,
            "last_error": None,
        }
        self._save_state(device["id"], state)

    def _record_failure(self, device, error, checked_at):
        previous = self.states.get(device["id"], {})
        missed = previous.get("missed_checks", 0) + 1
        state = {
            **previous,
            "device_id": device["id"],
            "device_name": device["name"],
            "status": "offline" if missed >= 2 else previous.get("status", "unknown"),
            "missed_checks": missed,
            "checked_at": checked_at,
            "last_error": str(error)[:200],
        }
        self._save_state(device["id"], state)

    def _save_state(self, device_id, state):
        self.rack_store.save_health(device_id, state)
        with self.lock:
            self.states[device_id] = state

    def reconcile(self, inventory_state):
        devices = [
            device for device in inventory_state["devices"]
            if device["discovery"] is not None and device["management_ip"] and device["credential_ref"]
        ]
        active_ids = {device["id"] for device in devices}
        self.rack_store.prune_health(active_ids)
        with self.lock:
            self.states = {key: value for key, value in self.states.items() if key in active_ids}
        return devices

    def check_device(self, device):
        with self.lock:
            device_lock = self.device_locks.setdefault(device["id"], threading.Lock())
        with device_lock:
            state = self.rack_store.read()["state"]
            current = next((item for item in state["devices"] if item["id"] == device["id"]), None)
            if (current is None or current["discovery"] is None
                    or not current["management_ip"] or not current["credential_ref"]):
                self.reconcile(state)
                return
            device = current
            checked_at = datetime.now(timezone.utc).isoformat()
            try:
                self._record_success(device, self._poll(device), checked_at)
            except DiscoveryError as error:
                LOG.warning("Device health check failed for %s: %s", device["name"], error)
                self._record_failure(device, error, checked_at)

    def enroll(self, device):
        threading.Thread(target=self.check_device, args=(device,), daemon=True).start()

    def check_once(self):
        inventory = self.rack_store.read()["state"]
        devices = self.reconcile(inventory)
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(devices)))) as pool:
            futures = {pool.submit(self.check_device, device): device for device in devices}
            for future in as_completed(futures):
                future.result()

    def run(self):
        while True:
            started = time.monotonic()
            try:
                self.check_once()
            except (OSError, sqlite3.Error, ValueError):
                LOG.exception("Device health polling cycle failed")
            time.sleep(max(0, self.interval - (time.monotonic() - started)))


def build_alerts(services, inventory=None, health_states=None):
    alerts = []

    def component_label(name, component_type):
        if component_type == "power_supplies":
            number = re.search(r"(?:psu|ps|power supply)\s*[-#]?\s*(\d+)", name, re.IGNORECASE)
            return f"PSU-{number.group(1)}" if number else name
        number = re.fullmatch(
            r"(?:drive[\s_-]*(?:bay[\s_-]*)?|disk[\s_-]*|bay[\s_-]*)(\d+)(?:\s+status)?",
            name, re.IGNORECASE,
        )
        return f"Drive-Bay {number.group(1)}" if number else re.sub(
            r"drive[\s_-]*bay", "Drive-Bay", name, flags=re.IGNORECASE,
        )

    def add_alarm(alarm_id, kind, severity, hostname, detail):
        labels = {
            "network": "Network alarm",
            "hardware": "Hardware alarm",
            "resource": "Resource-Usage alarm",
            "service": "Service alarm",
        }
        alerts.append({
            "id": alarm_id,
            "severity": severity,
            "kind": kind,
            "title": f"{hostname} - {labels[kind]}",
            "detail": detail,
        })

    for service in services:
        if service["status"] == "outage":
            add_alarm(f"service:{service['name']}", "service", "critical", service["name"],
                      "The automated website check is failing.")
    health_states = health_states or {}
    if inventory:
        devices = {device["id"]: device for device in inventory["devices"]}
        connected_ports = {}
        for cable in inventory["cables"]:
            connected_ports.setdefault(cable["device_a"], set()).add(cable["port_a"].strip().casefold())
            connected_ports.setdefault(cable["device_b"], set()).add(cable["port_b"].strip().casefold())
        for device_id, state in health_states.items():
            device = devices.get(device_id)
            if device is None:
                continue
            if state.get("status") == "offline":
                add_alarm(f"hardware:{device_id}:offline", "hardware", "critical", device["name"],
                          "The management interface missed two consecutive health checks.")
            telemetry = state.get("telemetry", {})
            system = telemetry.get("system", {})
            if system.get("state", "").casefold() in ("off", "poweroff", "poweredoff"):
                add_alarm(f"hardware:{device_id}:power", "hardware", "critical", device["name"],
                          "The device reports that system power is off.")
            system_health = system.get("health", "").casefold()
            if system_health in ("warning", "critical", "failed"):
                add_alarm(
                    f"hardware:{device_id}:health", "hardware",
                    "critical" if system_health in ("critical", "failed") else "error",
                    device["name"], f"System hardware has entered a degraded state (reported health: {system.get('health')}).",
                )
            for resource in telemetry.get("resources", []):
                health = str(resource.get("health", "")).casefold()
                if not resource.get("threshold_exceeded") and health not in ("warning", "critical", "failed"):
                    continue
                resource_id = str(resource.get("id", "resource"))
                resource_name = str(resource.get("name", "resource"))
                severity = "critical" if health in ("critical", "failed") else "error"
                add_alarm(
                    f"resource:{device_id}:{resource_id}", "resource", severity, device["name"],
                    f"High {resource_name} threshold counter detected"
                    + (f" (reported health: {resource.get('health')})." if resource.get("health") else "."),
                )
            for component_type in ("drives", "power_supplies"):
                for component in telemetry.get(component_type, []):
                    health = component.get("health", "").casefold()
                    component_state = component.get("state", "").casefold()
                    if health in ("warning", "critical", "failed") or component_state in ("absent", "disabled"):
                        component_id = str(component["id"])
                        label = component_label(str(component.get("name", component_id)), component_type)
                        detail = (
                            f"{label} device power loss detected."
                            if component_type == "power_supplies" and component_state in ("absent", "disabled")
                            else f"{label} device connection failed (check BIOS)."
                            if component_type == "drives" and component_state in ("absent", "disabled")
                            else f"{label} has entered a degraded state."
                        )
                        add_alarm(
                            f"hardware:{device_id}:{component_type}:{component_id}",
                            "hardware",
                            "critical" if health in ("critical", "failed") or component_state in ("absent", "disabled") else "error",
                            device["name"], detail,
                        )
                for component_id in state.get("missing_components", {}).get(component_type, []):
                    label = component_label(
                        state.get("component_names", {}).get(component_type, {}).get(component_id, component_id),
                        component_type,
                    )
                    if component_type == "power_supplies":
                        detail = f"{label} device power loss detected."
                    else:
                        detail = f"{label} device connection failed (check BIOS)."
                    add_alarm(
                        f"hardware:{device_id}:{component_type}:{component_id}",
                        "hardware", "critical", device["name"], detail,
                    )
            attached = connected_ports.get(device_id, set())
            for port in telemetry.get("network_ports", []):
                identifiers = {
                    str(port.get(key, "")).strip().casefold()
                    for key in ("id", "name", "mac")
                    if port.get(key)
                }
                for key in ("id", "name"):
                    value = str(port.get(key, "")).strip().casefold()
                    if value:
                        identifiers.add(value.rsplit("/", 1)[-1])
                        identifiers.add(value.rsplit("-", 1)[-1])
                if not attached.intersection(identifiers):
                    continue
                link = str(port.get("link", "")).casefold()
                port_health = str(port.get("health", "")).casefold()
                port_id = str(port.get("id", port.get("name", "port")))
                port_name = str(port.get("name", port_id))
                number = re.fullmatch(r"(?:ethernet|interface|port)?\s*[-# ]*(\d+)", port_name, re.IGNORECASE)
                if number:
                    port_name = number.group(1)
                failed = link in ("down", "nolink", "lowerlayerdown", "notpresent", "dormant")
                degraded = port_health in ("warning", "critical", "failed")
                if failed or degraded:
                    detail = (
                        f"Network port {port_name} connection failed; inspect the cable/SFP for a possible short."
                        if failed else f"Network port {port_name} has entered a degraded state."
                    )
                    add_alarm(
                        f"network:{device_id}:{port_id}", "network",
                        "critical" if failed or port_health in ("critical", "failed") else "error",
                        device["name"], detail,
                    )
    return alerts


def record_alarm_state(monitor, rack_store, health_monitor):
    while True:
        try:
            inventory = rack_store.read()["state"]
            device_ids = {device["id"] for device in inventory["devices"]}
            health = health_monitor.snapshot(device_ids) if health_monitor else {}
            rack_store.sync_alarms(build_alerts(monitor.snapshot(), inventory, health))
        except (OSError, sqlite3.Error, ValueError):
            LOG.exception("Unable to record alarm state")
        time.sleep(5)


def make_handler(monitor, surface, rack_store=None, catalog=None, credential_file=None, health_monitor=None):
    test_auth_enabled = surface == "management" and os.environ.get("NOC_TEST_AUTH") == "1"
    test_username = os.environ.get("NOC_TEST_AUTH_USERNAME", "")
    test_password = os.environ.get("NOC_TEST_AUTH_PASSWORD", "")
    if test_auth_enabled and (not test_username or not test_password):
        raise ValueError("NOC_TEST_AUTH requires NOC_TEST_AUTH_USERNAME and NOC_TEST_AUTH_PASSWORD")
    test_session_cookie = "noc_test_session"
    test_session_ttl = 28800
    test_sessions = {}
    test_sessions_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def test_session_id(self):
            if not test_auth_enabled:
                return None
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
            except CookieError:
                return None
            morsel = cookies.get(test_session_cookie)
            session_id = morsel.value if morsel else ""
            if not session_id:
                return None
            with test_sessions_lock:
                expires_at = test_sessions.get(session_id)
                if expires_at is None:
                    return None
                if expires_at <= time.time():
                    test_sessions.pop(session_id, None)
                    return None
            return session_id

        def require_test_session(self, path):
            if not test_auth_enabled or self.test_session_id():
                return True
            if path in ("/login", "/login.js", "/style.css"):
                return True
            if path.startswith("/api/"):
                self.json_response({"error": "Authentication required"}, 401)
            else:
                self.redirect("/login")
            return False

        def test_cookie_header(self, value, max_age):
            cookies = SimpleCookie()
            cookies[test_session_cookie] = value
            morsel = cookies[test_session_cookie]
            morsel["path"] = "/"
            morsel["httponly"] = True
            morsel["samesite"] = "Strict"
            morsel["max-age"] = str(max_age)
            return morsel.OutputString()

        def redirect(self, location, cookie=None):
            self.send_response(303)
            self.send_header("Location", location)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def test_login(self):
            origin_text = self.headers.get("Origin", "")
            origin = urllib.parse.urlsplit(origin_text)
            host = self.headers.get("Host", "")
            same_origin = (
                origin.scheme in ("http", "https")
                and origin.netloc.lower() == host.lower()
                and not (origin.path or origin.query or origin.fragment)
            )
            if origin_text == "null":
                same_origin = (
                    self.headers.get("Sec-Fetch-Site") == "same-origin"
                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                )
            if self.headers.get_content_type() != "application/x-www-form-urlencoded" or not same_origin:
                self.respond(403, b"Same-origin form required\n", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= 8192:
                    raise ValueError("Login form is out of range")
                values = urllib.parse.parse_qs(
                    self.rfile.read(length).decode("utf-8"),
                    keep_blank_values=True,
                    max_num_fields=8,
                )
                username_values = values.get("httpd_username", [])
                password_values = values.get("httpd_password", [])
                if len(username_values) != 1 or len(password_values) != 1:
                    raise ValueError("Login form is invalid")
                username = username_values[0].encode("utf-8")
                password = password_values[0].encode("utf-8")
                valid_username = hmac.compare_digest(username, test_username.encode("utf-8"))
                valid_password = hmac.compare_digest(password, test_password.encode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                valid_username = valid_password = False

            if not valid_username or not valid_password:
                self.redirect("/login?failed=1")
                return

            session_id = secrets.token_urlsafe(32)
            with test_sessions_lock:
                now = time.time()
                for session, expires_at in list(test_sessions.items()):
                    if expires_at <= now:
                        test_sessions.pop(session, None)
                test_sessions[session_id] = now + test_session_ttl
            self.redirect("/", self.test_cookie_header(session_id, test_session_ttl))

        def do_GET(self):
            parsed_path = urllib.parse.urlsplit(self.path)
            path = parsed_path.path
            if test_auth_enabled and path == "/logout":
                session_id = self.test_session_id()
                if session_id:
                    with test_sessions_lock:
                        test_sessions.pop(session_id, None)
                self.redirect("/login?logged_out=1", self.test_cookie_header("", 0))
                return
            if not self.require_test_session(path):
                return
            if path == "/api/status":
                self.json_response({"services": monitor.snapshot()})
            elif path == "/api/alerts":
                if surface == "management" and rack_store is not None:
                    try:
                        self.json_response(rack_store.read_alarm_snapshot())
                    except sqlite3.Error:
                        LOG.exception("Unable to read alarm state")
                        self.json_response({"error": "Unable to read alarm state"}, 500)
                else:
                    self.json_response({
                        "alerts": build_alerts(monitor.snapshot()),
                        "resolved_alerts": [],
                    })
            elif surface == "management" and path == "/api/alarm-history" and rack_store is not None:
                try:
                    query = urllib.parse.parse_qs(parsed_path.query, keep_blank_values=True, max_num_fields=8)
                    if set(query) - {"from", "to", "type", "limit", "offset"}:
                        raise ValueError("Unsupported alarm history filter")

                    def query_date(name, inclusive_end=False):
                        value = query.get(name, [""])[0]
                        if not value:
                            return None
                        parsed = date.fromisoformat(value)
                        if parsed.isoformat() != value:
                            raise ValueError(f"{name} must use YYYY-MM-DD format")
                        if inclusive_end:
                            parsed += timedelta(days=1)
                        return datetime.combine(parsed, datetime.min.time(), timezone.utc).isoformat()

                    start = query_date("from")
                    end = query_date("to", inclusive_end=True)
                    if start and end and start >= end:
                        raise ValueError("The start date must not be after the end date")
                    alarm_type = query.get("type", [""])[0] or None
                    if alarm_type not in (None, "network", "hardware", "resource", "service"):
                        raise ValueError("Unsupported alarm type")
                    limit = int(query.get("limit", ["100"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                    if not 1 <= limit <= 200 or not 0 <= offset <= 1000000:
                        raise ValueError("Alarm history pagination is out of range")
                    self.json_response(rack_store.read_alarm_history(start, end, alarm_type, limit, offset))
                except ValueError as error:
                    self.json_response({"error": str(error)}, 400)
                except sqlite3.Error:
                    LOG.exception("Unable to read alarm history")
                    self.json_response({"error": "Unable to read alarm history"}, 500)
            elif surface == "management" and path == "/api/health" and health_monitor is not None:
                try:
                    inventory = rack_store.read()["state"] if rack_store else {"devices": []}
                    health = health_monitor.snapshot({device["id"] for device in inventory["devices"]})
                    self.json_response({"devices": health})
                except (sqlite3.Error, ValueError):
                    LOG.exception("Unable to read device health")
                    self.json_response({"error": "Unable to read device health"}, 500)
            elif surface == "management" and path == "/api/management":
                inventory = rack_store.read()["state"] if rack_store else None
                health = health_monitor.snapshot({device["id"] for device in inventory["devices"]}) if health_monitor and inventory else {}
                alarm_state = rack_store.read_alarm_snapshot() if rack_store else {
                    "alerts": build_alerts(monitor.snapshot(), inventory, health),
                    "resolved_alerts": [],
                }
                self.json_response({
                    "services": monitor.snapshot(),
                    "devices": monitor.config.get("devices", []),
                    "health": health,
                    **alarm_state,
                })
            elif surface == "management" and path == "/api/racks" and rack_store is not None:
                try:
                    self.json_response(rack_store.read())
                except (sqlite3.Error, ValueError) as error:
                    LOG.exception("Unable to read rack inventory: %s", error)
                    self.json_response({"error": "Unable to read rack inventory"}, 500)
            elif surface == "management" and path == "/api/catalog" and catalog is not None:
                self.json_response(catalog)
            elif path in ("/", "/style.css", "/app.js", "/alerts.js", "/live.js",
                          "/rack", "/rack.js", "/login", "/login.js"):
                static_files = {
                    "/": f"{surface}.html",
                    "/style.css": "style.css",
                    "/app.js": f"{surface}.js",
                    "/alerts.js": "alerts.js",
                    "/live.js": "live.js",
                    "/rack": "rack.html",
                    "/rack.js": "rack.js",
                    "/login": "login.html",
                    "/login.js": "login.js",
                }
                if path in ("/rack", "/rack.js", "/login", "/login.js") and surface != "management":
                    self.respond(404, b"Not found\n", "text/plain; charset=utf-8")
                    return
                name = static_files[path]
                data = (ROOT / "web" / name).read_bytes()
                content_type = "text/html" if path in ("/", "/rack", "/login") else (
                    "text/css" if path.endswith(".css") else "application/javascript"
                )
                self.respond(200, data, content_type + "; charset=utf-8")
            else:
                self.respond(404, b"Not found\n", "text/plain; charset=utf-8")

        def do_PUT(self):
            if not self.require_test_session(urllib.parse.urlsplit(self.path).path):
                return
            if surface != "management" or urllib.parse.urlsplit(self.path).path != "/api/racks" or rack_store is None:
                self.respond(404, b"Not found\n", "text/plain; charset=utf-8")
                return
            origin = self.headers.get("Origin", "")
            parsed = urllib.parse.urlsplit(origin)
            host = self.headers.get("Host", "")
            if (parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host.lower()
                    or parsed.path or parsed.query or parsed.fragment
                    or self.headers.get("X-Requested-With") != "RackEditor"
                    or self.headers.get_content_type() != "application/json"):
                self.json_response({"error": "Same-origin JSON request required"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= 262144:
                    raise ValueError("Request body must be between 1 and 262144 bytes")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or set(payload) != {"revision", "state"}:
                    raise ValueError("Expected revision and state")
                revision = rack_store.save(payload["revision"], payload["state"])
                if revision is None:
                    self.json_response({"error": "Inventory changed in another session. Reload before saving."}, 409)
                else:
                    if health_monitor is not None:
                        health_monitor.reconcile(payload["state"])
                    self.json_response({"revision": revision})
            except (ValueError, UnicodeDecodeError, KeyError, TypeError) as error:
                self.json_response({"error": str(error)}, 400)
            except sqlite3.Error:
                LOG.exception("Unable to save rack inventory")
                self.json_response({"error": "Unable to save rack inventory"}, 500)

        def do_POST(self):
            if surface != "management" or rack_store is None:
                self.respond(404, b"Not found\n", "text/plain; charset=utf-8")
                return
            path = urllib.parse.urlsplit(self.path).path
            if test_auth_enabled and path == "/dologin":
                self.test_login()
                return
            if not self.require_test_session(path):
                return
            parts = path.split("/")
            if len(parts) != 5 or parts[1:3] != ["api", "devices"] or parts[4] != "discover":
                self.respond(404, b"Not found\n", "text/plain; charset=utf-8")
                return
            origin = self.headers.get("Origin", "")
            parsed = urllib.parse.urlsplit(origin)
            host = self.headers.get("Host", "")
            if (parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host.lower()
                    or parsed.path or parsed.query or parsed.fragment
                    or self.headers.get("X-Requested-With") != "RackEditor"
                    or self.headers.get_content_type() != "application/json"):
                self.json_response({"error": "Same-origin JSON request required"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= 16384:
                    raise ValueError("Request body must be between 1 and 16384 bytes")
                payload = json.loads(self.rfile.read(length))
                if (not isinstance(payload, dict)
                        or set(payload) != {"revision", "protocol", "credential_ref"}
                        or not isinstance(payload["revision"], int)
                        or isinstance(payload["revision"], bool)
                        or payload["protocol"] not in ("redfish", "snmp", "ipmi")
                        or not isinstance(payload["credential_ref"], str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", payload["credential_ref"])):
                    raise ValueError("Expected revision, protocol and credential profile")
                snapshot = rack_store.read()
                if snapshot["revision"] != payload["revision"]:
                    self.json_response({"error": "Inventory changed in another session. Reload before discovery."}, 409)
                    return
                device_id = urllib.parse.unquote(parts[3])
                device = next((item for item in snapshot["state"]["devices"] if item["id"] == device_id), None)
                if device is None:
                    self.respond(404, b"Device not found\n", "text/plain; charset=utf-8")
                    return
                if (payload["protocol"] != device["management_protocol"]
                        or payload["credential_ref"] != device["credential_ref"]):
                    raise ValueError("Save the device management settings before discovery")
                profile = load_profile(payload["credential_ref"], payload["protocol"], credential_file)
                discovered = discover(device["management_ip"], payload["protocol"], profile)
                device["discovery"] = {
                    "protocol": payload["protocol"],
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "details": discovered["details"],
                    "unavailable": discovered["unavailable"],
                }
                revision = rack_store.save(payload["revision"], snapshot["state"])
                if revision is None:
                    self.json_response({"error": "Inventory changed in another session. Reload before saving discovery results."}, 409)
                else:
                    if health_monitor is not None:
                        health_monitor.enroll(device)
                    self.json_response({"revision": revision, "discovery": device["discovery"]})
            except DiscoveryError as error:
                LOG.warning("Device discovery failed: %s", error)
                self.json_response({"error": str(error)}, error.status)
            except (ValueError, UnicodeDecodeError, KeyError, TypeError) as error:
                self.json_response({"error": str(error)}, 400)
            except sqlite3.Error:
                LOG.exception("Unable to read or save device discovery results")
                self.json_response({"error": "Unable to read or save device discovery results"}, 500)

        def json_response(self, value, status=200):
            self.respond(status, json.dumps(value).encode("utf-8"), "application/json; charset=utf-8")

        def respond(self, status, data, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to dashboard JSON configuration")
    parser.add_argument("--surface", required=True, choices=("status", "management"))
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default: loopback)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state", default="rack-state.sqlite3", help="management inventory database path")
    parser.add_argument("--catalog", default=str(ROOT / "hardware_catalog.json"), help="bundled hardware catalog")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    monitor = Monitor(config)
    catalog = load_catalog(args.catalog) if args.surface == "management" else None
    model_map = {model["id"]: model for model in catalog["models"]} if catalog else None
    rack_store = RackStore(args.state, config.get("devices", []), model_map) if catalog else None
    health_monitor = (
        DeviceHealthMonitor(rack_store, config.get("device_check_interval_seconds", 30))
        if rack_store else None
    )
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(monitor, args.surface, rack_store, catalog, health_monitor=health_monitor),
    )
    threading.Thread(target=monitor.run, daemon=True).start()
    if health_monitor:
        threading.Thread(target=health_monitor.run, daemon=True).start()
        threading.Thread(
            target=record_alarm_state,
            args=(monitor, rack_store, health_monitor),
            daemon=True,
        ).start()
    LOG.info("%s listening on %s:%s", args.surface, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
