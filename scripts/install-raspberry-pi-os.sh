#!/usr/bin/env bash
set -euo pipefail

INSTALL_DEPENDENCIES=false

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/install-raspberry-pi-os.sh [--install-dependencies]

By default, the installer does not run apt-get or install Python dependencies.
Use --install-dependencies for a first-time setup or dependency refresh.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dependencies)
      INSTALL_DEPENDENCIES=true
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root:" >&2
  echo "  sudo ./scripts/install-raspberry-pi-os.sh" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify the operating system (/etc/os-release is missing)." >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "raspbian" && "${ID:-}" != "debian" ]]; then
  echo "This installer targets Raspberry Pi OS; detected ${PRETTY_NAME:-unknown}." >&2
  exit 1
fi

case "${VERSION_CODENAME:-}" in
  bookworm|trixie) ;;
  *)
    echo "Raspberry Pi OS Bookworm or Trixie is required." >&2
    echo "Legacy Bullseye uses dhcpcd by default and is not supported." >&2
    exit 1
    ;;
esac

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/towelbar-agent"
CONFIG_DIR="/etc/towelbar-agent"
STATE_DIR="/var/lib/towelbar-agent"

if [[ "${INSTALL_DEPENDENCIES}" == true ]]; then
  apt-get update
  apt-get install -y network-manager polkitd python3-venv ripgrep
else
  echo "Skipping OS and Python dependency installation."
  echo "Use --install-dependencies to enable it."
fi

if ! systemctl is-active --quiet NetworkManager.service; then
  echo "NetworkManager is installed but is not active." >&2
  echo "Enable it before installing the agent." >&2
  exit 1
fi

if ! id towelbar >/dev/null 2>&1; then
  useradd --system --home-dir "${STATE_DIR}" --shell /usr/sbin/nologin towelbar
fi

install -d -o root -g root -m 0755 "${INSTALL_DIR}" "${CONFIG_DIR}"
install -d -o towelbar -g towelbar -m 0750 "${STATE_DIR}"
install -d -o root -g root -m 0755 /etc/polkit-1/rules.d

python3 -m venv "${INSTALL_DIR}/venv"
if [[ "${INSTALL_DEPENDENCIES}" == true ]]; then
  "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
  "${INSTALL_DIR}/venv/bin/pip" install --force-reinstall "${SOURCE_DIR}[mcp]"
else
  if ! "${INSTALL_DIR}/venv/bin/python" -c \
    'import httpx, paho.mqtt.client, yaml, mcp' 2>/dev/null; then
    echo "Required Python dependencies are missing from the existing environment." >&2
    echo "Re-run with --install-dependencies for a first-time installation." >&2
    exit 1
  fi
  PURELIB="$("${INSTALL_DIR}/venv/bin/python" -c \
    'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  install -d -o root -g root -m 0755 "${PURELIB}/towelbar_agent"
  cp -a "${SOURCE_DIR}/src/towelbar_agent/." "${PURELIB}/towelbar_agent/"
  install -o root -g root -m 0755 \
    "${SOURCE_DIR}/deploy/towelbar-agent" \
    "${INSTALL_DIR}/venv/bin/towelbar-agent"
  install -o root -g root -m 0755 \
    "${SOURCE_DIR}/deploy/towelbar-mcp" \
    "${INSTALL_DIR}/venv/bin/towelbar-mcp"
fi

install -o root -g root -m 0644 \
  "${SOURCE_DIR}/deploy/towelbar-agent.service" \
  /etc/systemd/system/towelbar-agent.service
install -o root -g root -m 0644 \
  "${SOURCE_DIR}/deploy/90-towelbar-agent.rules" \
  /etc/polkit-1/rules.d/90-towelbar-agent.rules

if [[ ! -e "${CONFIG_DIR}/config.yaml" ]]; then
  install -o root -g towelbar -m 0640 \
    "${SOURCE_DIR}/config.example.yaml" \
    "${CONFIG_DIR}/config.yaml"
fi

systemctl daemon-reload
echo "Installed on ${PRETTY_NAME}. Edit ${CONFIG_DIR}/config.yaml, then run:"
echo "  sudo systemctl enable --now towelbar-agent"
