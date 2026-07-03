#!/usr/bin/env bash
# setup.sh — Install / uninstall / status the tc-limit daemon.
#
# Usage:
#   sudo bash setup.sh install   [--no-start]
#   sudo bash setup.sh uninstall
#   sudo bash setup.sh status

set -euo pipefail

APP_NAME="tc-limit"
INSTALL_DIR="/opt/${APP_NAME}"
SRC_DIR="${INSTALL_DIR}/src"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_DIR="/etc/${APP_NAME}"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
STATE_DIR="/run/${APP_NAME}"
BIN_LINK="/usr/local/bin/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
NO_START=false
TEMPLATE="default"

# ── Helpers ──────────────────────────────────────────────────────────────

log()  { echo "[setup] $*"; }
err()  { echo "[setup] ERROR: $*" >&2; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        err "This script must be run as root (use sudo)"
    fi
}

# ── Systemd unit ─────────────────────────────────────────────────────────

write_service_file() {
    log "Writing systemd unit to ${SERVICE_FILE}"
    cat > "${SERVICE_FILE}" <<'UNIT'
[Unit]
Description=Smart Bandwidth Limit Daemon (tc)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
RuntimeDirectory=tc-limit
StateDirectory=tc-limit
ExecStart=/usr/local/bin/tc-limit daemon --config /etc/tc-limit/config.yaml
ExecReload=/bin/kill -HUP $MAINPID
ExecStop=/usr/local/bin/tc-limit stop --config /etc/tc-limit/config.yaml
Restart=always
RestartSec=5

ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/run/tc-limit /etc/tc-limit /var/lib/tc-limit
NoNewPrivileges=true

StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
}

# ── Install ──────────────────────────────────────────────────────────────

do_install() {
    local SCRIPT_DIR
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

    # 1. Copy source
    log "Copying source to ${SRC_DIR}"
    mkdir -p "${SRC_DIR}"
    cp -r "${SCRIPT_DIR}/pyproject.toml" "${SRC_DIR}/"
    cp -r "${SCRIPT_DIR}/src" "${SRC_DIR}/"
    # Also copy the config example + templates (only used if config dir doesn't exist yet)
    cp -r "${SCRIPT_DIR}/config.example.yaml" "${SRC_DIR}/"

    # 2. Create venv
    log "Creating virtual environment at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"

    # 3. Install package
    log "Installing tc-limit into venv"
    "${VENV_DIR}/bin/pip" install --quiet "${SRC_DIR}"

    # 4. Config directory
    log "Setting up config directory ${CONFIG_DIR}"
    if [[ ! -f "${CONFIG_FILE}" ]]; then
        mkdir -p "${CONFIG_DIR}"
        if [[ "${TEMPLATE}" != "default" ]]; then
            log "  Generating config from template: ${TEMPLATE}"
            "${VENV_DIR}/bin/python3" -c "
from tc_limit.templates import generate_yaml
print(generate_yaml('${TEMPLATE}'), end='')
" > "${CONFIG_FILE}"
        else
            cp "${SCRIPT_DIR}/config.example.yaml" "${CONFIG_FILE}"
        fi
        log "  Created ${CONFIG_FILE} (edit to customize)"
    else
        log "  ${CONFIG_FILE} already exists — skipped"
    fi

    # 5. Runtime / state directories
    mkdir -p "${STATE_DIR}"
    mkdir -p "/var/lib/${APP_NAME}"

    # 6. Symlink
    log "Creating symlink ${BIN_LINK} → ${VENV_DIR}/bin/${APP_NAME}"
    ln -sf "${VENV_DIR}/bin/${APP_NAME}" "${BIN_LINK}"

    # 7. systemd
    write_service_file
    systemctl daemon-reload
    systemctl enable "${APP_NAME}"

    # 8. Start (unless --no-start)
    if $NO_START; then
        log "Skipping start (--no-start)"
    else
        log "Starting ${APP_NAME}"
        systemctl start "${APP_NAME}"
        log "Status:"
        systemctl status --no-pager "${APP_NAME}" || true
    fi

    log "Install complete."
    log "  Usage:  ${APP_NAME} status"
    log "  Config: ${CONFIG_FILE}"
}

# ── Uninstall ────────────────────────────────────────────────────────────

do_uninstall() {
    # Stop & disable
    if systemctl is-active --quiet "${APP_NAME}" 2>/dev/null; then
        log "Stopping ${APP_NAME}"
        systemctl stop "${APP_NAME}"
    fi
    if systemctl is-enabled --quiet "${APP_NAME}" 2>/dev/null; then
        log "Disabling ${APP_NAME}"
        systemctl disable "${APP_NAME}"
    fi

    # Remove files
    log "Removing systemd unit"
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload || true

    log "Removing symlink ${BIN_LINK}"
    rm -f "${BIN_LINK}"

    log "Removing installation directory ${INSTALL_DIR}"
    rm -rf "${INSTALL_DIR}"

    # Prompt for config / runtime dirs
    if [[ -d "${CONFIG_DIR}" ]]; then
        read -rp "Remove config directory ${CONFIG_DIR}? [y/N] " answer
        if [[ "$answer" =~ ^[Yy]$ ]]; then
            rm -rf "${CONFIG_DIR}"
            log "  ${CONFIG_DIR} removed"
        fi
    fi

    if [[ -d "${STATE_DIR}" ]]; then
        read -rp "Remove runtime directory ${STATE_DIR}? [y/N] " answer
        if [[ "$answer" =~ ^[Yy]$ ]]; then
            rm -rf "${STATE_DIR}"
            log "  ${STATE_DIR} removed"
        fi
    fi

    log "Uninstall complete."
}

# ── Status ───────────────────────────────────────────────────────────────

do_status() {
    if systemctl is-active --quiet "${APP_NAME}" 2>/dev/null; then
        echo "Service: active"
    else
        echo "Service: inactive"
    fi

    if [[ -x "${BIN_LINK}" ]]; then
        echo "Binary:  ${BIN_LINK}"
    else
        echo "Binary:  (not installed)"
    fi

    if [[ -f "${CONFIG_FILE}" ]]; then
        echo "Config:  ${CONFIG_FILE}"
    else
        echo "Config:  (not found)"
    fi
}

# ── Main ─────────────────────────────────────────────────────────────────

main() {
    case "${1:-}" in
        install)
            require_root
            while [[ $# -gt 1 ]]; do
                case "$2" in
                    --no-start) NO_START=true; shift ;;
                    --template) TEMPLATE="$3"; shift 2 ;;
                    *) shift ;;
                esac
            done
            do_install
            ;;
        uninstall)
            require_root
            do_uninstall
            ;;
        status)
            do_status
            ;;
        *)
            echo "Usage: $0 {install|uninstall|status}"
            exit 1
            ;;
    esac
}

main "$@"
