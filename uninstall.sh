#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  Mail-Agent Uninstall Script
#  Removes the launchd job. Does NOT delete user data.
# ============================================================

LABEL="com.user.mailagent"
PLIST_DST="${HOME}/Library/LaunchAgents/com.user.mailagent.plist"

echo ""
echo "========================================"
echo "  Mail-Agent Uninstaller"
echo "========================================"
echo ""

# ----------------------------------------------------------
# 1. Unload the launchd job (ignore error if not loaded)
# ----------------------------------------------------------
echo "Stopping and removing launchd job: ${LABEL}"
launchctl bootout "gui/${UID}/${LABEL}" 2>/dev/null || true

# ----------------------------------------------------------
# 2. Remove the plist from LaunchAgents
# ----------------------------------------------------------
rm -f "${PLIST_DST}"
echo "Removed: ${PLIST_DST}"

# ----------------------------------------------------------
# 3. Preserve user data — print instructions
# ----------------------------------------------------------
echo ""
echo "========================================"
echo "  Uninstall complete"
echo "========================================"
echo ""
echo "  User data preserved at ~/.mail-agent/"
echo "  To wipe it:  rm -rf ~/.mail-agent/"
echo ""
echo "  Queue file preserved at:"
echo "    /Users/martin/Dropbox (Personal)/Obsidian - Personal/Inbox/Mail Triage.md"
echo "  Delete manually if desired."
echo ""
echo "  The project source at ~/projects/mail-agent/ is untouched."
echo ""
