#!/usr/bin/env osascript -l JavaScript

// list_accounts.js - emit a JSON array of Apple Mail accounts as
// {id, name}, one entry per account. Used by fetcher.py to map the
// UUIDs that appear under ~/Library/Mail/V<N>/ to friendly names so the
// flag-setter (which expects mail.accounts.byName(...)) keeps working.
//
// Returns an empty array on any failure.

function run(argv) {
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
    // Mail not running or no permission - emit empty array.
  }
  return JSON.stringify(out);
}
