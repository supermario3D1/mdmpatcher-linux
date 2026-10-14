#!/usr/bin/env bash
# =============================================================================
# MDMPatcher-Linux — One-Command Installer & Bootstrapper (iOS 9 – 27+)
#
#   QUICK START (no manual file copying, nothing to download by hand):
#
#     curl -fsSL https://raw.githubusercontent.com/supermario3D1/mdmpatcher-linux/main/scripts/install.sh | bash
#
#   ...then, from any directory:
#
#     mdmpatcher patch
#
# Works in two modes:
#   1. Bootstrap mode (piped via curl / run with no checkout next to it):
#      git-clones this repository from the `main` branch into the install
#      directory and re-runs itself from the fresh clone.
#   2. In-place mode (`./scripts/install.sh` inside an existing checkout):
#      installs from the checkout you already have.
#
# What it sets up:
#   - distro packages (usbmuxd, libimobiledevice, openssl, python3, tkinter…)
#   - /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules (non-root iPhone access)
#   - usbmuxd systemd service
#   - a Python virtual environment (.venv) with pymobiledevice3
#   - a `mdmpatcher` launcher on your PATH (no `cd`, no `source .venv/...`)
#   - the optional native C binary (src/c/mdm_patch)
#
# Supported: Fedora / RHEL / Nobara / Bazzite (dnf, rpm-ostree), Ubuntu / Debian
#            / Mint / Pop!_OS / Kali (apt), Arch / Manjaro / CachyOS (pacman),
#            openSUSE Tumbleweed / Leap (zypper)
# =============================================================================

set -euo pipefail

APP_VERSION="2.7.0"
LAUNCHER_NAME="mdmpatcher"

# ---------------------------------------------------------------------------
# Defaults (every one of these can be overridden by a flag or env var)
# ---------------------------------------------------------------------------
REPO_URL="${MDM_REPO_URL:-https://github.com/supermario3D1/mdmpatcher-linux.git}"
BRANCH="${MDM_BRANCH:-main}"
INSTALL_DIR="${MDM_INSTALL_DIR:-}"
BIN_DIR="${MDM_BIN_DIR:-}"
WITH_VENV=1
WITH_C_BUILD=1
SKIP_UDEV=0
SKIP_PACKAGES="${MDM_SKIP_PACKAGES:-0}"
DO_UNINSTALL=0
PURGE=0

BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
RESET="\033[0m"

info() { echo -e "${CYAN}[*]${RESET} $*"; }
ok()   { echo -e "${GREEN}[+]${RESET} $*"; }
warn() { echo -e "${YELLOW}[!]${RESET} $*"; }
err()  { echo -e "${RED}[✗]${RESET} $*" >&2; }

