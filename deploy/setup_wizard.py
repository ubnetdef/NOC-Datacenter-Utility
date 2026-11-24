#!/usr/bin/env python3
"""Interactive Debian/Ubuntu setup for the NOC dashboard host."""

import getpass
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = Path("/etc/noc-datacenter-utility")
APACHE_SITE = Path("/etc/apache2/sites-available/noc-dashboard.conf")
SESSION_KEY_FILE = Path("/etc/apache2/noc-session-key")
PAM_SERVICE = Path("/etc/pam.d/noc-management")
ADMIN_GROUP = "noc-admin"
STACK_NAME = "noc"
PROFILE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


class SetupError(Exception):
    pass


def run(command, *, cwd=None, env=None, capture=False):
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, check=True, text=True,
            capture_output=capture,
        )
    except FileNotFoundError as error:
        raise SetupError(f"Required command not found: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if error.stderr else ""
        raise SetupError(f"Command failed: {' '.join(command)}" + (f"\n{detail}" if detail else "")) from error
    return result.stdout.strip() if capture else ""


def prompt(label, default=None):
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default or ""


def confirm(label, default=False):
    choice = prompt(f"{label} (y/N)" if not default else f"{label} (Y/n)")
    return choice.lower() in ("y", "yes") if choice else default


def require_supported_host():
    if os.geteuid() != 0:
        raise SetupError("Run as root, for example: sudo bash deploy/setup.sh")
    release = Path("/etc/os-release")
    if not release.exists():
        raise SetupError("This setup script supports Debian or Ubuntu Linux hosts.")
    values = {}
    for line in release.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    identity = " ".join((values.get("ID", ""), values.get("ID_LIKE", ""))).lower()
    if not any(name in identity.split() for name in ("debian", "ubuntu")):
        raise SetupError("This setup script supports Debian or Ubuntu Linux hosts.")
    if platform.system() != "Linux":
        raise SetupError("Run this script on the Linux Docker host, optionally over SSH.")


def validate_management_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise SetupError("Enter a valid private lights-out/BMC IP address.") from error
    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    if (not any(address.version == network.version and address in network for network in private_networks)
            or address.is_loopback or address.is_multicast or address.is_unspecified or address.is_reserved):
        raise SetupError("The lights-out address must be a private IPv4 or unique-local IPv6 address.")
    return address


def discover_route(address):
    family = "-6" if address.version == 6 else "-4"
    output = run(["ip", family, "route", "get", address.compressed], capture=True)
    match = re.search(r"(?:^|\s)src\s+(\S+)", output)
    if not match:
        raise SetupError(f"The host has no usable source route to {address}. Check its lights-out network routing.")
    return match.group(1), output


def validate_proxy_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise SetupError("Enter the separate reverse-proxy server's IP address.") from error
    if address.is_loopback or address.is_multicast or address.is_unspecified:
        raise SetupError("The reverse-proxy address cannot be loopback, multicast, or unspecified.")
    return address.compressed


def collect_profiles():
    profiles = {}
    while True:
        name = prompt("Credential profile name")
        if not PROFILE_NAME.fullmatch(name):
            raise SetupError("Profile names must be 1-64 letters, numbers, dots, underscores, or hyphens.")
        if name in profiles:
            raise SetupError(f"Duplicate profile name: {name}")
        protocol_choice = prompt("Protocol: 1=Redfish, 2=SNMP v2c, 3=IPMI")
        if protocol_choice not in ("1", "2", "3"):
            raise SetupError("Choose protocol 1, 2, or 3.")
        protocol = {"1": "redfish", "2": "snmp", "3": "ipmi"}[protocol_choice]
        profile = {"protocol": protocol}
        if protocol == "snmp":
            community = getpass.getpass("SNMP community (input hidden): ")
            if (not community or len(community) > 128
                    or any(ord(character) < 32 or ord(character) > 126 for character in community)):
                raise SetupError("SNMP communities must contain 1-128 printable ASCII characters.")
            profile["community"] = community
        else:
            username = prompt("Read-only device username")
            password = getpass.getpass("Device password (input hidden): ")
            if (not username or len(username) > 128 or not password or len(password) > 256
                    or any(ord(character) < 32 or ord(character) == 127 for character in username + password)):
                raise SetupError("Device credentials are empty or exceed the supported length.")
            profile.update({"username": username, "password": password})
            if protocol == "redfish":
                ca_path = prompt("Optional private CA PEM file path (blank if not needed)")
                if ca_path:
                    try:
                        certificate = Path(ca_path).expanduser().read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as error:
                        raise SetupError(f"Could not read the Redfish CA certificate: {error}") from error
                    if not certificate or len(certificate) > 32768:
                        raise SetupError("The Redfish CA certificate must contain 1-32768 characters.")
                    profile["ca_certificate"] = certificate
        profiles[name] = profile
        if not confirm("Add another device credential profile"):
            break
    encoded = json.dumps({"profiles": profiles}, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > 65536:
        raise SetupError("The credential profiles exceed the 64 KiB Docker secret limit.")
    return encoded


def add_admin_users():
    import grp
    import pwd

    try:
        grp.getgrnam(ADMIN_GROUP)
    except KeyError:
        run(["groupadd", "--system", ADMIN_GROUP])
    print("Management website access uses existing Linux OS accounts.")
    names = prompt("Authorized local usernames (comma-separated)")
    users = [name.strip() for name in names.split(",") if name.strip()]
    if not users:
        raise SetupError("Add at least one authorized Linux account to the management group.")
    for name in users:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?", name):
            raise SetupError(f"Invalid Linux username: {name}")
        if name == "root":
            raise SetupError("Do not use the root account for management-site sign-in.")
        try:
            pwd.getpwnam(name)
        except KeyError as error:
            if not confirm(f"Create Linux account {name} now"):
                raise SetupError(f"Linux user does not exist: {name}.") from error
            run(["adduser", "--disabled-password", "--gecos", "", name])
            run(["passwd", name])
            try:
                pwd.getpwnam(name)
            except KeyError as missing_user:
                raise SetupError(f"The Linux account was not created: {name}.") from missing_user
        run(["usermod", "-a", "-G", ADMIN_GROUP, name])
    print(f"Added {', '.join(users)} to OS group {ADMIN_GROUP}.")


def write_atomic(path, content, mode, backup=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent == SECRETS_DIR:
        os.chmod(SECRETS_DIR, 0o700)
    if backup and path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_name(path.name + f".backup-{stamp}")
        shutil.copy2(path, backup_path)
        os.chmod(backup_path, mode)
        print(f"Saved previous configuration as {backup_path}")
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apache_config(proxy_ip, certificate, private_key):
    session_key_path = SESSION_KEY_FILE.as_posix()
    return f"""<VirtualHost *:80>
    ServerName status.ubnetdef.org
    Redirect permanent / https://status.ubnetdef.org/
</VirtualHost>

<VirtualHost *:80>
    ServerName management.ubnetdef.org
    Redirect permanent / https://management.ubnetdef.org/
</VirtualHost>

<VirtualHost *:443>
    ServerName status.ubnetdef.org
    SSLEngine on
    SSLCertificateFile {certificate}
    SSLCertificateKeyFile {private_key}
    ProxyRequests Off
    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:8080/
    ProxyPassReverse / http://127.0.0.1:8080/
    <Location />
        Require all granted
    </Location>
</VirtualHost>

<VirtualHost *:443>
    ServerName management.ubnetdef.org
    SSLEngine on
    SSLCertificateFile {certificate}
    SSLCertificateKeyFile {private_key}
    ProxyRequests Off
    ProxyPreserveHost On
    RequestHeader unset Authorization
    ProxyPass / http://127.0.0.1:8081/
    ProxyPassReverse / http://127.0.0.1:8081/
    <Location />
        AuthType form
        AuthName "/"
        AuthFormProvider PAM
        AuthPAMService noc-management
        AuthFormLoginRequiredLocation /login
        Session On
        SessionMaxAge 28800
        SessionCookieName __Host-noc-management path=/;HttpOnly;Secure;SameSite=Strict
        SessionCryptoPassphraseFile {session_key_path}
        SessionCookieRemove On
        <RequireAll>
            Require ip {proxy_ip}
            Require valid-user
        </RequireAll>
    </Location>
    <Location /login>
        AuthType None
        <RequireAll>
            Require ip {proxy_ip}
        </RequireAll>
    </Location>
    <Location /dologin>
        SetHandler form-login-handler
        AuthType form
        AuthName "/"
        AuthFormProvider PAM
        AuthPAMService noc-management
        AuthFormLoginRequiredLocation /login?failed=1
        AuthFormLoginSuccessLocation /
        Session On
        SessionMaxAge 28800
        SessionCookieName __Host-noc-management path=/;HttpOnly;Secure;SameSite=Strict
        SessionCryptoPassphraseFile {session_key_path}
        <RequireAll>
            Require ip {proxy_ip}
        </RequireAll>
    </Location>
    <Location /logout>
        SetHandler form-logout-handler
        AuthType None
        AuthName "/"
        AuthFormLogoutLocation /login?logged_out=1
        Session On
        SessionMaxAge 1
        SessionCookieName __Host-noc-management path=/;HttpOnly;Secure;SameSite=Strict
        SessionCryptoPassphraseFile {session_key_path}
        <RequireAll>
            Require ip {proxy_ip}
        </RequireAll>
    </Location>
</VirtualHost>
"""


def pam_config():
    return (
        "auth       include      common-auth\n"
        f"account    required     pam_succeed_if.so user ingroup {ADMIN_GROUP}\n"
        "account    include      common-account\n"
    )


def ensure_session_key():
    if SESSION_KEY_FILE.is_symlink():
        raise SetupError(f"Refusing to use a symlinked session-key file: {SESSION_KEY_FILE}")
    if SESSION_KEY_FILE.exists():
        if not SESSION_KEY_FILE.is_file() or not SESSION_KEY_FILE.read_text(encoding="utf-8").strip():
            raise SetupError(f"The Apache session-key file is invalid: {SESSION_KEY_FILE}")
        os.chown(SESSION_KEY_FILE, 0, 0)
        os.chmod(SESSION_KEY_FILE, 0o600)
        return
    write_atomic(SESSION_KEY_FILE, secrets.token_urlsafe(64) + "\n", 0o600, backup=False)
    os.chown(SESSION_KEY_FILE, 0, 0)


def configure_apache():
    proxy_ip = validate_proxy_ip(prompt("Separate reverse-proxy server IP"))
    certificate = Path(prompt("Application-host TLS certificate/full-chain path")).expanduser()
    private_key = Path(prompt("Application-host TLS private-key path")).expanduser()
    for path in (certificate, private_key):
        if not path.is_file():
            raise SetupError(f"TLS file does not exist: {path}")
        if any(char in str(path) for char in ("\n", "\r", '"')):
            raise SetupError("TLS paths cannot contain quotes or line breaks.")
    add_admin_users()

    required_packages = ["apache2", "libapache2-mod-authnz-pam"]
    missing = []
    for package in required_packages:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", package],
            text=True, capture_output=True, check=False,
        )
        if result.returncode or "install ok installed" not in result.stdout:
            missing.append(package)
    if missing:
        print("Installing application-host reverse-proxy packages: " + ", ".join(missing))
        run(["apt-get", "update"])
        run(["apt-get", "install", "-y", *missing])
    ensure_session_key()
    run([
        "a2enmod", "ssl", "proxy", "proxy_http", "headers",
        "auth_form", "authnz_pam", "session", "session_cookie", "session_crypto",
    ])
    write_atomic(PAM_SERVICE, pam_config(), 0o644)

    old_site = APACHE_SITE.read_text(encoding="utf-8") if APACHE_SITE.exists() else None
    write_atomic(APACHE_SITE, apache_config(proxy_ip, certificate, private_key), 0o644)
    run(["a2ensite", "noc-dashboard.conf"])
    try:
        run(["apachectl", "configtest"])
    except SetupError:
        if old_site is None:
            APACHE_SITE.unlink(missing_ok=True)
            run(["a2dissite", "noc-dashboard.conf"])
        else:
            write_atomic(APACHE_SITE, old_site, 0o644, backup=False)
        raise
    run(["systemctl", "reload", "apache2"])


def ensure_dashboard_configs():
    configuration_dir = ROOT / "Configuration"
    example_path = configuration_dir / "dashboard.example.json"
    if not example_path.is_file():
        raise SetupError(f"Missing tracked dashboard template: {example_path}")
    template = example_path.read_text(encoding="utf-8")
    for name in ("status.json", "management.json"):
        path = configuration_dir / name
        if path.is_symlink():
            raise SetupError(f"Refusing to configure a symlinked dashboard file: {path}")
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(template)
            print(f"Created local configuration: {path}")
        if not path.is_file():
            raise SetupError(f"Dashboard configuration is not a regular file: {path}")
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SetupError(f"Dashboard configuration is not readable valid JSON: {path}") from error
        if (not isinstance(config, dict) or not isinstance(config.get("services", []), list)
                or not isinstance(config.get("devices", []), list)):
            raise SetupError(f"Dashboard configuration needs services and devices arrays: {path}")
        if name == "status.json" and config.get("devices"):
            raise SetupError("Keep the public status configuration's devices array empty.")
        os.chown(path, 65532, 65532)
        os.chmod(path, 0o400)


def local_interface_addresses():
    try:
        interfaces = json.loads(run(["ip", "-j", "address", "show"], capture=True))
    except json.JSONDecodeError as error:
        raise SetupError("Could not read local interface addresses for Docker Swarm.") from error
    addresses = set()
    for interface in interfaces:
        for item in interface.get("addr_info", []):
            try:
                address = ipaddress.ip_address(item.get("local", ""))
            except ValueError:
                continue
            if not address.is_loopback and not address.is_link_local:
                addresses.add(address.compressed)
    return sorted(addresses)


def initialize_swarm():
    state = run(["docker", "info", "--format", "{{.Swarm.LocalNodeState}}"], capture=True)
    if state != "active":
        if not confirm("Initialize a single-node Docker Swarm on this host", default=False):
            raise SetupError("Initialize or join a Swarm manager, then rerun this setup.")
        addresses = local_interface_addresses()
        if not addresses:
            raise SetupError("No non-loopback host IP was found for Docker Swarm.")
        print("Docker Swarm uses a host interface address; choose the LAN address used for cluster management, not a BMC address.")
        advertise_ip = prompt("Docker Swarm advertise IP (" + ", ".join(addresses) + ")")
        try:
            advertise_ip = ipaddress.ip_address(advertise_ip).compressed
        except ValueError as error:
            raise SetupError("Enter one of the host's listed IP addresses.") from error
        if advertise_ip not in addresses:
            raise SetupError("The Docker Swarm advertise IP must be assigned to this host.")
        run(["docker", "swarm", "init", "--advertise-addr", advertise_ip])
    manager = run(["docker", "info", "--format", "{{.Swarm.ControlAvailable}}"], capture=True)
    if manager.lower() != "true":
        raise SetupError("This Docker Swarm node is not a manager. Run setup on a Swarm manager.")
    node_id = run(["docker", "info", "--format", "{{.Swarm.NodeID}}"], capture=True)
    if not node_id:
        raise SetupError("Docker did not report this Swarm node's ID.")
    run(["docker", "node", "update", "--label-add", "noc_dashboard=true", node_id])


def install_credentials_and_deploy():
    ensure_dashboard_configs()
    initialize_swarm()

    profiles = collect_profiles()
    profile_names = sorted(json.loads(profiles)["profiles"])
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    secret_name = f"noc-device-credentials-{suffix}"
    secret_path = SECRETS_DIR / f"{secret_name}.json"
    write_atomic(secret_path, profiles, 0o600, backup=False)
    run(["docker", "secret", "create", secret_name, str(secret_path)])
    print(f"Saved source credentials in root-only file: {secret_path}")
    print("Enter these profile names in the device editor: " + ", ".join(profile_names))

    run(["docker", "compose", "build"], cwd=ROOT)
    env = os.environ.copy()
    env["NOC_CREDENTIAL_SECRET"] = secret_name
    run(["docker", "stack", "deploy", "-c", "deploy/swarm-stack.yaml", STACK_NAME], cwd=ROOT, env=env)
    print(f"Deployed stack {STACK_NAME} using Swarm secret {secret_name}.")
    print("Old versioned secrets are not removed automatically; keep one until rollback is no longer needed.")


def main():
    parser = argparse.ArgumentParser(description="Configure the NOC dashboard application host.")
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="configure the Apache/PAM login without changing Docker or device credentials",
    )
    args = parser.parse_args()
    require_supported_host()
    if not (ROOT / "deploy" / "swarm-stack.yaml").is_file():
        raise SetupError("Run this script from the checked-out NOC-Datacenter-Utility repository.")
    if args.auth_only:
        print("Configuring management-site OS-account sign-in only; Docker services and device credentials will not change.")
        configure_apache()
        print("Apache/PAM sign-in configured. Management remains restricted to the separate proxy and its VPN/private-network ACL.")
        return

    print("This wizard configures the Linux application host. It is safe to run over SSH.")
    print("The management container uses host networking and the host's route to the lights-out network.")
    print("No SSH tunnel is created; BMC polling uses Redfish, SNMP v2c, or IPMI with the profiles you enter.")
    bmc_ip = validate_management_ip(prompt("Example BMC/iDRAC/iLO IP to verify host routing"))
    _, route = discover_route(bmc_ip)
    print(f"Host route to {bmc_ip}: {route}")
    print("Swarm host networking will use this same Linux routing table.")
    if not confirm("Configure OS-user access, deploy containers, and configure the local Apache proxy"):
        print("No changes made.")
        return
    ensure_dashboard_configs()
    configure_apache()
    install_credentials_and_deploy()
    print("\nSetup finished.")
    print("Use your Linux username and password at https://management.ubnetdef.org.")
    print("Ensure the separate proxy preserves Host and Authorization and restricts management to VPN clients.")
    print("The management host firewall must allow only the external proxy to reach its HTTPS listener.")
    print("The management host also needs restricted outbound access to the private BMC network.")


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, KeyboardInterrupt) as error:
        print(f"\nSetup stopped: {error}", file=sys.stderr)
        sys.exit(1)
