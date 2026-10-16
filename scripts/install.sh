#!/usr/bin/env bash
# =============================================================================
# MDMPatcher-Linux — Automated Cross-Distro Installer (Fedora, Ubuntu, Arch, openSUSE)
# Supports:
#   - Fedora Linux 39 / 40 / 41 / 42 / 43 / Rawhide (dnf & rpm-ostree / Silverblue)
#   - RHEL / CentOS Stream / AlmaLinux / Rocky Linux (dnf)
#   - Ubuntu / Debian / Linux Mint / Pop!_OS / Kali Linux (apt)
#   - Arch Linux / Manjaro / EndeavourOS / CachyOS (pacman)
#   - openSUSE Tumbleweed / Leap (zypper)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
RESET="\033[0m"

info()  { echo -e "${CYAN}[*]${RESET} $*"; }
ok()    { echo -e "${GREEN}[+]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
err()   { echo -e "${RED}[✗]${RESET} $*" >&2; }

WITH_VENV=0
WITH_C_BUILD=1
SKIP_UDEV=0

for arg in "$@"; do
    case "$arg" in
        --venv)       WITH_VENV=1 ;;
        --no-c-build) WITH_C_BUILD=0 ;;
        --skip-udev)  SKIP_UDEV=1 ;;
        -h|--help)
            echo "Usage: ./scripts/install.sh [--venv] [--no-c-build] [--skip-udev]"
            echo ""
            echo "Options:"
            echo "  --venv        Create a Python virtual environment (.venv) and install pymobiledevice3"
            echo "  --no-c-build  Skip compiling the optional native C binary (src/c/mdm_patch)"
            echo "  --skip-udev   Skip installing /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules"
            exit 0
            ;;
    esac
done

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        warn "Running as non-root and 'sudo' was not found; system package installation may fail."
    fi
fi

echo -e "${BOLD}=====================================================================${RESET}"
echo -e "${BOLD}  MDMPatcher-Linux v2.8.0 — Official Linux Installer (iOS 9 – 26.x)${RESET}"
echo -e "${BOLD}=====================================================================${RESET}"

DISTRO_ID="unknown"
DISTRO_LIKE=""
if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    DISTRO_ID="${ID:-unknown}"
    DISTRO_LIKE="${ID_LIKE:-}"
fi

info "Detected Linux distribution: ${BOLD}${PRETTY_NAME:-$DISTRO_ID}${RESET}"

install_fedora() {
    info "Installing Fedora / RHEL packages via dnf..."
    if command -v rpm-ostree >/dev/null 2>&1 && [[ -f /run/ostree-booted ]]; then
        info "Detected Fedora Atomic / Silverblue / Kinoite (rpm-ostree)..."
        $SUDO rpm-ostree install --idempotent --allow-inactive \
            usbmuxd libimobiledevice libimobiledevice-utils python3 python3-tkinter openssl
    else
        $SUDO dnf install -y \
            usbmuxd \
            libimobiledevice \
            libimobiledevice-utils \
            libplist \
            openssl \
            openssl-libs \
            python3 \
            python3-pip \
            python3-tkinter \
            gcc \
            make \
            pkgconf-pkg-config \
            libimobiledevice-devel \
            libimobiledevice-glue-devel \
            libplist-devel \
            openssl-devel \
            libzip-devel \
            readline-devel || {
                warn "Some optional -devel packages were unavailable; installing core runtime packages..."
                $SUDO dnf install -y usbmuxd libimobiledevice libimobiledevice-utils python3 python3-pip python3-tkinter openssl
            }
    fi
}

install_debian_ubuntu() {
    info "Installing Ubuntu / Debian packages via apt..."
    $SUDO apt-get update -y
    $SUDO apt-get install -y \
        usbmuxd \
        libimobiledevice6 \
        libimobiledevice-utils \
        ideviceinstaller \
        libplist-utils \
        openssl \
        python3 \
        python3-pip \
        python3-venv \
        python3-tk \
        build-essential \
        pkg-config \
        libimobiledevice-dev \
        libplist-dev \
        libssl-dev \
        libzip-dev \
        libreadline-dev || {
            warn "Falling back to core runtime packages..."
            $SUDO apt-get install -y usbmuxd libimobiledevice-utils python3 python3-pip python3-venv python3-tk openssl
        }
}

