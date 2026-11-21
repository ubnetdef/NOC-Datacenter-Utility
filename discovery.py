"""Read-only, single-address discovery for management interfaces."""

import base64
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit


class DiscoveryError(Exception):
    """A safe, user-presentable discovery failure."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


DEFAULT_CREDENTIAL_FILE = "/run/secrets/noc_device_credentials"
MAX_CREDENTIAL_FILE = 65536
MAX_REDFISH_RESPONSE = 1048576
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def load_profile(reference, protocol, path=None):
    path = Path(path or os.environ.get("NOC_CREDENTIALS_FILE", DEFAULT_CREDENTIAL_FILE))
    try:
        if path.stat().st_size > MAX_CREDENTIAL_FILE:
            raise DiscoveryError("The device credential secret is too large.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DiscoveryError("The Docker Swarm device credential secret is not mounted.", 503) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryError("The Docker Swarm device credential secret could not be read.", 503) from error
    profiles = data.get("profiles") if isinstance(data, dict) else None
    profile = profiles.get(reference) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict) or profile.get("protocol") != protocol:
        raise DiscoveryError("No matching credential profile exists in the Docker Swarm secret.", 400)
    if protocol in ("redfish", "ipmi"):
        if not all(isinstance(profile.get(key), str) and profile[key] for key in ("username", "password")):
            raise DiscoveryError("The selected credential profile needs a username and password.", 400)
        if (len(profile["username"]) > 128 or len(profile["password"]) > 256
                or "\x00" in profile["username"] or "\x00" in profile["password"]):
            raise DiscoveryError("The selected device credentials are invalid.", 400)
    if protocol == "redfish":
        ca_certificate = profile.get("ca_certificate")
        if ca_certificate is not None and (not isinstance(ca_certificate, str) or len(ca_certificate) > 32768):
            raise DiscoveryError("The Redfish CA certificate is invalid.", 400)
    elif protocol == "snmp":
        if not isinstance(profile.get("community"), str) or not profile["community"] or len(profile["community"]) > 128:
            raise DiscoveryError("The selected SNMP credential profile needs a community string.", 400)
    return profile


def _management_address(value):
    if not isinstance(value, str):
        raise DiscoveryError("Set a valid management IP address before discovery.", 400)
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise DiscoveryError("Set a valid management IP address before discovery.", 400) from error
    if (not any(address.version == network.version and address in network for network in PRIVATE_NETWORKS)
            or address.is_loopback or address.is_multicast or address.is_unspecified or address.is_reserved):
        raise DiscoveryError("Discovery is limited to private-network management IPs.", 400)
    return address


def discover(address_text, protocol, profile):
    address = _management_address(address_text)
    if protocol == "redfish":
        details = _discover_redfish(address, profile)
    elif protocol == "snmp":
        details = _discover_snmp(address, profile)
        try:
            details["NetworkInterfaces"] = _monitor_snmp_interfaces(address, profile)
        except DiscoveryError as error:
            details.setdefault("DiscoveryWarnings", []).append(f"Network interface inventory: {error}")
    elif protocol == "ipmi":
        details = _discover_ipmi(address, profile)
        try:
            supplies, drives, resources = _monitor_ipmi_sensors(address, profile)
            details["PowerSupplies"] = supplies
            details["Drives"] = drives
            details["Resources"] = resources
        except DiscoveryError as error:
            details.setdefault("DiscoveryWarnings", []).append(f"Sensor inventory: {error}")
    else:
        raise DiscoveryError("Unsupported management protocol.")

    warnings = details.pop("DiscoveryWarnings", [])
    groups = (
        ("Host name", ("HostName", "Name", "System name"), None),
        ("Manufacturer", ("Manufacturer",), None),
        ("Model", ("Model",), None),
        ("Serial number", ("SerialNumber",), None),
        ("Processor details", ("ProcessorSummary", "Processors"), "CPU"),
        ("Memory details", ("MemorySummary", "MemoryModules"), "Memory"),
        ("Drive inventory", ("Drives",), None),
        ("Power-supply inventory", ("PowerSupplies",), None),
        ("Network-interface inventory", ("NetworkInterfaces",), None),
    )
    unavailable = [
        f"{label} was not reported by {protocol.upper()}."
        for label, keys, resource_name in groups
        if not any(
            key in details and (
                isinstance(details[key], list) and key in ("Drives", "PowerSupplies", "NetworkInterfaces")
                or details[key] not in (None, "", [], {})
            )
            for key in keys
        ) and not (
            resource_name and any(
                str(resource.get("name", "")).casefold() == resource_name.casefold()
                for resource in details.get("Resources", []) if isinstance(resource, dict)
            )
        )
    ]
    unavailable.extend(str(warning)[:240] for warning in warnings[:16])
    return {"details": details, "unavailable": unavailable}


def monitor_device(address_text, protocol, profile):
    address = _management_address(address_text)
    if protocol == "redfish":
        return _monitor_redfish(address, profile)
    if protocol == "snmp":
        _discover_snmp(address, profile)
        result = {
            "system": {"state": "reachable", "health": "ok"},
            "network_ports": [],
        }
        try:
            result["network_ports"] = _monitor_snmp_interfaces(address, profile)
        except DiscoveryError as error:
            result["telemetry_warning"] = str(error)
            result["network_telemetry_unavailable"] = True
        return result
    if protocol == "ipmi":
        details = _discover_ipmi(address, profile)
        result = {
            "system": {"state": "reachable", "health": "unknown"},
            "power_supplies": [],
            "drives": [],
            "resources": [],
            "network_ports": [],
            "details": details,
            "component_coverage": {"power_supplies": False, "drives": False},
            "network_telemetry_unavailable": True,
        }
        try:
            (result["power_supplies"], result["drives"],
             result["resources"]) = _monitor_ipmi_sensors(address, profile)
            result["component_coverage"] = {"power_supplies": True, "drives": True}
        except DiscoveryError as error:
            result["telemetry_warning"] = str(error)
        return result
    raise DiscoveryError("Unsupported management protocol.")


def _redfish_path(value):
    parsed = urlsplit(value)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or not parsed.path.startswith("/redfish/v1/")):
        raise DiscoveryError("The device returned an invalid Redfish resource link.")
    return parsed.path


def _redfish_get(connection, path, authorization):
    deadline = getattr(connection, "discovery_deadline", None)
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DiscoveryError("Redfish discovery exceeded its time limit.", 504)
        connection.timeout = min(5, remaining)
        sock = getattr(connection, "sock", None)
        if sock is not None:
            sock.settimeout(connection.timeout)
    connection.request("GET", _redfish_path(path), headers={
        "Accept": "application/json",
        "Authorization": authorization,
    })
    response = connection.getresponse()
    if response.status != 200:
        response.read(4096)
        raise DiscoveryError(f"Redfish returned HTTP {response.status}.")
    try:
        content_length = int(response.getheader("Content-Length", "0") or "0")
    except ValueError as error:
        raise DiscoveryError("Redfish returned an invalid response length.") from error
    if content_length > MAX_REDFISH_RESPONSE:
        response.close()
        raise DiscoveryError("The Redfish response exceeded the size limit.")
    payload = response.read(MAX_REDFISH_RESPONSE + 1)
    if len(payload) > MAX_REDFISH_RESPONSE:
        raise DiscoveryError("The Redfish response exceeded the size limit.")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiscoveryError("The device returned invalid Redfish JSON.") from error
    if not isinstance(value, dict):
        raise DiscoveryError("The device returned an invalid Redfish resource.")
    return value


def _safe_fields(resource, fields):
    result = {}
    for key in fields:
        value = resource.get(key)
        if isinstance(value, str):
            result[key] = value[:300]
        elif isinstance(value, (int, float)) and (not isinstance(value, float) or math.isfinite(value)):
            result[key] = value
    return result


def _redfish_members(connection, authorization, link, fields, limit=8):
    if not isinstance(link, dict) or not isinstance(link.get("@odata.id"), str):
        return []
    collection = _redfish_get(connection, link["@odata.id"], authorization)
    members = collection.get("Members")
    if not isinstance(members, list):
        return []
    results = []
    for member in members[:limit]:
        if not isinstance(member, dict) or not isinstance(member.get("@odata.id"), str):
            continue
        resource = _redfish_get(connection, member["@odata.id"], authorization)
        details = _safe_fields(resource, fields)
        if details:
            results.append(details)
    return results


def _discover_redfish(address, profile):
    try:
        context = ssl.create_default_context(cadata=profile.get("ca_certificate"))
    except (ssl.SSLError, ValueError) as error:
        raise DiscoveryError("The Redfish CA certificate could not be loaded.") from error
    connection = http.client.HTTPSConnection(address.compressed, 443, timeout=5, context=context)
    connection.discovery_deadline = time.monotonic() + 30
    authorization = "Basic " + base64.b64encode(
        f"{profile['username']}:{profile['password']}".encode("utf-8")
    ).decode("ascii")
    try:
        root = _redfish_get(connection, "/redfish/v1/", authorization)
        systems = root.get("Systems")
        system_link = systems.get("@odata.id") if isinstance(systems, dict) else None
        if not isinstance(system_link, str):
            raise DiscoveryError("Redfish did not advertise a Systems collection.")
        collection = _redfish_get(connection, system_link, authorization)
        members = collection.get("Members")
        if not isinstance(members, list) or not members or not isinstance(members[0], dict):
            raise DiscoveryError("Redfish did not return a system resource.")
        system_link = members[0].get("@odata.id")
        if not isinstance(system_link, str):
            raise DiscoveryError("Redfish returned an invalid system resource link.")
        system = _redfish_get(connection, system_link, authorization)
        details = {}
        for key in ("HostName", "Name", "Manufacturer", "Model", "SerialNumber", "SKU", "PowerState", "SystemType", "AssetTag"):
            details.update(_safe_fields(system, (key,)))
        processor = system.get("ProcessorSummary")
        if isinstance(processor, dict):
            details["ProcessorSummary"] = _safe_fields(processor, ("Count", "Model", "Status"))
        memory = system.get("MemorySummary")
        if isinstance(memory, dict):
            details["MemorySummary"] = _safe_fields(memory, ("TotalSystemMemoryGiB", "Status"))
        storage = system.get("StorageSummary")
        if isinstance(storage, dict):
            details["StorageSummary"] = _safe_fields(storage, ("StorageControllers", "Status"))
        warnings = []
        try:
            processors = _redfish_members(
                connection, authorization, system.get("Processors"),
                ("Manufacturer", "Model", "TotalCores", "TotalThreads"), limit=32,
            )
            if processors:
                details["Processors"] = processors
        except DiscoveryError as error:
            warnings.append(f"Processor inventory: {error}")
        try:
            memory_modules = _redfish_members(
                connection, authorization, system.get("Memory"),
                ("DeviceLocator", "Manufacturer", "Model", "SerialNumber", "CapacityMiB", "SizeMiB"),
                limit=64,
            )
            if memory_modules:
                details["MemoryModules"] = memory_modules
        except DiscoveryError as error:
            warnings.append(f"Memory inventory: {error}")
        storage_link = system.get("Storage")
        if isinstance(storage_link, dict) and isinstance(storage_link.get("@odata.id"), str):
            drives = []
            try:
                storage_collection = _redfish_get(connection, storage_link["@odata.id"], authorization)
                storage_members = storage_collection.get("Members")
                if isinstance(storage_members, list):
                    for member in storage_members[:8]:
                        if not isinstance(member, dict):
                            continue
                        drives.extend(_redfish_members(
                            connection, authorization, member.get("Drives"),
                            ("Name", "Model", "SerialNumber", "CapacityBytes", "MediaType", "Protocol"),
                            limit=max(0, 128 - len(drives)),
                        ))
                        if len(drives) >= 128:
                            break
                details["Drives"] = drives
            except DiscoveryError as error:
                if drives:
                    details["Drives"] = drives
                warnings.append(f"Drive inventory: {error}")

        interfaces_link = system.get("EthernetInterfaces")
        if isinstance(interfaces_link, dict) and isinstance(interfaces_link.get("@odata.id"), str):
            try:
                interfaces = _redfish_members(
                    connection, authorization, interfaces_link,
                    ("Id", "Name", "Description", "MACAddress", "PermanentMACAddress", "LinkStatus"),
                    limit=64,
                )
                details["NetworkInterfaces"] = interfaces
            except DiscoveryError as error:
                warnings.append(f"Network interface inventory: {error}")
        else:
            warnings.append("Network interface inventory: Redfish did not advertise EthernetInterfaces.")

        chassis_link = root.get("Chassis")
        if isinstance(chassis_link, dict) and isinstance(chassis_link.get("@odata.id"), str):
            try:
                chassis_items = _redfish_collection(connection, authorization, chassis_link, limit=8)
                supplies = []
                for chassis_id, chassis in chassis_items:
                    power_link = chassis.get("Power")
                    if not isinstance(power_link, dict) and isinstance(chassis.get("PowerSubsystem"), dict):
                        subsystem_link = chassis["PowerSubsystem"].get("@odata.id")
                        subsystem = _redfish_get(connection, subsystem_link, authorization) if isinstance(subsystem_link, str) else {}
                        power_link = subsystem.get("PowerSupplies")
                    if isinstance(power_link, dict) and isinstance(power_link.get("@odata.id"), str):
                        power = _redfish_get(connection, power_link["@odata.id"], authorization)
                        direct_supplies = power.get("PowerSupplies")
                        if isinstance(direct_supplies, list):
                            for index, supply in enumerate(direct_supplies[:16]):
                                if isinstance(supply, dict):
                                    supplies.append(_redfish_component(supply, f"{chassis_id}:psu:{index + 1}"))
                        else:
                            supplies.extend(
                                _redfish_component(resource, resource_id)
                                for resource_id, resource in _redfish_collection(
                                    connection, authorization, power_link, limit=16,
                                )
                            )
                details["PowerSupplies"] = supplies
            except DiscoveryError as error:
                warnings.append(f"Power-supply inventory: {error}")
        else:
            warnings.append("Power-supply inventory: Redfish did not advertise Chassis.")

        if warnings:
            details["DiscoveryWarnings"] = warnings
        return details
    except DiscoveryError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as error:
        raise DiscoveryError("Could not securely connect to the Redfish interface.") from error
    finally:
        connection.close()


def _redfish_component(resource, resource_id):
    status = resource.get("Status") if isinstance(resource.get("Status"), dict) else {}
    return {
        "id": str(resource.get("Id") or resource.get("Name") or resource_id)[:120],
        "name": str(resource.get("Name") or resource.get("Id") or resource_id)[:120],
        "state": str(resource.get("State") or "unknown")[:40],
        "health": str(status.get("Health") or "unknown")[:40],
    }


def _redfish_collection(connection, authorization, link, limit=32):
    if not isinstance(link, dict) or not isinstance(link.get("@odata.id"), str):
        return []
    collection = _redfish_get(connection, link["@odata.id"], authorization)
    members = collection.get("Members")
    if not isinstance(members, list):
        return []
    results = []
    for member in members[:limit]:
        if isinstance(member, dict) and isinstance(member.get("@odata.id"), str):
            resource = _redfish_get(connection, member["@odata.id"], authorization)
            results.append((member["@odata.id"], resource))
    return results


def _monitor_redfish(address, profile):
    try:
        context = ssl.create_default_context(cadata=profile.get("ca_certificate"))
    except (ssl.SSLError, ValueError) as error:
        raise DiscoveryError("The Redfish CA certificate could not be loaded.") from error
    connection = http.client.HTTPSConnection(address.compressed, 443, timeout=5, context=context)
    connection.discovery_deadline = time.monotonic() + 20
    authorization = "Basic " + base64.b64encode(
        f"{profile['username']}:{profile['password']}".encode("utf-8")
    ).decode("ascii")
    try:
        root = _redfish_get(connection, "/redfish/v1/", authorization)
        systems = _redfish_collection(connection, authorization, root.get("Systems"), limit=1)
        if not systems:
            raise DiscoveryError("Redfish did not return a system resource.")
        system_id, system = systems[0]
        system_status = system.get("Status") if isinstance(system.get("Status"), dict) else {}
        result = {
            "system": {
                "state": str(system.get("PowerState") or system.get("State") or "unknown")[:40],
                "health": str(system_status.get("Health") or "unknown")[:40],
            },
            "power_supplies": [],
            "drives": [],
            "resources": [],
            "network_ports": [],
            "component_coverage": {"power_supplies": False, "drives": False},
        }
        for resource_id, resource_name, summary_key in (
            ("cpu", "CPU", "ProcessorSummary"),
            ("memory", "Memory", "MemorySummary"),
        ):
            summary = system.get(summary_key)
            status = summary.get("Status") if isinstance(summary, dict) else None
            health = status.get("Health") if isinstance(status, dict) else None
            if isinstance(health, str):
                result["resources"].append({
                    "id": resource_id,
                    "name": resource_name,
                    "health": health[:40],
                    "threshold_exceeded": health.casefold() in ("warning", "critical", "failed"),
                })
        warnings = []
        chassis_link = root.get("Chassis")
        try:
            chassis_items = _redfish_collection(connection, authorization, chassis_link, limit=8)
        except DiscoveryError as error:
            chassis_items = []
            warnings.append(f"Chassis inventory unavailable: {error}")
        for chassis_id, chassis in chassis_items:
            power_link = chassis.get("Power")
            try:
                if not isinstance(power_link, dict) and isinstance(chassis.get("PowerSubsystem"), dict):
                    subsystem_link = chassis["PowerSubsystem"].get("@odata.id")
                    subsystem = _redfish_get(connection, subsystem_link, authorization) if isinstance(subsystem_link, str) else {}
                    power_link = subsystem.get("PowerSupplies")
                if isinstance(power_link, dict) and isinstance(power_link.get("@odata.id"), str):
                    power = _redfish_get(connection, power_link["@odata.id"], authorization)
                    supplies = power.get("PowerSupplies")
                    if isinstance(supplies, list):
                        result["component_coverage"]["power_supplies"] = True
                        for index, supply in enumerate(supplies[:16]):
                            if isinstance(supply, dict):
                                result["power_supplies"].append(
                                    _redfish_component(supply, f"{chassis_id}:psu:{index + 1}")
                                )
                    else:
                        members = _redfish_collection(connection, authorization, power_link, limit=16)
                        result["component_coverage"]["power_supplies"] = True
                        result["power_supplies"].extend(
                            _redfish_component(resource, resource_id)
                            for resource_id, resource in members
                        )
            except DiscoveryError as error:
                result["component_coverage"]["power_supplies"] = False
                warnings.append(f"Power-supply telemetry unavailable: {error}")

        storage_link = system.get("Storage")
        try:
            controllers = _redfish_collection(connection, authorization, storage_link, limit=8)
            if isinstance(storage_link, dict) and isinstance(storage_link.get("@odata.id"), str):
                result["component_coverage"]["drives"] = True
            for controller_id, controller in controllers:
                result["drives"].extend(
                    _redfish_component(drive, drive_id)
                    for drive_id, drive in _redfish_collection(
                        connection, authorization, controller.get("Drives"), limit=64
                    )
                )
        except DiscoveryError as error:
            result["component_coverage"]["drives"] = False
            warnings.append(f"Drive telemetry unavailable: {error}")
        result["drives"] = result["drives"][:128]

        try:
            network_link = system.get("EthernetInterfaces")
            if not isinstance(network_link, dict) or not isinstance(network_link.get("@odata.id"), str):
                result["network_telemetry_unavailable"] = True
                network_interfaces = []
            else:
                network_interfaces = _redfish_collection(
                    connection, authorization, network_link, limit=64
                )
                result["network_telemetry_unavailable"] = False
            result["network_ports"] = [
                {
                    **_redfish_component(interface, interface_id),
                    "link": str(interface.get("LinkStatus") or "unknown")[:40],
                    "mac": str(interface.get("MACAddress") or "")[:64],
                }
                for interface_id, interface in network_interfaces
            ]
        except DiscoveryError as error:
            result["network_telemetry_unavailable"] = True
            warnings.append(f"Network-interface telemetry unavailable: {error}")
        result["network_ports"] = [
            {key: value for key, value in port.items() if value}
            for port in result["network_ports"]
        ]
        if warnings:
            result["telemetry_warning"] = " ".join(warnings)[:600]
        return result
    except DiscoveryError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError, KeyError) as error:
        raise DiscoveryError("Could not securely read Redfish health data.") from error
    finally:
        connection.close()


def _tlv(tag, data):
    length = len(data)
    if length < 128:
        encoded_length = bytes((length,))
    else:
        raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
        encoded_length = bytes((0x80 | len(raw),)) + raw
    return bytes((tag,)) + encoded_length + data


def _ber_integer(value):
    size = max(1, (value.bit_length() + 8) // 8)
    return _tlv(0x02, value.to_bytes(size, "big", signed=True))


def _base128(value):
    result = [value & 0x7f]
    value >>= 7
    while value:
        result.append(0x80 | (value & 0x7f))
        value >>= 7
    return bytes(reversed(result))


def _ber_oid(value):
    arcs = [int(arc) for arc in value.split(".")]
    if len(arcs) < 2 or arcs[0] > 2 or arcs[1] > 39 and arcs[0] < 2:
        raise ValueError("Invalid SNMP object identifier")
    encoded = _base128(40 * arcs[0] + arcs[1])
    encoded += b"".join(_base128(arc) for arc in arcs[2:])
    return _tlv(0x06, encoded)


def _read_tlv(data, offset):
    if offset + 2 > len(data):
        raise DiscoveryError("SNMP returned a truncated response.")
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7f
        if count == 0 or count > 4 or offset + count > len(data):
            raise DiscoveryError("SNMP returned an invalid response length.")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise DiscoveryError("SNMP returned a truncated response.")
    return tag, data[offset:end], end


def _read_children(data):
    children = []
    offset = 0
    while offset < len(data):
        tag, value, offset = _read_tlv(data, offset)
        children.append((tag, value))
    return children


def _decode_oid(data):
    encoded_arcs = []
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7f)
        if not byte & 0x80:
            encoded_arcs.append(value)
            value = 0
    if value or not encoded_arcs:
        raise DiscoveryError("SNMP returned an invalid object identifier.")
    first = encoded_arcs.pop(0)
    leading = (0, first) if first < 40 else (1, first - 40) if first < 80 else (2, first - 80)
    return ".".join(str(arc) for arc in (*leading, *encoded_arcs))


def _snmp_value(tag, value):
    if tag in (0x02, 0x41, 0x42, 0x43, 0x46):
        return int.from_bytes(value, "big", signed=tag == 0x02)
    if tag == 0x04:
        return value.decode("utf-8", errors="replace")
    if tag == 0x06:
        return _decode_oid(value)
    if tag == 0x40:
        return str(ipaddress.ip_address(value))
    if tag in (0x80, 0x81, 0x82):
        raise DiscoveryError("The SNMP agent reported an unavailable object.")
    return value.hex()


def _snmp_packet(community, request_id, oids, pdu_tag=0xa0):
    bindings = b"".join(
        _tlv(0x30, _ber_oid(oid) + _tlv(0x05, b""))
        for oid in oids
    )
    varbind_list = _tlv(0x30, bindings)
    pdu = _tlv(pdu_tag, _ber_integer(request_id) + _ber_integer(0) + _ber_integer(0) + varbind_list)
    return _tlv(0x30, _ber_integer(1) + _tlv(0x04, community.encode("utf-8")) + pdu)


def _snmp_get_bulk(address, community, oid, request_id):
    bindings = _tlv(0x30, _ber_oid(oid) + _tlv(0x05, b""))
    varbind_list = _tlv(0x30, bindings)
    pdu = _tlv(0xa5, _ber_integer(request_id) + _ber_integer(0) + _ber_integer(20) + varbind_list)
    packet = _tlv(0x30, _ber_integer(1) + _tlv(0x04, community.encode("utf-8")) + pdu)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as client:
            client.settimeout(3)
            client.connect((address.compressed, 161))
            client.send(packet)
            response = client.recv(65535)
    except OSError as error:
        raise DiscoveryError("Could not reach the SNMP agent at the configured address.") from error
    tag, outer, end = _read_tlv(response, 0)
    if tag != 0x30 or end != len(response):
        raise DiscoveryError("The SNMP agent returned an invalid response.")
    message = _read_children(outer)
    if (len(message) != 3 or message[0] != (0x02, b"\x01")
            or message[1] != (0x04, community.encode("utf-8")) or message[2][0] != 0xa2):
        raise DiscoveryError("The SNMP agent returned an invalid response.")
    fields = _read_children(message[2][1])
    if (len(fields) != 4 or int.from_bytes(fields[0][1], "big", signed=True) != request_id
            or int.from_bytes(fields[1][1], "big", signed=True)
            or int.from_bytes(fields[2][1], "big", signed=True)):
        raise DiscoveryError("The SNMP agent rejected the read-only interface request.")
    values = []
    for _, binding in _read_children(fields[3][1]):
        parts = _read_children(binding)
        if len(parts) == 2 and parts[0][0] == 0x06 and parts[1][0] not in (0x80, 0x81, 0x82):
            values.append((_decode_oid(parts[0][1]), _snmp_value(*parts[1])))
    return values


def _snmp_walk(address, community, root_oid, limit=128):
    found = []
    cursor = root_oid
    for _ in range(limit // 20 + 1):
        values = _snmp_get_bulk(address, community, cursor, secrets.randbelow(0x7ffffffe) + 1)
        if not values:
            break
        advanced = False
        for oid, value in values:
            if not oid.startswith(root_oid + "."):
                return found
            if tuple(map(int, oid.split("."))) <= tuple(map(int, cursor.split("."))):
                raise DiscoveryError("The SNMP agent returned a non-advancing interface table.")
            found.append((oid, value))
            cursor = oid
            advanced = True
            if len(found) >= limit:
                return found
        if not advanced:
            break
    return found


def _monitor_snmp_interfaces(address, profile):
    community = profile["community"]
    if any(ord(char) < 32 or ord(char) > 126 for char in community):
        raise DiscoveryError("SNMP community strings must use printable ASCII.")
    names = _snmp_walk(address, community, "1.3.6.1.2.1.31.1.1.1.1")
    descriptions = _snmp_walk(address, community, "1.3.6.1.2.1.2.2.1.2")
    statuses = _snmp_walk(address, community, "1.3.6.1.2.1.2.2.1.8")
    name_map = {oid.rsplit(".", 1)[-1]: value for oid, value in names}
    description_map = {oid.rsplit(".", 1)[-1]: value for oid, value in descriptions}
    label = {1: "up", 2: "down", 3: "testing", 4: "unknown",
             5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
    return [
        {
            "id": index,
            "name": str(name_map.get(index) or description_map.get(index) or f"Interface {index}")[:120],
            "state": "enabled",
            "health": "ok" if value == 1 else "warning" if value in (2, 5, 7) else "unknown",
            "link": label.get(value, "unknown"),
        }
        for oid, value in statuses
        for index in (oid.rsplit(".", 1)[-1],)
    ]


def _discover_snmp(address, profile):
    community = profile["community"]
    if any(ord(char) < 32 or ord(char) > 126 for char in community):
        raise DiscoveryError("SNMP community strings must use printable ASCII.")
    request_id = secrets.randbelow(0x7ffffffe) + 1
    oids = (
        "1.3.6.1.2.1.1.1.0",
        "1.3.6.1.2.1.1.2.0",
        "1.3.6.1.2.1.1.5.0",
    )
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as client:
            client.settimeout(3)
            client.connect((address.compressed, 161))
            client.send(_snmp_packet(community, request_id, oids))
            packet = client.recv(65535)
    except OSError as error:
        raise DiscoveryError("Could not reach the SNMP agent at the configured address.") from error

    tag, outer, end = _read_tlv(packet, 0)
    if tag != 0x30 or end != len(packet):
        raise DiscoveryError("The SNMP agent returned an invalid response.")
    message = _read_children(outer)
    if (len(message) != 3 or message[0] != (0x02, b"\x01")
            or message[1] != (0x04, community.encode("utf-8"))):
        raise DiscoveryError("The SNMP agent returned an invalid response.")
    if message[2][0] != 0xa2:
        raise DiscoveryError("The SNMP agent returned an invalid response.")
    fields = _read_children(message[2][1])
    if len(fields) != 4 or fields[0][0] != 0x02 or int.from_bytes(fields[0][1], "big", signed=True) != request_id:
        raise DiscoveryError("The SNMP agent returned an unmatched response.")
    if int.from_bytes(fields[1][1], "big", signed=True):
        raise DiscoveryError("The SNMP agent rejected the read-only inventory request.")
    details = {}
    for _, binding in _read_children(fields[3][1]):
        parts = _read_children(binding)
        if len(parts) != 2 or parts[0][0] != 0x06:
            continue
        oid = _decode_oid(parts[0][1])
        details[{"1.3.6.1.2.1.1.1.0": "System description",
                 "1.3.6.1.2.1.1.2.0": "System object identifier",
                 "1.3.6.1.2.1.1.5.0": "System name"}.get(oid, oid)] = _snmp_value(*parts[1])
    details = {
        key: value[:512] if isinstance(value, str) else value
        for key, value in details.items()
    }
    if not details:
        raise DiscoveryError("The SNMP agent returned no inventory values.")
    return details


def _discover_ipmi(address, profile):
    command = [
        "ipmitool", "-I", "lanplus", "-H", address.compressed,
        "-U", profile["username"], "-E", "-N", "2", "-R", "0", "mc", "info",
    ]
    if address.version == 6:
        command.insert(1, "-6")
    environment = os.environ.copy()
    environment["IPMI_PASSWORD"] = profile["password"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8,
                                check=False, env=environment)
    except FileNotFoundError as error:
        raise DiscoveryError("IPMI support is unavailable in this container image.") from error
    except subprocess.TimeoutExpired as error:
        raise DiscoveryError("The IPMI request timed out.") from error
    if result.returncode:
        raise DiscoveryError("The IPMI interface rejected the read-only device information request.")
    details = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() in {"Device ID", "Device Revision", "Firmware Revision",
                               "Manufacturer ID", "Product ID"}:
                details[key.strip()] = value.strip()[:120]
    if not details:
        raise DiscoveryError("The IPMI interface returned no device information.")
    return details


def _monitor_ipmi_sensors(address, profile):
    command = [
        "ipmitool", "-I", "lanplus", "-H", address.compressed,
        "-U", profile["username"], "-E", "-N", "2", "-R", "0", "sensor", "list",
    ]
    if address.version == 6:
        command.insert(1, "-6")
    environment = os.environ.copy()
    environment["IPMI_PASSWORD"] = profile["password"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8,
                                check=False, env=environment)
    except FileNotFoundError as error:
        raise DiscoveryError("IPMI support is unavailable in this container image.") from error
    except (subprocess.TimeoutExpired, OSError) as error:
        raise DiscoveryError("The IPMI sensor request failed.") from error
    if result.returncode:
        raise DiscoveryError("The IPMI interface did not return sensor status.")
    supplies, drives, resources = [], [], []
    for line in result.stdout[:131072].splitlines()[:512]:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 2:
            continue
        name = fields[0][:120]
        normalized = name.casefold()
        is_supply = any(term in normalized for term in ("psu", "power supply")) or re.match(r"ps\d+", normalized)
        is_drive = any(term in normalized for term in ("drive", "disk", "hdd", "ssd"))
        is_cpu = any(term in normalized for term in ("cpu", "processor")) and not any(
            term in normalized for term in ("temp", "temperature", "voltage", "fan")
        )
        is_memory = any(term in normalized for term in ("memory", "dimm")) and not any(
            term in normalized for term in ("temp", "temperature", "voltage")
        )
        if not (is_supply or is_drive or is_cpu or is_memory):
            continue
        reading = (fields[3] if len(fields) >= 4 else fields[-1]).casefold()
        health = "ok" if reading in ("ok", "normal", "nominal") else (
            "critical" if reading in ("cr", "critical", "nr", "failure", "failed") else
            "warning" if reading in ("unc", "lnc", "warning", "non-critical") else "unknown"
        )
        component = {
            "id": name,
            "name": name,
            "state": "absent" if reading in ("no reading", "not present", "absent") else "enabled",
            "health": health,
        }
        if is_supply:
            supplies.append(component)
        elif is_drive:
            drives.append(component)
        elif is_cpu or is_memory:
            resources.append({
                "id": "cpu" if is_cpu else "memory",
                "name": "CPU" if is_cpu else "Memory",
                "health": health,
                "threshold_exceeded": health in ("warning", "critical"),
            })
    return supplies, drives, resources
