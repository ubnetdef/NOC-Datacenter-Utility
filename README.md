# UBNetDef infrastructure dashboards

Two Docker services provide a public website-status page and a private
infrastructure management dashboard. The management hostname uses a branded
Apache/PAM sign-in page backed by authorized Linux accounts and must remain
restricted to the VPN/private network.

## Install

On a clean Debian/Ubuntu application host that can reach the BMC/light-out
network, install the small download prerequisites and run the installer:

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl
curl -fsSLo /tmp/noc-setup.sh \
  https://raw.githubusercontent.com/ubnetdef/NOC-Datacenter-Utility/main/deploy/setup.sh
sudo bash /tmp/noc-setup.sh
sudo rm -f /tmp/noc-setup.sh
```

The installer installs Docker Engine 28+ and Compose from Docker's official
Debian/Ubuntu repository, clones the project to
`/opt/noc-datacenter-utility`, and launches the interactive setup wizard. It
refuses repositories whose history still contains the legacy
`Configuration/config.json`; revoke any old webhook and clean the remote history
before installing.

The wizard checks a route to an example private BMC IP, creates missing local
configuration files, sets their container-readable permissions, and asks for:

- Management usernames to authorize. The wizard can create missing Linux
  accounts and prompt you to set their passwords; the web login uses those OS
  credentials.
- The separate reverse-proxy IP and the application host's TLS certificate/key.
- Read-only Redfish, SNMP v2c, or IPMI credential profiles. Passwords and
  communities are entered without echo and stored in a root-only file under
  `/etc/noc-datacenter-utility/`; a versioned Docker Swarm secret is mounted
  only in the management container.

To run from an already-cloned checkout, use `sudo bash deploy/setup.sh`.

The branded sign-in page is backed by PAM and the `noc-admin` group. Successful
logins use an encrypted, secure session cookie that expires after eight hours;
the management pages include a sign-out link.

Enter the profile name printed by the wizard, plus the private BMC IP, in each
device's editor. Never enter device passwords in the UI or commit them.

The management host uses its own Linux route to reach BMCs. The Swarm services
use host networking and are pinned to the host that passed the route check.
SSH is for administering that host; no SSH tunnel or device-side SSH is used.
The wizard does not configure network interfaces, routes, or firewalls.

The wizard configures Apache/PAM on the application host, **not** the separate
public reverse proxy. Configure that proxy to:

- Route both hostnames to the application host over verified HTTPS, preserving
  the original `Host` and session cookies.
- Allow management traffic only from VPN/private clients and apply rate limits.
- Restrict application-host HTTPS access to the proxy. The application host
  sees the proxy's IP, not the original client IP.

Use a certificate valid for both hostnames. Do not expose port 8081 or the
management container directly. Local loopback access is not authenticated;
trusted host users and Docker administrators can reach it. The application-host
Apache config allows only the separate proxy IP; the external proxy must enforce
the client-network restriction before forwarding requests. The login page does
not replace that network gate.

## Operation

- Status: `https://status.ubnetdef.org`
- Management: `https://management.ubnetdef.org` (encrypted session after Linux-account login)
- Open **Rack designer** from management to add devices, configure ports, and
  edit front/rear rack views.
- Discovery uses read-only Redfish, SNMP v2c, or IPMI against one saved private
  address. Available identity, CPU/memory, drive, PSU, and interface details
  are filled into editable fields; missing data is listed for manual entry.
- Successful discovery enrolls a 30-second heartbeat. UI data refreshes every
  five seconds. Network, hardware, resource, and service alarms clear when
  subsequent checks report recovery; resolved alarms remain in history.
- Telemetry depends on what each vendor/protocol reports. CPU/memory thresholds
  are not invented. SNMP v2c is unencrypted; use it only on a trusted network.
- Catalog drawings are schematic, not guaranteed service-tag-accurate. Confirm
  the actual chassis, drive-bay variant, cable layout, and optional hardware.

## Configuration and data

`Configuration/dashboard.example.json` is copied to ignored local files:
`Configuration/status.json` and `Configuration/management.json`. Keep the
public status file free of device inventory; the setup wizard rejects devices
there. The management UI stores rack inventory in the Docker `rack_data` volume.
Back up this private volume consistently and do not run `docker compose down -v`
unless you intend to delete its data.

Website checks run every 60 seconds by default; device polling runs every 30
seconds; the UI refreshes every five seconds. Configure services in the local
JSON files. Add and maintain physical device inventory in the management UI.

## Update an installed Swarm deployment

Run on the application host from a clean Git branch with an upstream:

```sh
sudo bash deploy/update.sh
```

The script fast-forwards the checkout, rebuilds images, redeploys using the
currently mounted credential secret, forces service restarts, and checks both
local APIs. If it finds the old managed Apache Basic Auth config, it prompts to
migrate the vhost to the sign-in page without rotating device credentials.
It does not replace configuration or rack data. Keep old versioned secrets
until rollback is no longer needed.

To migrate only the Apache/PAM login without rebuilding or changing device
credentials, run `sudo bash deploy/setup.sh --auth-only`.

## Local development

```sh
cp Configuration/dashboard.example.json Configuration/status.json
cp Configuration/dashboard.example.json Configuration/management.json
docker compose up -d --build
```

The local Compose ports bind to loopback (`8080` and `8081`). Compose does not
mount the Swarm credential secret or configure Apache/PAM. Its management
container enables a test-only login (`admin` / `admin`) so the sign-in and
sign-out flow can be exercised locally. These fixed credentials are for local
testing only. The ports are loopback-only by default; never expose this test
deployment publicly or reuse these credentials.

Real device discovery and OS-account authentication require the Linux setup
above. The Swarm production stack does not enable the test credentials.

Run tests with:

```sh
python -m unittest discover -s tests
node --check web/rack.js
node --check web/management.js
```