install_arch() {
    info "Installing Arch Linux packages via pacman..."
    $SUDO pacman -Sy --needed --noconfirm \
        usbmuxd \
        libimobiledevice \
        libplist \
        openssl \
        libzip \
        readline \
        python \
        python-pip \
        tk \
        base-devel \
        pkgconf
}

install_opensuse() {
    info "Installing openSUSE packages via zypper..."
    $SUDO zypper install -y \
        usbmuxd \
        libimobiledevice-tools \
        python3 \
        python3-pip \
        python3-tk \
        gcc \
        make \
        pkg-config \
        libimobiledevice-devel \
        libplist-devel \
        libopenssl-devel \
        libzip-devel \
        readline-devel
}

case "$DISTRO_ID" in
    fedora|rhel|centos|almalinux|rocky|nobara|ultramarine|bazzite)
        install_fedora
        ;;
    ubuntu|debian|linuxmint|pop|kali|elementary|zorin|raspbian)
        install_debian_ubuntu
        ;;
    arch|manjaro|endeavouros|cachyos|garuda|artix)
        install_arch
        ;;
    opensuse*|sles)
        install_opensuse
        ;;
    *)
        if [[ "$DISTRO_LIKE" == *"fedora"* || "$DISTRO_LIKE" == *"rhel"* ]]; then
            install_fedora
        elif [[ "$DISTRO_LIKE" == *"debian"* || "$DISTRO_LIKE" == *"ubuntu"* ]]; then
            install_debian_ubuntu
        elif [[ "$DISTRO_LIKE" == *"arch"* ]]; then
            install_arch
        elif [[ "$DISTRO_LIKE" == *"suse"* ]]; then
            install_opensuse
        else
            warn "Unrecognized distro '$DISTRO_ID'. Please ensure usbmuxd, libimobiledevice, python3, and openssl are installed."
        fi
        ;;
esac

if [[ "$SKIP_UDEV" -eq 0 && -d /etc/udev/rules.d ]]; then
    info "Installing Apple USB udev rules to /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules..."
    $SUDO cp "${SCRIPT_DIR}/39-usbmuxd-mdmpatcher.rules" /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules
    $SUDO chmod 644 /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules
    if command -v udevadm >/dev/null 2>&1; then
        $SUDO udevadm control --reload-rules || true
        $SUDO udevadm trigger --subsystem-match=usb --attr-match=idVendor=05ac || true
    fi
    ok "Apple USB udev rules installed and reloaded."
fi

if command -v systemctl >/dev/null 2>&1; then
    info "Ensuring usbmuxd systemd service is active..."
    $SUDO systemctl start usbmuxd.service 2>/dev/null || true
fi

if [[ "$WITH_VENV" -eq 1 ]]; then
    info "Creating Python virtual environment at ${REPO_ROOT}/.venv (for iOS 26.x MobileConfigService support)..."
    python3 -m venv "${REPO_ROOT}/.venv"
    "${REPO_ROOT}/.venv/bin/pip" install --upgrade pip
    "${REPO_ROOT}/.venv/bin/pip" install -e "${REPO_ROOT}[usb]"
    ok "Virtual environment ready: source .venv/bin/activate"
fi

if [[ "$WITH_C_BUILD" -eq 1 ]]; then
    info "Attempting to build native C binary (src/c/mdm_patch)..."
    if make -C "${REPO_ROOT}/src/c" >/dev/null 2>&1; then
        ok "Built native C binary: ${REPO_ROOT}/src/c/mdm_patch"
    else
        warn "Native C binary build skipped (optional; Python CLI './mdmpatcher.py' is ready out of the box)."
    fi
fi

chmod +x "${REPO_ROOT}/mdmpatcher.py"
echo ""
ok "${BOLD}Installation complete!${RESET}"
echo "  Run './mdmpatcher.py info' to verify device detection, or './mdmpatcher.py patch' to patch MDM."
