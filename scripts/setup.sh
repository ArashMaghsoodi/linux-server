#!/usr/bin/env bash

set -euo pipefail

BASE_DIR="$HOME/bot-server"
BOT_DIR="$BASE_DIR/bots"
PANEL_DIR="$BASE_DIR/panel"
HERMES_HOME_DIR="$HOME/.hermes"
NINEROUTER_HOME_DIR="$HOME/.9router"
BOT_REGISTRY_PATH="${BOT_REGISTRY_PATH:-$BASE_DIR/bot-registry.json}"
BOT_CREDENTIALS_KEY_PATH="${BOT_CREDENTIALS_KEY_PATH:-$BASE_DIR/.bot_credentials_key}"

echo "======================================"
echo "Server setup"
echo "======================================"

mkdir -p "$BASE_DIR" "$BOT_DIR" "$PANEL_DIR" "$NINEROUTER_HOME_DIR"
chmod 700 "$NINEROUTER_HOME_DIR"

# Persist the dynamic bot fleet across a fresh runner instance.
if [ ! -f "$BOT_REGISTRY_PATH" ]; then
    python3.12 - "$BOT_REGISTRY_PATH" <<'PY'
import json, os, sys
path = sys.argv[1]
registry = {"version": 1, "bots": [], "encrypted_credentials": {}}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(registry, handle, indent=2)
    handle.write("\n")
PY
fi

if [ ! -f "$BOT_CREDENTIALS_KEY_PATH" ]; then
    python3.12 - "$BOT_CREDENTIALS_KEY_PATH" <<'PY'
import os, sys
from cryptography.fernet import Fernet
path = sys.argv[1]
key = Fernet.generate_key().decode("ascii")
with open(path, "w", encoding="utf-8") as handle:
    handle.write(f"{key}\n")
os.chmod(path, 0o600)
PY
fi

# The hosted runner may include Google's Chrome source, which can briefly serve
# Packages metadata that does not match its Release file. Chrome is not a
# dependency of this server, so disable every APT definition that references it.
while IFS= read -r -d '' apt_source; do
    if grep -Eqi 'dl\.google\.com/linux/chrome|google-chrome' "$apt_source"; then
        echo "==> Disabling unused Chrome APT source: $apt_source"
        sudo mv "$apt_source" "${apt_source}.disabled"
    fi
done < <(find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -print0 2>/dev/null)

echo
echo "==> Updating system"
sudo apt-get update
sudo apt-get install -y \
    software-properties-common \
    build-essential \
    curl \
    openssl \
    git \
    wget \
    jq \
    htop \
    neofetch \
    tmux \
    nano \
    vim \
    tree \
    ncdu \
    net-tools \
    iputils-ping \
    dnsutils \
    unzip \
    openssh-server

echo
echo "==> Installing cloudflared"
curl -fsSL --output /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb || sudo apt-get install -f -y
rm -f /tmp/cloudflared.deb

echo
echo "==> Installing Tailscale"
curl -fsSL https://tailscale.com/install.sh | sh
tailscale version || true

echo
echo "==> Installing Hermes Agent"
export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"

HERMES_REPO_DIR="$HERMES_HOME_DIR/hermes-agent"
HERMES_COMMIT="ea81748579ee1732d214ccb75f91d22208ed623d"
HERMES_CACHED=false

if command -v hermes >/dev/null 2>&1 && [ -d "$HERMES_REPO_DIR" ]; then
    if git -C "$HERMES_REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 && hermes --version >/dev/null 2>&1; then
        CACHED_HERMES_COMMIT="$(git -C "$HERMES_REPO_DIR" rev-parse HEAD)"
        if [ "$CACHED_HERMES_COMMIT" = "$HERMES_COMMIT" ]; then
            HERMES_CACHED=true
            echo "Pinned Hermes installation found in the restored runner cache; skipping update."
        else
            echo "Hermes cache is at $CACHED_HERMES_COMMIT, not pinned commit $HERMES_COMMIT; reinstalling."
        fi
    else
        echo "Hermes cache is present but incomplete or corrupted; removing the stale checkout and reinstalling."
    fi
    if [ "$HERMES_CACHED" = "false" ]; then
        rm -rf "$HERMES_REPO_DIR"
        rm -rf "$HERMES_HOME_DIR/bin"
        rm -f "$HOME/.local/bin/hermes" 2>/dev/null || true
    fi
