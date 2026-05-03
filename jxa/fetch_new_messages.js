#!/usr/bin/env osascript -l JavaScript
'use strict';

/**
 * fetch_new_messages.js — JXA script for the mail-to-todo agent.
 *
 * Queries Apple Mail for messages across ALL accounts' inboxes received at or
 * after a given timestamp, and emits one NDJSON object per message to stdout.
 * Designed to be called from agent.py via:
 *   osascript -l JavaScript fetch_new_messages.js --since <ISO8601> [--max N] [--truncate-bytes N]
 *
 * CLI:
 *   --since          REQUIRED. ISO8601 timestamp. Only messages with
 *                    dateReceived >= since are returned.
 *   --max            Optional. Default 200. Maximum number of messages to emit
 *                    (oldest-first within the window).
 *   --truncate-bytes Optional. Default 4096. Truncate message body to this many
 *                    UTF-8 bytes (conservative: truncates by character, treating
 *                    each char as up to 4 bytes to stay safely under the limit).
 *
 * Output: NDJSON (one JSON object per line) with fields:
 *   id, account, mailbox, subject, sender, replyTo, messageId, dateReceived,
 *   junk, read, headers, content
 *
 * Exit 0 on success (including zero messages found).
 * Exit 1 on errors (Mail not running, permission denied, bad arguments).
 * Errors are written to stderr.
 */

