#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer as root: sudo bash deploy/setup.sh" >&2
    exit 1
fi

if [[ ! -r /etc/os-release ]]; then
    echo "This installer supports Debian or Ubuntu Linux hosts." >&2
    exit 1
fi
. /etc/os-release
case "${ID:-}" in
    debian|ubuntu) ;;
    *)
        echo "This installer supports Debian or Ubuntu Linux hosts." >&2
        exit 1
        ;;
esac

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y adduser ca-certificates curl git python3

install_docker_repository() {
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$(dpkg --print-architecture)" "${ID}" "${VERSION_CODENAME}" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
}

if ! command -v docker >/dev/null 2>&1; then
    install_docker_repository
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker
fi

if ! docker info >/dev/null 2>&1; then
    echo "Docker is installed but its daemon is unavailable." >&2
    exit 1
fi

docker_version="$(docker version --format '{{.Server.Version}}')"
python3 - "${docker_version}" <<'PY'
import re
import sys

match = re.match(r"^(\d+)\.(\d+)\.(\d+)", sys.argv[1])
if not match or tuple(map(int, match.groups())) < (28, 0, 0):
    print("Docker Engine 28.0.0 or newer is required.", file=sys.stderr)
    raise SystemExit(1)
PY

if ! docker compose version >/dev/null 2>&1; then
    install_docker_repository
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin
fi

source_repo=""
script_path="${BASH_SOURCE[0]:-}"
if [[ -n "${script_path}" && -f "${script_path}" ]]; then
    script_dir="$(cd -- "$(dirname -- "${script_path}")" && pwd)"
    candidate="$(cd -- "${script_dir}/.." && pwd)"
    if [[ -f "${candidate}/deploy/setup_wizard.py" ]]; then
        source_repo="${candidate}"
    fi
fi
if [[ -z "${source_repo}" && -f "${PWD}/deploy/setup_wizard.py" ]]; then
    source_repo="$(pwd)"
fi

if [[ -z "${source_repo}" ]]; then
    install_dir="${NOC_INSTALL_DIR:-/opt/noc-datacenter-utility}"
    repo_url="${NOC_REPO_URL:-https://github.com/ubnetdef/NOC-Datacenter-Utility.git}"
    repo_branch="${NOC_REPO_BRANCH:-main}"
    if [[ -d "${install_dir}/.git" ]]; then
        source_repo="${install_dir}"
    elif [[ -e "${install_dir}" ]]; then
        if [[ -d "${install_dir}" && -z "$(find "${install_dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
            rmdir "${install_dir}"
        else
            echo "Install path exists and is not an NOC repository: ${install_dir}" >&2
            exit 1
        fi
    fi
    if [[ -z "${source_repo}" ]]; then
        git clone --branch "${repo_branch}" "${repo_url}" "${install_dir}"
        source_repo="${install_dir}"
    fi
fi

source_repo="$(cd -- "${source_repo}" && pwd)"
if [[ ! -f "${source_repo}/deploy/setup_wizard.py" ]]; then
    echo "The checkout is missing deploy/setup_wizard.py: ${source_repo}" >&2
    echo "Use a complete NOC-Datacenter-Utility repository checkout." >&2
    exit 1
fi

if git -C "${source_repo}" rev-list --all --objects | grep -q ' Configuration/config\.json$'; then
    echo "Refusing to install: Git history still contains Configuration/config.json." >&2
    echo "Remove that file from all remote history and revoke any old webhook before retrying." >&2
    exit 1
fi

cd "${source_repo}"
exec python3 "${source_repo}/deploy/setup_wizard.py" "$@"