usage() {
    cat <<'USAGE'
MDMPatcher-Linux installer — clones/updates this tool from GitHub `main` and
installs a `mdmpatcher` command on your PATH.

QUICK INSTALL (copy & paste, nothing to download manually):
  curl -fsSL https://raw.githubusercontent.com/supermario3D1/mdmpatcher-linux/main/scripts/install.sh | bash

OR (if you already cloned the repo):
  git clone https://github.com/supermario3D1/mdmpatcher-linux.git
  cd mdmpatcher-linux
  ./scripts/install.sh

USAGE:
  ./scripts/install.sh [options]

OPTIONS:
  --dir DIR          Directory to install into (default: $HOME/mdmpatcher-linux,
                     /opt/mdmpatcher-linux when run as root, or the current
                     checkout when run in place)
  --branch BRANCH    Git branch to clone/track (default: main)
  --repo URL         Git repository URL (default: official GitHub repo)
  --venv             Create a .venv with pymobiledevice3 (default: enabled)
  --no-venv          Use the system Python instead of a virtual environment
  --skip-packages    Do not install distro packages (offline / already installed)
  --skip-udev        Do not install the Apple USB udev rules
  --no-c-build       Do not compile the optional native C binary
  --uninstall        Remove the `mdmpatcher` command and udev rules
  --purge            With --uninstall: also delete the install directory
  -h, --help         Show this help text

ENVIRONMENT:
  MDM_INSTALL_DIR, MDM_BRANCH, MDM_REPO_URL, MDM_BIN_DIR, MDM_SKIP_PACKAGES

EXAMPLES:
  curl -fsSL .../main/scripts/install.sh | bash
  curl -fsSL .../main/scripts/install.sh | bash -s -- --dir ~/tools/mdmpatcher
  ./scripts/install.sh --branch main --venv
  ./scripts/install.sh --uninstall --purge
USAGE
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)           [[ $# -ge 2 ]] || { err "--dir requires a path"; exit 1; }; INSTALL_DIR="$2"; shift 2 ;;
        --dir=*)         INSTALL_DIR="${1#*=}"; shift ;;
        --branch)        [[ $# -ge 2 ]] || { err "--branch requires a name"; exit 1; }; BRANCH="$2"; shift 2 ;;
        --branch=*)      BRANCH="${1#*=}"; shift ;;
        --repo)          [[ $# -ge 2 ]] || { err "--repo requires a URL"; exit 1; }; REPO_URL="$2"; shift 2 ;;
        --repo=*)        REPO_URL="${1#*=}"; shift ;;
        --venv)          WITH_VENV=1; shift ;;
        --no-venv)       WITH_VENV=0; shift ;;
        --skip-packages) SKIP_PACKAGES=1; shift ;;
        --skip-udev)     SKIP_UDEV=1; shift ;;
        --no-c-build)    WITH_C_BUILD=0; shift ;;
        --uninstall)     DO_UNINSTALL=1; shift ;;
        --purge)         PURGE=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               err "Unknown option: $1"; echo; usage; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Where are we? (a real checkout next to this script => in-place mode)
# ---------------------------------------------------------------------------
SCRIPT_PATH=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi

LOCAL_REPO=""
if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH/../mdmpatcher/cli.py" && -f "$SCRIPT_PATH/../mdmpatcher.py" ]]; then
    LOCAL_REPO="$(cd -- "$SCRIPT_PATH/.." && pwd)"
fi

if [[ -z "$INSTALL_DIR" ]]; then
    if [[ -n "$LOCAL_REPO" ]]; then
        INSTALL_DIR="$LOCAL_REPO"
    elif [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        # Running as root: install system-wide so every user gets the command.
        INSTALL_DIR="/opt/mdmpatcher-linux"
    else
        INSTALL_DIR="${HOME:-/root}/mdmpatcher-linux"
    fi
fi

# Always work with an absolute path (relative paths would break the launcher).
case "$INSTALL_DIR" in
    /*) ;;
    *) INSTALL_DIR="$PWD/$INSTALL_DIR" ;;
esac
INSTALL_DIR="${INSTALL_DIR%/}"

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        warn "Running as non-root and 'sudo' was not found; system steps may fail."
    fi
fi

# ===========================================================================
# Uninstall
# ===========================================================================
do_uninstall() {
    echo -e "${BOLD}=====================================================================${RESET}"
    echo -e "${BOLD}  MDMPatcher-Linux v${APP_VERSION} — Uninstaller${RESET}"
    echo -e "${BOLD}=====================================================================${RESET}"
    info "Install directory: ${BOLD}${INSTALL_DIR}${RESET}"

    local removed=0
    for cand in "${BIN_DIR:-}" "/usr/local/bin/${LAUNCHER_NAME}" "${HOME:-/root}/.local/bin/${LAUNCHER_NAME}"; do
        [[ -n "$cand" && -e "$cand" ]] || continue
        if grep -q "MDMPatcher-Linux launcher" "$cand" 2>/dev/null; then
            rm -f "$cand" "$cand.mdmpatcher-backup" && ok "Removed launcher: $cand" && removed=1
        fi
    done
    [[ "$removed" -eq 1 ]] || warn "No MDMPatcher-Linux launcher found on PATH."

    if [[ -f /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules ]]; then
        rm -f /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules 2>/dev/null \
            || $SUDO rm -f /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules
        ok "Removed udev rules: /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules"
        if command -v udevadm >/dev/null 2>&1; then
            $SUDO udevadm control --reload-rules 2>/dev/null || true
        fi
    fi

    if [[ "$PURGE" -eq 1 ]]; then
        if [[ -d "$INSTALL_DIR" ]]; then
            rm -rf "$INSTALL_DIR"
            ok "Deleted install directory: $INSTALL_DIR"
        fi
    else
        info "Kept ${INSTALL_DIR} (re-run with --purge to delete it, your venv included)."
    fi

    ok "Uninstall complete."
    exit 0
}

[[ "$DO_UNINSTALL" -eq 1 ]] && do_uninstall

# ===========================================================================
# Bootstrap: clone from `main` (only when we are NOT inside a checkout)
# ===========================================================================
github_slug() {
    # Derive "owner/repo" from the configured repo URL (empty for non-GitHub URLs).
    local slug=""
    case "$REPO_URL" in
        https://github.com/*)   slug="${REPO_URL#https://github.com/}" ;;
        http://github.com/*)    slug="${REPO_URL#http://github.com/}" ;;
        git@github.com:*)       slug="${REPO_URL#git@github.com:}" ;;
    esac
    slug="${slug%.git}"
    slug="${slug%/}"
    case "$slug" in
        */*) echo "$slug" ;;
        *)   echo "" ;;
    esac
}

