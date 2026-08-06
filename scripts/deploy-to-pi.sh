#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="${SOURCE_DIR}/towelbar-agent-raspberry-pi.zip"

if ! command -v zip >/dev/null 2>&1; then
  echo "The local 'zip' command is required." >&2
  exit 1
fi

echo "Building installer archive from the current workspace..."
TEMP_DIR="$(mktemp -d /tmp/towelbar-agent.XXXXXX)"
TEMP_ARCHIVE="${TEMP_DIR}/towelbar-agent-raspberry-pi.zip"
cleanup() {
  rm -f "${TEMP_ARCHIVE}"
  rmdir "${TEMP_DIR}" 2>/dev/null || true
}
trap cleanup EXIT
(
  cd "${SOURCE_DIR}"
  zip -qr "${TEMP_ARCHIVE}" \
    .gitignore README.md config.example.yaml pyproject.toml deploy scripts src tests \
    -x '*/__pycache__/*' '*.pyc' '.pytest_cache/*' \
       'src/*.egg-info/*' 'src/*.egg-info/'
)
mv "${TEMP_ARCHIVE}" "${ARCHIVE}"

read -r -p "Raspberry Pi hostname or IP: " PI_HOST
if [[ -z "${PI_HOST}" ]]; then
  echo "A hostname or IP is required." >&2
  exit 2
fi
read -r -p "SSH user [pi]: " PI_USER
PI_USER="${PI_USER:-pi}"
if [[ ! "${PI_HOST}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "Hostname contains unsupported characters." >&2
  exit 2
fi
if [[ ! "${PI_USER}" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  echo "SSH username contains unsupported characters." >&2
  exit 2
fi
read -r -p "Install/refresh dependencies? [y/N]: " INSTALL_DEPS

REMOTE_ARCHIVE="/tmp/towelbar-agent-raspberry-pi.zip"
INSTALL_FLAG=""
if [[ "${INSTALL_DEPS}" =~ ^[Yy]$ ]]; then
  INSTALL_FLAG="--install-dependencies"
fi

echo "Uploading installer to ${PI_USER}@${PI_HOST}..."
echo "SSH and sudo will prompt interactively if passwords are required."
scp "${ARCHIVE}" "${PI_USER}@${PI_HOST}:${REMOTE_ARCHIVE}"

ssh -tt "${PI_USER}@${PI_HOST}" \
  "deploy_dir=\$(mktemp -d /tmp/towelbar-deploy.XXXXXX) && \
   python3 -m zipfile -e '${REMOTE_ARCHIVE}' \"\${deploy_dir}\" && \
   sudo bash \"\${deploy_dir}/scripts/install-raspberry-pi-os.sh\" ${INSTALL_FLAG} && \
   sudo systemctl restart towelbar-agent && \
   sudo systemctl --no-pager --full status towelbar-agent"

echo "Deployment complete. Reconnect the towelbar-discovery MCP server if it is running."
