#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  Apple Mail Triage Agent Uninstall Script
#  Removes the launchd job. Does NOT delete user data.
# ============================================================

LABEL="com.user.apple-mail-triage"
PLIST_DST="${HOME}/Library/LaunchAgents/com.user.apple-mail-triage.plist"

echo ""
echo "========================================"
echo "  Apple Mail Triage Agent Uninstaller"
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
echo "  User data preserved at ~/.apple-mail-triage/"
echo "  To wipe it:  rm -rf ~/.apple-mail-triage/"
echo ""
echo "  Queue file preserved at:"
echo "    /Users/martin/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian/Inbox/Mail Triage.md"
echo "  Delete manually if desired."
echo ""
echo "  The project source at ~/projects/apple-mail-triage/ is untouched."
echo ""
