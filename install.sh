#!/usr/bin/env bash
# keytap install script
# Installs Python dependencies, checks system tools, registers the
# `keytap` shell command in your ~/.zshrc or ~/.bash_profile, and installs the
# harden-recording Claude skill into ~/.claude/skills so it works from any folder.
#
# Usage:
#   bash install.sh               # full install
#   bash install.sh --skill-only  # only (re)install the Claude skill, e.g. after editing it
#
# After install, open a new terminal and run:
#   keytap

set -euo pipefail

KEYTAP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[keytap]${NC} $*"; }
warn()  { echo -e "${YELLOW}[keytap]${NC} $*"; }
error() { echo -e "${RED}[keytap]${NC} $*"; }

# ── Claude skill ──────────────────────────────────────────────────────────────

# Copies skills/harden-recording to ~/.claude/skills and writes this keytap folder's path into
# it, so Claude can find replay.py when it is not started inside the keytap folder.
install_skill() {
    local src="$KEYTAP_DIR/skills/harden-recording"
    local dst="$HOME/.claude/skills/harden-recording"
    if [[ ! -d "$src" ]]; then
        warn "skill not found at $src, skipping"
        return 0
    fi
    rm -rf "$dst"
    mkdir -p "$dst"
    cp -R "$src/." "$dst/"
    find "$dst" -name '*.md' -exec sed -i.bak "s|__KEYTAP_DIR__|$KEYTAP_DIR|g" {} +
    find "$dst" -name '*.bak' -delete
    info "Installed Claude skill harden-recording to $dst"
}

if [[ "${1:-}" == "--skill-only" ]]; then
    install_skill
    exit 0
fi

# ── Python ────────────────────────────────────────────────────────────────────

PYTHON=$(command -v python3 || true)
if [[ -z "$PYTHON" ]]; then
    error "python3 not found. Install it from https://python.org or via Homebrew: brew install python"
    exit 1
fi
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python $PY_VER found at $PYTHON"

# ── pip install ───────────────────────────────────────────────────────────────

info "Installing Python dependencies..."

PKGS=("av>=18.0" "numpy>=1.24" "Pillow>=9.0" "pygame>=2.0" "uiautomator2>=2.0" "mitmproxy>=10.0" "perfetto>=0.5")

# Try plain install first; if PEP 668 blocks it (Homebrew/system Python),
# retry with --user --break-system-packages which installs to ~/Library/Python.
if ! "$PYTHON" -m pip install --quiet "${PKGS[@]}" 2>/tmp/keytap_pip_err; then
    if grep -q "externally-managed" /tmp/keytap_pip_err; then
        info "Externally managed Python — installing to user site-packages"
        "$PYTHON" -m pip install --quiet --user --break-system-packages "${PKGS[@]}"
    else
        cat /tmp/keytap_pip_err
        exit 1
    fi
fi

info "Python dependencies installed."

# ── System tools ──────────────────────────────────────────────────────────────

check_tool() {
    local name="$1" hint="$2"
    if command -v "$name" &>/dev/null; then
        info "$name found at $(command -v "$name")"
    else
        warn "$name not found. $hint"
    fi
}

check_tool adb    "Install Android SDK platform-tools or run: brew install android-platform-tools"
check_tool ffmpeg "Install ffmpeg (optional, used by screenrecord fallback): brew install ffmpeg"

# ── mitmproxy CA cert (required for network capture) ──────────────────────────

MITM_CERT="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
if [[ ! -f "$MITM_CERT" ]]; then
    info "Generating mitmproxy CA certificate..."
    timeout 2 mitmdump --quiet 2>/dev/null || true
    if [[ -f "$MITM_CERT" ]]; then
        info "CA cert generated at $MITM_CERT"
    else
        warn "Could not generate mitmproxy cert automatically. Run 'mitmdump' once manually."
    fi
else
    info "mitmproxy CA cert already exists at $MITM_CERT"
fi

if [[ -f "$MITM_CERT" ]]; then
    echo ""
    warn "Network capture requires the mitmproxy CA cert installed on your Android device."
    echo "  One-time device setup (debug apps only):"
    echo ""
    echo "  1. Push cert to device:"
    echo "       adb push $MITM_CERT /sdcard/mitmproxy-ca.pem"
    echo "  2. On device: Settings > Security > Install certificate > CA certificate"
    echo "     Select mitmproxy-ca.pem from Internal Storage"
    echo "  3. Your app's network_security_config.xml must allow user CAs:"
    echo "       <debug-overrides>"
    echo "           <trust-anchors>"
    echo "               <certificates src=\"user\" />"
    echo "           </trust-anchors>"
    echo "       </debug-overrides>"
    echo ""
fi

# ── Shell function ────────────────────────────────────────────────────────────

KEYTAP_FUNC=$(cat <<EOF

# keytap — keyboard-driven Android mirror
keytap() {
    python3 "$KEYTAP_DIR/keytap.py" "\$@"
}
EOF
)

add_to_shell() {
    local rc_file="$1"
    if [[ -f "$rc_file" ]]; then
        if grep -q "keytap()" "$rc_file" 2>/dev/null; then
            # Update the path in case repo moved
            sed -i.bak "/python3.*keytap\.py/c\\    python3 \"$KEYTAP_DIR/keytap.py\" \"\\\$@\"" "$rc_file"
            info "Updated keytap path in $rc_file"
        else
            echo "$KEYTAP_FUNC" >> "$rc_file"
            info "Added keytap function to $rc_file"
        fi
        return 0
    fi
    return 1
}

ADDED=0
# Prefer zshrc on macOS, fall back to bash_profile, then bashrc
for RC in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
    if add_to_shell "$RC"; then
        ADDED=1
        break
    fi
done

if [[ $ADDED -eq 0 ]]; then
    # None exist yet — create .zshrc
    touch "$HOME/.zshrc"
    echo "$KEYTAP_FUNC" >> "$HOME/.zshrc"
    info "Created ~/.zshrc with keytap function"
fi

install_skill

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
info "Install complete."
echo ""
echo "  Open a new terminal and run:"
echo ""
echo "      keytap"
echo ""
echo "  Options:"
echo "      keytap --height 1000          # taller mirror window"
echo "      keytap --backend screenrecord # fallback capture"
echo "      keytap --serial emulator-5554 # target specific device"
echo ""
echo "  Claude skill (harden a flaky recording): in Claude Code, ask it to harden a keytap recording,"
echo "  or run: ./replay.py --help"
echo ""