fi

if [ "$HERMES_CACHED" = "false" ]; then
    if [ -d "$HERMES_REPO_DIR" ]; then
        echo "Removing stale Hermes checkout before reinstalling."
        rm -rf "$HERMES_REPO_DIR"
        rm -rf "$HERMES_HOME_DIR/bin"
        rm -f "$HOME/.local/bin/hermes" 2>/dev/null || true
    fi
    echo "No matching cached Hermes installation found; installing pinned commit $HERMES_COMMIT."
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --commit "$HERMES_COMMIT"
    export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"
fi

if ! command -v hermes >/dev/null 2>&1; then
    echo "ERROR: Hermes Agent installation did not provide the hermes command."
    exit 1
fi
hermes --version
mkdir -p "$HERMES_HOME_DIR"
chmod 700 "$HERMES_HOME_DIR"

echo "==> Installing Hermes operator context (SOUL.md)"
if [ -f ./hermes/SOUL.md ]; then
    install -m 600 ./hermes/SOUL.md "$HERMES_HOME_DIR/SOUL.md"
else
    echo "WARNING: hermes/SOUL.md not found; Hermes keeps its default identity."
fi

echo
echo "==> Preparing 9Router data directory"
if [ ! -f "$NINEROUTER_HOME_DIR/.env" ]; then
    cat > "$NINEROUTER_HOME_DIR/.env" <<EOF
JWT_SECRET=$(openssl rand -hex 32)
INITIAL_PASSWORD=${SERVER_PASSWORD:-admin}
API_KEY_SECRET=$(openssl rand -hex 32)
MACHINE_ID_SALT=$(openssl rand -hex 32)
DATA_DIR=/app/data
PORT=20128
NODE_ENV=production
BASE_URL=http://127.0.0.1:20128
NEXT_PUBLIC_BASE_URL=http://127.0.0.1:20128
REQUIRE_API_KEY=false
ENABLE_REQUEST_LOGS=false
EOF
    chmod 600 "$NINEROUTER_HOME_DIR/.env"
fi
NINEROUTER_ENV_TMP="$NINEROUTER_HOME_DIR/.env.tmp"
grep -v '^INITIAL_PASSWORD=' "$NINEROUTER_HOME_DIR/.env" > "$NINEROUTER_ENV_TMP" || true
printf 'INITIAL_PASSWORD=%s\n' "${SERVER_PASSWORD:-admin}" >> "$NINEROUTER_ENV_TMP"
chmod 600 "$NINEROUTER_ENV_TMP"
mv "$NINEROUTER_ENV_TMP" "$NINEROUTER_HOME_DIR/.env"
if grep -q '^REQUIRE_API_KEY=true$' "$NINEROUTER_HOME_DIR/.env"; then
    sed -i 's/^REQUIRE_API_KEY=true$/REQUIRE_API_KEY=false/' "$NINEROUTER_HOME_DIR/.env"
fi

echo
echo "==> Installing Python 3.12"

sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update

sudo apt-get install -y \
    python3.12 \
    python3.12-venv \
    python3.12-dev

python3.12 --version

echo
echo "==> Setting up Panel environment"
cp -r ./panel/* "$PANEL_DIR/" 2>/dev/null || true
cd "$PANEL_DIR"

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install flask psutil requests pyyaml cryptography flask-sock==0.7.0 simple-websocket==1.1.0
deactivate

echo
echo "======================================"
echo "Setup complete"
echo "======================================"

echo
echo "Bot registry:  $BOT_REGISTRY_PATH"
echo "Bot creds key: $BOT_CREDENTIALS_KEY_PATH"
echo "Panel:         $PANEL_DIR"
echo "Hermes home:   $HERMES_HOME_DIR"