download_tarball() {
    local dest="$1" slug url
    slug="$(github_slug)"
    if [[ -z "$slug" ]]; then
        warn "Tarball fallback is only available for GitHub repositories."
        return 1
    fi
    url="https://codeload.github.com/${slug}/tar.gz/refs/heads/${BRANCH}"
    mkdir -p "$dest"
    info "Downloading the ${BRANCH} tarball from GitHub..."
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" | tar -xz --strip-components=1 -C "$dest"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$url" | tar -xz --strip-components=1 -C "$dest"
    else
        return 1
    fi
    [[ -f "$dest/mdmpatcher.py" && -f "$dest/scripts/install.sh" ]]
}

bootstrap_repo() {
    local parent
    parent="$(dirname -- "$INSTALL_DIR")"
    mkdir -p "$parent"

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        info "Existing installation found at ${BOLD}${INSTALL_DIR}${RESET} — updating from ${BRANCH}..."
        if ! git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH"; then
            warn "Could not reach origin/${BRANCH}; continuing with the local copy."
            return 0
        fi
        if git -C "$INSTALL_DIR" checkout --quiet "$BRANCH" 2>/dev/null \
            && git -C "$INSTALL_DIR" pull --ff-only --quiet origin "$BRANCH"; then
            ok "Updated to the latest ${BRANCH}."
        else
            warn "Local copy has changes that prevent a fast-forward; using it as-is."
        fi
        return 0
    fi

    if [[ -d "$INSTALL_DIR" ]] && [[ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
        err "Directory '${INSTALL_DIR}' already exists and is not an MDMPatcher-Linux checkout."
        err "Re-run with '--dir <another/path>' or move that directory first."
        exit 1
    fi

    if command -v git >/dev/null 2>&1; then
        info "Cloning ${BOLD}${REPO_URL}${RESET} (branch: ${BRANCH}) -> ${INSTALL_DIR}"
        if git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"; then
            ok "Repository cloned."
            return 0
        fi
        warn "git clone failed; falling back to a tarball download..."
        rm -rf "$INSTALL_DIR"
    else
        warn "git is not installed; falling back to a tarball download..."
    fi

    if download_tarball "$INSTALL_DIR"; then
        ok "Source downloaded from ${BRANCH} (git not used; 'mdmpatcher update' will need git)."
        return 0
    fi

    err "Could not clone or download MDMPatcher-Linux."
    err "Install git (or curl/wget) and re-run, or clone manually:"
    err "  git clone ${REPO_URL} && cd mdmpatcher-linux && ./scripts/install.sh"
    exit 1
}

if [[ -z "$LOCAL_REPO" && "${MDM_BOOTSTRAPPED:-0}" != "1" ]]; then
    echo -e "${BOLD}=====================================================================${RESET}"
    echo -e "${BOLD}  MDMPatcher-Linux v${APP_VERSION} — One-Command Installer (iOS 9 – 27+)${RESET}"
    echo -e "${BOLD}=====================================================================${RESET}"
    bootstrap_repo
    if [[ ! -f "$INSTALL_DIR/scripts/install.sh" ]]; then
        err "Bootstrap failed: ${INSTALL_DIR}/scripts/install.sh is missing."
        exit 1
    fi
    info "Handing over to the installer inside ${INSTALL_DIR}..."
    exec env MDM_BOOTSTRAPPED=1 \
        MDM_INSTALL_DIR="$INSTALL_DIR" \
        MDM_BRANCH="$BRANCH" \
        MDM_REPO_URL="$REPO_URL" \
        MDM_SKIP_PACKAGES="$SKIP_PACKAGES" \
        bash "$INSTALL_DIR/scripts/install.sh" \
        --dir "$INSTALL_DIR" --branch "$BRANCH" --repo "$REPO_URL" \
        $( [[ "$WITH_VENV" -eq 1 ]] && echo --venv || echo --no-venv ) \
        $( [[ "$WITH_C_BUILD" -eq 0 ]] && echo --no-c-build ) \
        $( [[ "$SKIP_UDEV" -eq 1 ]] && echo --skip-udev ) \
        $( [[ "$SKIP_PACKAGES" -eq 1 ]] && echo --skip-packages )
fi

# ===========================================================================
# From here on we are definitely running inside a checkout at $INSTALL_DIR
# ===========================================================================
REPO_ROOT="$INSTALL_DIR"
if [[ -f "$REPO_ROOT/scripts/39-usbmuxd-mdmpatcher.rules" ]]; then
    SCRIPT_DIR="$REPO_ROOT/scripts"
else
    SCRIPT_DIR="$SCRIPT_PATH"
fi

echo ""
echo -e "${BOLD}=====================================================================${RESET}"
echo -e "${BOLD}  MDMPatcher-Linux v${APP_VERSION} — Official Linux Installer (iOS 9 – 27+)${RESET}"
echo -e "${BOLD}=====================================================================${RESET}"
info "Install directory: ${BOLD}${REPO_ROOT}${RESET}"
if [[ -d "$REPO_ROOT/.git" ]]; then
    info "Git branch       : ${BOLD}$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')${RESET}"
fi

# ---------------------------------------------------------------------------
# 1) Distro packages
# ---------------------------------------------------------------------------
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

if [[ "$SKIP_PACKAGES" -eq 1 ]]; then
    warn "Skipping distro package installation (--skip-packages)."
else
    case "$DISTRO_ID" in
        fedora|rhel|centos|almalinux|rocky|nobara|ultramarine|bazzite)
            install_fedora ;;
        ubuntu|debian|linuxmint|pop|kali|elementary|zorin|raspbian)
            install_debian_ubuntu ;;
        arch|manjaro|endeavouros|cachyos|garuda|artix)
            install_arch ;;
        opensuse*|sles)
            install_opensuse ;;
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
fi

