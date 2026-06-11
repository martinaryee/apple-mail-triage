#!/usr/bin/env osascript -l JavaScript

/**
 * set_flags.js — set Apple Mail flag colors on a list of messages.
 *
 * Called from agent.py after classification to annotate messages in-place.
 * Reads a JSON array of flag assignments from a file.
 *
 * CLI:
 *   --input <path>   REQUIRED. Path to a JSON file containing an array of:
 *                    { account, mailbox, id, flagIndex }
 *                    where flagIndex is 0-6 (or -1 to clear):
 *                    0=red  1=orange  2=yellow  3=green  4=blue  5=purple  6=grey
 *
 * Exit 0 on success (partial failures are logged to stderr but not fatal).
 * Exit 1 on argument or I/O errors.
 *
 * Performance: assignments are grouped by mailbox; each mailbox's message ids
 * are bulk-fetched in ONE round trip and indexed, then flags are set by direct
 * index. The previous implementation ran a messages.whose({id}) scan per
 * message (O(mailbox) each) and slept 1s between every set - holding Mail's
 * event loop for ~100s on a full 100-message batch, which froze the UI. This
 * version keeps a short 50ms yield between sets, so a full batch stays under
 * ~5s of automation.
 */

ObjC.import('Foundation');

// Yield between individual flag sets so Mail's event loop can service the UI.
// 50ms (vs the old 1000ms) keeps a 100-message batch under ~5s instead of ~100s.
var YIELD_SECONDS = 0.05;

function run(argv) {
  var inputPath = null;
  for (var i = 0; i < argv.length; i++) {
    if (argv[i] === '--input' && i + 1 < argv.length) {
      inputPath = argv[++i];
    }
  }

  if (!inputPath) {
    writeStderr('--input <path> is required');
    $.exit(1);
  }

  var nsContent = $.NSString.stringWithContentsOfFileEncodingError(
    $(inputPath), $.NSUTF8StringEncoding, null
  );
  if (!nsContent || !nsContent.isKindOfClass || !nsContent.isKindOfClass($.NSString)) {
    writeStderr('Failed to read input file: ' + inputPath);
    $.exit(1);
  }

  var assignments;
  try {
    assignments = JSON.parse(ObjC.unwrap(nsContent));
  } catch (e) {
    writeStderr('Failed to parse input JSON: ' + e.message);
    $.exit(1);
  }

  var mail = Application('Mail');
  var ok = 0;
  var failed = 0;

  // ── Group assignments by account+mailbox ──────────────────────────────────
  // Lets us resolve each mailbox once and bulk-fetch its ids a single time,
  // rather than re-resolving accounts/mailboxes and scanning per message.
  // The key is a JSON-encoded [account, mailbox] pair so names containing
  // spaces or other separators (e.g. "All Mail") can never collide.
  var groups = {};  // key -> { account, mailbox, items: [{ id, flagIndex }] }
  var order = [];   // preserve first-seen group order for stable logging
  for (var i = 0; i < assignments.length; i++) {
    var a = assignments[i];
    var msgId = (typeof a.id === 'number') ? a.id : parseInt(a.id, 10);
    if (isNaN(msgId)) {
      writeStderr('Skipping entry with non-numeric id: ' + JSON.stringify(a.id));
      failed++;
      continue;
    }
    var key = JSON.stringify([a.account, a.mailbox]);
    if (!groups[key]) {
      groups[key] = { account: a.account, mailbox: a.mailbox, items: [] };
      order.push(key);
    }
    groups[key].items.push({ id: msgId, flagIndex: a.flagIndex });
  }

  // ── Apply flags one mailbox at a time ─────────────────────────────────────
  for (var gi = 0; gi < order.length; gi++) {
    var g = groups[order[gi]];

    var mailbox;
    try {
      var account = mail.accounts.byName(g.account);
      mailbox = account.mailboxes.byName(g.mailbox);
    } catch (e) {
      writeStderr('Cannot resolve mailbox ' + g.account + '/' + g.mailbox + ': ' + e.message);
      failed += g.items.length;
      continue;
    }

    // One round trip: fetch every message id in this mailbox, then build an
    // id -> array-index map. Index order matches mailbox.messages, so we can
    // set flags via messages.at(idx) without a per-message whose() scan.
    var allIds;
    try {
      allIds = mailbox.messages.id();
    } catch (e) {
      writeStderr('Bulk id fetch failed for ' + g.account + '/' + g.mailbox + ': ' + e.message);
      failed += g.items.length;
      continue;
    }
    var idToIdx = {};
    for (var j = 0; j < allIds.length; j++) {
      idToIdx[allIds[j]] = j;
    }

    var msgs = mailbox.messages;
    for (var k = 0; k < g.items.length; k++) {
      var item = g.items[k];
      var idx = idToIdx[item.id];
      if (idx === undefined) {
        writeStderr('Not found: id=' + item.id + ' (' + g.account + '/' + g.mailbox + ')');
        failed++;
        continue;
      }
      try {
        // Setting flagIndex directly is sufficient - a non-zero value marks the
        // message as flagged with that color; -1 clears the flag. Avoid setting
        // flaggedStatus first: doing so triggers an IMAP sync which appears to
        // put the message in a transitional state that causes the subsequent
        // flagIndex assignment to throw "AppleEvent handler failed".
        msgs.at(idx).flagIndex = item.flagIndex;
        ok++;
      } catch (e) {
        writeStderr('Error flagging id=' + item.id + ': ' + e.message);
        failed++;
        continue;
      }
      $.NSThread.sleepForTimeInterval(YIELD_SECONDS);
    }
  }

  writeStderr(ok + ' flagged, ' + failed + ' failed');
}

function writeStderr(line) {
  var handle = $.NSFileHandle.fileHandleWithStandardError;
  var data = $(line + '\n').dataUsingEncoding($.NSUTF8StringEncoding);
  handle.writeData(data);
}
