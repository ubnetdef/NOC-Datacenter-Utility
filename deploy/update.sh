#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
stack_name="${NOC_STACK_NAME:-noc}"

if [[ ! "${stack_name}" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    echo "Invalid NOC_STACK_NAME." >&2
    exit 1
fi

cd "${repo_root}"

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Refusing to update with local Git changes. Commit or save them first." >&2
    exit 1
fi

upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
if [[ -z "${upstream}" ]]; then
    echo "This branch has no upstream. Configure its Git remote before updating." >&2
    exit 1
fi
git pull --ff-only

if [[ "$(docker info --format '{{.Swarm.LocalNodeState}}')" != "active" ]]; then
    echo "Docker Swarm is not active on this host." >&2
    exit 1
fi
if [[ "$(docker info --format '{{.Swarm.ControlAvailable}}')" != "true" ]]; then
    echo "Run updates on a Docker Swarm manager." >&2
    exit 1
fi

node_id="$(docker info --format '{{.Swarm.NodeID}}')"
if [[ "$(docker node inspect "${node_id}" --format '{{ index .Spec.Labels "noc_dashboard" }}')" != "true" ]]; then
    echo "This node is not labeled for the dashboard stack (noc_dashboard=true)." >&2
    exit 1
fi

management_service="${stack_name}_management"
docker service inspect "${management_service}" >/dev/null
mapfile -t active_secrets < <(
    docker service inspect "${management_service}" \
        --format '{{range .Spec.TaskTemplate.ContainerSpec.Secrets}}{{println .SecretName}}{{end}}'
)
if [[ "${#active_secrets[@]}" -ne 1 || -z "${active_secrets[0]}" ]]; then
    echo "Could not identify the active device-credentials secret; refusing to replace it." >&2
    exit 1
fi

docker compose build
NOC_CREDENTIAL_SECRET="${active_secrets[0]}" \
    docker stack deploy --compose-file deploy/swarm-stack.yaml "${stack_name}"

docker service update --force "${stack_name}_management"
docker service update --force "${stack_name}_status"

apache_site="/etc/apache2/sites-available/noc-dashboard.conf"
if [[ -f "${apache_site}" ]] && grep -Fq "AuthType Basic" "${apache_site}"; then
    echo "Migrating the application-host Apache login from Basic Auth to the management sign-in page."
    if [[ "${EUID}" -eq 0 ]]; then
        bash deploy/setup.sh --auth-only
    else
        sudo bash deploy/setup.sh --auth-only
    fi
elif [[ ! -f "${apache_site}" ]]; then
    echo "No managed Apache vhost found; verify your application-host auth and proxy configuration manually."
fi

python3 - <<'PY'
import time
import urllib.error
import urllib.request

targets = (
    "http://127.0.0.1:8080/api/status",
    "http://127.0.0.1:8081/api/racks",
)
deadline = time.monotonic() + 90
last_error = None
while time.monotonic() < deadline:
    try:
        for target in targets:
            with urllib.request.urlopen(target, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"{target} returned HTTP {response.status}")
        print("Status and management APIs are responding.")
        break
    except (OSError, urllib.error.URLError, RuntimeError) as error:
        last_error = error
        time.sleep(3)
else:
    raise SystemExit(f"Dashboard update did not become ready: {last_error}")
PY

echo "Dashboard update complete. Inventory, configuration, and credential secrets were preserved."