function run(argv) {
  // ── Argument parsing ──────────────────────────────────────────────────────
  var since = null;
  var maxMessages = 200;
  var truncateBytes = 4096;

  for (var i = 0; i < argv.length; i++) {
    if (argv[i] === '--since' && i + 1 < argv.length) {
      since = argv[++i];
    } else if (argv[i] === '--max' && i + 1 < argv.length) {
      var parsed = parseInt(argv[++i], 10);
      if (isNaN(parsed) || parsed < 1) {
        writeStderr('--max must be a positive integer');
        $.exit(1);
      }
      maxMessages = parsed;
    } else if (argv[i] === '--truncate-bytes' && i + 1 < argv.length) {
      var parsedTrunc = parseInt(argv[++i], 10);
      if (isNaN(parsedTrunc) || parsedTrunc < 1) {
        writeStderr('--truncate-bytes must be a positive integer');
        $.exit(1);
      }
      truncateBytes = parsedTrunc;
    }
  }

  if (!since) {
    writeStderr('--since is required. Usage: fetch_new_messages.js --since <ISO8601> [--max N] [--truncate-bytes N]');
    $.exit(1);
  }

  var sinceDate = new Date(since);
  if (isNaN(sinceDate.getTime())) {
    writeStderr('--since value is not a valid ISO8601 timestamp: ' + since);
    $.exit(1);
  }

  // ── Verify Mail is running ────────────────────────────────────────────────
  var mail;
  try {
    mail = Application('Mail');
    mail.includeStandardAdditions = true;
    // Accessing accounts forces a TCC check; if denied it throws here.
    var _ = mail.accounts.length;
  } catch (e) {
    writeStderr('Cannot connect to Mail: ' + e.message);
    $.exit(1);
  }

  // ── Collect messages across all accounts ─────────────────────────────────
  // Note: $.exit() is used throughout for error exits so that osascript does
  // not print the run() return value to stdout (any non-undefined return value
  // from run() is serialised to stdout by osascript, polluting the NDJSON stream).
  var results = [];
  var accounts;
  try {
    accounts = mail.accounts();
  } catch (e) {
    writeStderr('Failed to list Mail accounts: ' + e.message);
    $.exit(1);
  }

  for (var a = 0; a < accounts.length; a++) {
    var account = accounts[a];
    var accountName;
    try {
      accountName = account.name();
    } catch (e) {
      writeStderr('Skipping account (cannot read name): ' + e.message);
      continue;
    }

    // Find the inbox mailbox (IMAP uses "INBOX", local Mail uses "Inbox")
    var inbox = null;
    try {
      var mailboxes = account.mailboxes();
      for (var m = 0; m < mailboxes.length; m++) {
        var mb = mailboxes[m];
        var mbName;
        try {
          mbName = mb.name();
        } catch (e) {
          continue;
        }
        if (mbName.toLowerCase() === 'inbox') {
          inbox = mb;
          break;
        }
      }
    } catch (e) {
      writeStderr('Warning: could not list mailboxes for account "' + accountName + '": ' + e.message);
      continue;
    }

    if (!inbox) {
      writeStderr('Warning: no inbox found for account "' + accountName + '" — skipping');
      continue;
    }

    var inboxName;
    try {
      inboxName = inbox.name();
    } catch (e) {
      inboxName = 'INBOX';
    }

    // Strategy: bulk-fetch all dates in one round trip per account, then do
    // index math in JS. Calling `.at(i).dateReceived()` round-trips per i,
    // which is minutes on Gmail's 130k+ inbox; bulk-fetch finishes in seconds.
    //
    //   1. inbox.messages.dateReceived() -> array of Dates (newest-first).
    //   2. In JS: find boundary = first index where date < sinceDate.
    //   3. Indices [boundary - perAccountCap, boundary) are this account's
    //      oldest-since-since candidates. Capture (account, idx, date) so
    //      the global sort can pick the truly-oldest M across all accounts
    //      without per-message round trips for messages we'll discard.
    var totalMessages;
    try {
      totalMessages = inbox.messages.length;
    } catch (e) {
      writeStderr('Warning: failed to read message count for "' + accountName + '/' + inboxName + '": ' + e.message);
      continue;
    }
    if (totalMessages === 0) continue;

    var allDates;
    try {
      allDates = inbox.messages.dateReceived();
    } catch (e) {
      writeStderr('Warning: bulk dateReceived() failed for "' + accountName + '/' + inboxName + '": ' + e.message);
      continue;
    }

    // Find boundary in JS — linear from index 0 since the assumption
    // (newest-first) is what we're verifying anyway. This is O(boundary),
    // fast as long as the user's mailbox isn't *entirely* within the window.
    var boundary = 0;
    while (boundary < allDates.length && allDates[boundary] >= sinceDate) {
      boundary++;
    }

    // Per-account cap: take the OLDEST perAccountCap candidates within the
    // window, so a slow account can't starve fast ones in the global cap.
    var perAccountCap = maxMessages * 4;
    var startIdx = Math.max(0, boundary - perAccountCap);
    for (var idx = boundary - 1; idx >= startIdx; idx--) {
      results.push({
        idx: idx,
        inbox: inbox,
        account: accountName,
        mailbox: inboxName,
        dateReceived: allDates[idx],
      });
    }
  }

  // ── Apply global oldest-first sort and --max cap ──────────────────────────
  // We sort on the cached date (no Mail round trip) before fetching full
  // properties — so we only pay the per-message property cost for the M
  // messages we'll actually emit.
  results.sort(function(x, y) { return x.dateReceived - y.dateReceived; });

  if (results.length > maxMessages) {
    results = results.slice(0, maxMessages);
  }

  // ── Emit NDJSON ───────────────────────────────────────────────────────────
  for (var r = 0; r < results.length; r++) {
    var entry = results[r];
    var msg;
    try {
      msg = entry.inbox.messages.at(entry.idx);
    } catch (e) {
      writeStderr('Warning: failed to access message ' + entry.account + '#' + entry.idx + ': ' + e.message);
      continue;
    }
    var obj = buildMessageObject(msg, entry.account, entry.mailbox, truncateBytes);
    if (obj !== null) {
      writeStdout(JSON.stringify(obj));
    }
  }

  // Do NOT return a value — any return value from run() is printed to stdout
  // by osascript, which would corrupt the NDJSON stream.
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Build a plain JS object from a Mail message reference.
 * Returns null if the message can't be read (e.g. deleted mid-run).
 */
function buildMessageObject(msg, accountName, mailboxName, truncateBytes) {
  // Fetch ALL properties in a single round trip via msg.properties().
  // Per-property fetching costs ~6s per call against Apple Mail; calling
  // properties() returns all 22 in ~7s — orders of magnitude faster than
  // hitting each property individually.
  var p;
  try {
    p = msg.properties();
  } catch (e) {
    writeStderr('Warning: properties() failed: ' + e.message);
    return null;
  }

  var dateReceived = null;
  if (p.dateReceived) {
    try { dateReceived = p.dateReceived.toISOString(); } catch(e) {}
  }

  var headers = (p.allHeaders !== undefined && p.allHeaders !== null)
    ? String(p.allHeaders) : null;

  var messageId = null;
  if (headers) {
    var mid = extractMessageId(headers);
    if (mid) messageId = mid;
  }

  var content = null;
  if (p.content !== undefined && p.content !== null) {
    try {
      content = truncateToBytes(String(p.content), truncateBytes);
    } catch(e) {}
  }

  return {
    id: (p.id !== undefined && p.id !== null) ? p.id : null,
    account: accountName,
    mailbox: mailboxName,
    subject: (p.subject !== undefined && p.subject !== null) ? String(p.subject) : null,
    sender: (p.sender !== undefined && p.sender !== null) ? String(p.sender) : null,
    replyTo: (p.replyTo !== undefined && p.replyTo !== null) ? String(p.replyTo) : null,
    messageId: messageId,
    dateReceived: dateReceived,
    junk: !!p.junkMailStatus,
    read: !!p.readStatus,
    headers: headers,
    content: content,
  };
}

/**
 * Extract the Message-Id value from raw header text.
 * Handles folded (multi-line) header values per RFC 2822.
 * Returns the trimmed angle-bracket value, or null if not found.
 */
function extractMessageId(headers) {
  // Unfold the header block: replace CRLF/LF followed by whitespace with a space
  var unfolded = headers.replace(/\r?\n[ \t]+/g, ' ');
  // Now search for Message-Id: (case-insensitive) on its own line
  var lines = unfolded.split(/\r?\n/);
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    if (/^message-id\s*:/i.test(line)) {
      // Extract the value after the colon
      var value = line.replace(/^message-id\s*:\s*/i, '').trim();
      if (value.length > 0) {
        return value;
      }
    }
  }
  return null;
}

