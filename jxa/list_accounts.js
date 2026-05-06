#!/usr/bin/env osascript -l JavaScript
'use strict';

/**
 * list_accounts.js — emit a JSON array mapping each Apple Mail account's
 * UUID (as it appears under ~/Library/Mail/V*/) to its human-readable name.
 *
 * The on-disk index uses account UUIDs (folder names) but our flag-setting
 * code looks up `mail.accounts.byName(...)`, so the rest of the pipeline
 * needs the friendly name. JXA is the only way to read the user-facing
 * account name; this script returns in <1s and is the only JXA call we
 * make on the fetch side.
 *
 * Output: JSON array on stdout, e.g.
 *   [{"id": "ABC123-...", "name": "Gmail"}, ...]
 *
 * Exit 0 always; an empty array is printed if Mail is unreachable.
 */

function run() {
  var out = [];
  try {
    var mail = Application('Mail');
    var accts = mail.accounts();
    for (var i = 0; i < accts.length; i++) {
      try {
        out.push({ id: accts[i].id(), name: accts[i].name() });
      } catch (e) {
        // skip individual accounts that fail to read
      }
    }
  } catch (e) {
    // Mail not running or no permission — emit empty array, caller falls
    // back to UUIDs.
  }
  return JSON.stringify(out);
}
