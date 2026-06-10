#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  Apple Mail Triage Agent Install Script
#  Installs the launchd job that runs the status-aware agent
#  wrapper every 5 minutes.
# ============================================================

LABEL="com.user.apple-mail-triage"
PROJECT_DIR="/Users/martin/projects/apple-mail-triage"
PLIST_SRC="${PROJECT_DIR}/com.user.apple-mail-triage.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.user.apple-mail-triage.plist"
CONFIG_DIR="${HOME}/.apple-mail-triage"
LOG_DIR="${CONFIG_DIR}/logs"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
CONFIG_EXAMPLE="${PROJECT_DIR}/config.toml.example"

echo ""
echo "========================================"
echo "  Apple Mail Triage Agent Installer"
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

echo "[2/8] Dependencies: uv found"

# ----------------------------------------------------------
# 3. Check the on-device Apple foundation model is available
# ----------------------------------------------------------
echo "[3/8] Checking Apple Intelligence foundation model availability ..."
if ! uv run --project "${PROJECT_DIR}" python - <<'PY'
import sys
import apple_fm_sdk as fm
ok, reason = fm.SystemLanguageModel().is_available()
if not ok:
    print(f"      Foundation model unavailable: {reason}", file=sys.stderr)
    sys.exit(1)
PY
then
    echo ""
    echo "ERROR: the on-device Apple foundation model is not available." >&2
    echo "       Enable Apple Intelligence in System Settings > Apple Intelligence & Siri," >&2
    echo "       wait for the model download to finish, then re-run this script." >&2
    echo "       (Building apple-fm-sdk also requires full Xcode with an accepted license.)" >&2
    exit 1
fi
echo "      Foundation model available."

# ----------------------------------------------------------
# 4. Create ~/.apple-mail-triage/logs/ if missing
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
echo "      Wrapper script: agent_with_mail_app_status_check.py"
echo "      Mail app status checks will delay runs when Mail is active/focused"

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
echo "        \"apple-mail-triage wants access to control Mail.\""
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
echo "    tail -f ~/.apple-mail-triage/logs/agent.log"
echo ""
echo "  Review structured run stats:"
echo "    cat ~/.apple-mail-triage/logs/runs.ndjson"
echo ""
echo "  Check mail app status delays in runs:"
echo "    jq -r '.mail_app_status' ~/.apple-mail-triage/logs/runs.ndjson | head -20"
echo ""
echo "  Check launchd status:"
echo "    launchctl print gui/${UID}/${LABEL}"
echo ""