/**
 * Truncate a string to at most `maxBytes` UTF-8 bytes.
 * Conservative: counts each character as potentially multi-byte.
 * Uses encodeURIComponent to compute byte length when needed.
 */
function truncateToBytes(str, maxBytes) {
  if (str === null || str === undefined) return null;
  // Fast path: ASCII strings — byte length equals char length
  if (str.length <= maxBytes) {
    // Check actual byte length only if string length is close to limit
    var byteLen = unescape(encodeURIComponent(str)).length;
    if (byteLen <= maxBytes) return str;
  }

  // Binary search for the right truncation point
  var lo = 0;
  var hi = Math.min(str.length, maxBytes);
  while (lo < hi) {
    var mid = Math.floor((lo + hi + 1) / 2);
    var encoded = unescape(encodeURIComponent(str.slice(0, mid)));
    if (encoded.length <= maxBytes) {
      lo = mid;
    } else {
      hi = mid - 1;
    }
  }
  var result = str.slice(0, lo);
  if (lo < str.length) {
    result += '…';  // single char ellipsis to signal truncation
  }
  return result;
}

/**
 * Write a line to stdout via the ObjC bridge.
 * JXA's $.NSFileHandle is the reliable way to write to stdout from osascript.
 */
function writeStdout(line) {
  var handle = $.NSFileHandle.fileHandleWithStandardOutput;
  var nsStr = $(line + '\n');
  var data = nsStr.dataUsingEncoding($.NSUTF8StringEncoding);
  handle.writeData(data);
}

/**
 * Write a line to stderr.
 */
function writeStderr(line) {
  var handle = $.NSFileHandle.fileHandleWithStandardError;
  var nsStr = $(line + '\n');
  var data = nsStr.dataUsingEncoding($.NSUTF8StringEncoding);
  handle.writeData(data);
}