# ---------------------------------------------------------------------------
# 2) udev rules + usbmuxd
# ---------------------------------------------------------------------------
if [[ "$SKIP_UDEV" -eq 0 ]]; then
    if [[ -d /etc/udev/rules.d && -f "$SCRIPT_DIR/39-usbmuxd-mdmpatcher.rules" ]]; then
        info "Installing Apple USB udev rules to /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules..."
        if cp "$SCRIPT_DIR/39-usbmuxd-mdmpatcher.rules" /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules 2>/dev/null; then
            chmod 644 /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules 2>/dev/null || true
        else
            $SUDO cp "$SCRIPT_DIR/39-usbmuxd-mdmpatcher.rules" /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules
            $SUDO chmod 644 /etc/udev/rules.d/39-usbmuxd-mdmpatcher.rules
        fi
        if command -v udevadm >/dev/null 2>&1; then
            $SUDO udevadm control --reload-rules || true
            $SUDO udevadm trigger --subsystem-match=usb --attr-match=idVendor=05ac || true
        fi
        ok "Apple USB udev rules installed and reloaded."
    else
        warn "Skipping udev rules (/etc/udev/rules.d not present on this system)."
    fi
fi

if command -v systemctl >/dev/null 2>&1; then
    info "Ensuring usbmuxd systemd service is active..."
    $SUDO systemctl start usbmuxd.service 2>/dev/null || true
