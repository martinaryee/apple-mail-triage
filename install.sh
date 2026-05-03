#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  Mail-Agent Install Script
#  Installs the launchd job that runs agent.py every 5 minutes.
# ============================================================

LABEL="com.user.mailagent"
PROJECT_DIR="/Users/martin/projects/mail-agent"
PLIST_SRC="${PROJECT_DIR}/com.user.mailagent.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.user.mailagent.plist"
CONFIG_DIR="${HOME}/.mail-agent"
LOG_DIR="${CONFIG_DIR}/logs"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
CONFIG_EXAMPLE="${PROJECT_DIR}/config.toml.example"

echo ""
echo "========================================"
echo "  Mail-Agent Installer"
echo "========================================"
echo ""

# ----------------------------------------------------------
# 1. Platform check
# ----------------------------------------------------------
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: This script requires macOS." >&2
    exit 1
fi

if ! command -v osascript &>/dev/null; then
    echo "ERROR: osascript not found. This script requires macOS." >&2
    exit 1
fi

echo "[1/8] Platform check: macOS OK"

# ----------------------------------------------------------
# 2. Dependency checks
# ----------------------------------------------------------
if ! command -v uv &>/dev/null; then
    echo ""
    echo "ERROR: 'uv' not found on PATH." >&2
    echo "       Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "       Then re-run this script." >&2
    exit 1
fi

if ! command -v ollama &>/dev/null; then
    echo ""
    echo "ERROR: 'ollama' not found on PATH." >&2
    echo "       Install it from https://ollama.com and start it, then re-run this script." >&2
    exit 1
fi

echo "[2/8] Dependencies: uv and ollama found"

# ----------------------------------------------------------
# 3. Ensure gemma4:e4b is pulled
# ----------------------------------------------------------
echo "[3/8] Checking Ollama model gemma4:e4b ..."
if ! ollama list 2>/dev/null | grep -q "gemma4:e4b"; then
    echo "      Model gemma4:e4b not found locally — pulling now (this may take a few minutes)..."
    ollama pull gemma4:e4b
    echo "      Pull complete."
else
    echo "      Model gemma4:e4b already present."
fi

# ----------------------------------------------------------
# 4. Create ~/.mail-agent/logs/ if missing
# ----------------------------------------------------------
if [[ ! -d "${LOG_DIR}" ]]; then
    mkdir -p "${LOG_DIR}"
    echo "[4/8] Created ${LOG_DIR}"
else
    echo "[4/8] Log directory already exists: ${LOG_DIR}"
fi

# ----------------------------------------------------------
# 5. Config check — stop here if config is fresh
# ----------------------------------------------------------
if [[ ! -f "${CONFIG_FILE}" ]]; then
    cp "${CONFIG_EXAMPLE}" "${CONFIG_FILE}"
    echo ""
    echo "[5/8] *** ACTION REQUIRED — Config file created ***"
    echo ""
    echo "      A starter config has been copied to:"
    echo "        ${CONFIG_FILE}"
    echo ""
    echo "      Please review and edit it before the agent runs:"
    echo "        - vault_path  : path to your Obsidian vault"
    echo "        - queue_file  : relative path within the vault"
    echo "        - start_date  : earliest date the agent should look back to"
    echo "        - ollama_model: model name (default: gemma4:e4b)"
    echo ""
    echo "      Once you are happy with the config, re-run this script:"
    echo "        bash ${PROJECT_DIR}/install.sh"
    echo ""
    exit 0
fi

echo "[5/8] Config file found: ${CONFIG_FILE}"

# ----------------------------------------------------------
# 6. Copy plist and validate
# ----------------------------------------------------------
mkdir -p "${HOME}/Library/LaunchAgents"
cp "${PLIST_SRC}" "${PLIST_DST}"
echo "[6/8] Plist installed to ${PLIST_DST}"

plutil -lint "${PLIST_DST}"
echo "      plutil -lint: OK"

# ----------------------------------------------------------
# 7. Load the launchd job
# ----------------------------------------------------------
# Bootout any existing instance first (ignore error if not loaded)
launchctl bootout "gui/${UID}/${LABEL}" 2>/dev/null || true

launchctl bootstrap "gui/${UID}" "${PLIST_DST}"
echo "[7/8] launchd job bootstrapped: ${LABEL}"

# ----------------------------------------------------------
# 8. TCC Automation permission notice
# ----------------------------------------------------------
echo ""
echo "[8/8] *** IMPORTANT — Automation Permission (TCC) ***"
echo ""
echo "      The very first time the agent calls Apple Mail via AppleScript,"
echo "      macOS will display a system dialog:"
echo ""
echo "        \"mail-agent wants access to control Mail.\""
echo ""
echo "      You MUST click Allow in that dialog for the agent to work."
echo ""
echo "      If you miss the dialog, re-grant access manually:"
echo "        System Settings → Privacy & Security → Automation"
echo "        → Find 'osascript' (or 'Python') → enable the Mail toggle."
echo ""
echo "      The prompt typically appears within the first 5-minute run cycle."
echo ""

# ----------------------------------------------------------
# Verification hints
# ----------------------------------------------------------
echo "========================================"
echo "  Installation complete"
echo "========================================"
echo ""
echo "  Next run: within 5 minutes (RunAtLoad triggered an immediate run)"
echo ""
echo "  Watch the live log:"
echo "    tail -f ~/.mail-agent/logs/agent.log"
echo ""
echo "  Review structured run stats:"
echo "    cat ~/.mail-agent/logs/runs.ndjson"
echo ""
echo "  Check launchd status:"
echo "    launchctl print gui/${UID}/${LABEL}"
echo ""