elif command -v usbmuxd >/dev/null 2>&1; then
    info "Starting usbmuxd in the background..."
    $SUDO usbmuxd --daemon 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 3) Python environment (virtualenv by default — handled by the launcher)
# ---------------------------------------------------------------------------
python_install_deps() {
    local py="$1" target="${REPO_ROOT}"
    # Only request the `usb` extra when this checkout actually defines it.
    if grep -Eq '^usb[[:space:]]*=' "${REPO_ROOT}/pyproject.toml" 2>/dev/null; then
        target="${REPO_ROOT}[usb]"
    fi
    "$py" -m pip install --upgrade pip >/dev/null 2>&1 || true
    if "$py" -m pip install -e "${target}"; then
        return 0
    fi
    warn "Standard pip install failed; retrying with --break-system-packages..."
    if "$py" -m pip install --break-system-packages -e "${target}"; then
        return 0
    fi
    warn "Retrying with --user (pymobiledevice3 for iOS 27 may be unavailable)..."
    "$py" -m pip install --user --break-system-packages -e "${REPO_ROOT}" || {
        warn "Could not install Python dependencies (offline?). The core CLI still works."
        return 1
    }
}

if [[ "$WITH_VENV" -eq 1 ]]; then
    info "Creating Python virtual environment at ${REPO_ROOT}/.venv ..."
    if python3 -m venv "${REPO_ROOT}/.venv" 2>/dev/null; then
        ok "Virtual environment created."
        info "Installing Python dependencies (pymobiledevice3) into .venv ..."
        python_install_deps "${REPO_ROOT}/.venv/bin/python3" || true
    else
        warn "Could not create a virtual environment (is python3-venv / python3-pip installed?)."
        warn "Falling back to the system Python."
        WITH_VENV=0
        python_install_deps python3 || true
    fi
else
    info "Using the system Python (--no-venv)."
    python_install_deps python3 || true
fi

# ---------------------------------------------------------------------------
# 4) Optional native C binary
# ---------------------------------------------------------------------------
if [[ "$WITH_C_BUILD" -eq 1 ]]; then
    info "Attempting to build native C binary (src/c/mdm_patch)..."
    if make -C "${REPO_ROOT}/src/c" >/dev/null 2>&1; then
        ok "Built native C binary: ${REPO_ROOT}/src/c/mdm_patch"
    else
        warn "Native C binary build skipped (optional; the Python CLI works without it)."
    fi
fi

# ---------------------------------------------------------------------------
# 5) `mdmpatcher` launcher on PATH
# ---------------------------------------------------------------------------
pick_bin_dir() {
    if [[ -n "$BIN_DIR" ]]; then
        echo "$BIN_DIR"; return
    fi
    if [[ -d /usr/local/bin && -w /usr/local/bin ]]; then
        echo "/usr/local/bin"; return
    fi
    if [[ "${EUID:-$(id -u)}" -eq 0 && -d /usr/local/bin ]]; then
        echo "/usr/local/bin"; return
    fi
    echo "${HOME:-/root}/.local/bin"
}

install_launcher() {
    local target_dir="$1" target tmplaunch
    mkdir -p "$target_dir" 2>/dev/null || $SUDO mkdir -p "$target_dir"
    target="${target_dir}/${LAUNCHER_NAME}"
    tmplaunch="$(mktemp "${TMPDIR:-/tmp}/mdmpatcher-launcher.XXXXXX")"

    # Runtime values ($PY fallback, "$@") are escaped so they are evaluated
    # when the launcher runs, not now.
    cat > "$tmplaunch" <<EOF
#!/usr/bin/env bash
# MDMPatcher-Linux launcher — generated by scripts/install.sh
# Installed from: ${REPO_ROOT}
set -euo pipefail
INSTALL_DIR="${REPO_ROOT}"
if [[ -x "\${INSTALL_DIR}/.venv/bin/python3" ]]; then
    PY="\${INSTALL_DIR}/.venv/bin/python3"
else
    PY="\${MDM_PYTHON:-\$(command -v python3 || command -v python)}"
fi
export MDM_INSTALL_DIR="\${INSTALL_DIR}"
exec "\${PY}" "\${INSTALL_DIR}/mdmpatcher.py" "\$@"
EOF
    chmod 755 "$tmplaunch"

    if [[ -e "$target" ]] && ! grep -q "MDMPatcher-Linux launcher" "$target" 2>/dev/null; then
        warn "An unrelated '${target}' already exists; backing it up to ${target}.mdmpatcher-backup"
        if ! cp "$target" "${target}.mdmpatcher-backup" 2>/dev/null; then
            $SUDO cp "$target" "${target}.mdmpatcher-backup"
        fi
    fi

    if cp "$tmplaunch" "$target" 2>/dev/null; then
        chmod 755 "$target"
    else
        $SUDO cp "$tmplaunch" "$target"
        $SUDO chmod 755 "$target"
    fi
    rm -f "$tmplaunch"
    echo "$target"
}

BIN_TARGET_DIR="$(pick_bin_dir)"
info "Installing the '${LAUNCHER_NAME}' command into ${BOLD}${BIN_TARGET_DIR}${RESET} ..."
LAUNCHER_PATH="$(install_launcher "$BIN_TARGET_DIR")"
ok "Installed launcher: ${LAUNCHER_PATH}"

case ":${PATH}:" in
    *":${BIN_TARGET_DIR}:"*) ;;
    *)
        warn "${BIN_TARGET_DIR} is not in your PATH."
        for rc in "${HOME:-/root}/.bashrc" "${HOME:-/root}/.zshrc" "${HOME:-/root}/.profile"; do
            [[ -f "$rc" ]] || continue
            grep -q "mdmpatcher" "$rc" 2>/dev/null && continue
            printf '\n# Added by the MDMPatcher-Linux installer\nexport PATH="%s:$PATH"\n' "$BIN_TARGET_DIR" >> "$rc"
            ok "Added ${BIN_TARGET_DIR} to PATH in ${rc}"
        done
        info "Open a new shell, or run: export PATH=\"${BIN_TARGET_DIR}:\$PATH\""
        ;;
esac

# ---------------------------------------------------------------------------
# 6) Verify + finish
# ---------------------------------------------------------------------------
chmod +x "${REPO_ROOT}/mdmpatcher.py"

echo ""
if "$LAUNCHER_PATH" --version >/dev/null 2>&1; then
    ok "${BOLD}Verified:${RESET} $($LAUNCHER_PATH --version)"
else
    warn "The launcher installed but '--version' failed; run it directly to see the error:"
    warn "  ${LAUNCHER_PATH} --version"
fi

echo ""
ok "${BOLD}Installation complete!${RESET}"
cat <<EOF

  Plug in your iPhone/iPad (tap Trust), then run from ANY directory:

      ${LAUNCHER_NAME} info          # verify the device is detected
      ${LAUNCHER_NAME} patch         # auto-patches iOS 9 – iOS 27+
      ${LAUNCHER_NAME} skip-setup    # iOS 27 direct bypass (no restore)
      ${LAUNCHER_NAME} gui           # desktop GUI / interactive menu

  Keep it up to date with the latest '${BRANCH}':

      ${LAUNCHER_NAME} update

  Source installed at: ${REPO_ROOT}
  Uninstall with:      ${REPO_ROOT}/scripts/install.sh --uninstall [--purge]
EOF
