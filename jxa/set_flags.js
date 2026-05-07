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
 *                    where flagIndex is 0–6 (or -1 to clear):
 *                    0=red  1=orange  2=yellow  3=green  4=blue  5=purple  6=grey
 *
 * Exit 0 on success (partial failures are logged to stderr but not fatal).
 * Exit 1 on argument or I/O errors.
 */

ObjC.import('Foundation');

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

  for (var i = 0; i < assignments.length; i++) {
    var a = assignments[i];
    var msgId = (typeof a.id === 'number') ? a.id : parseInt(a.id, 10);
    if (isNaN(msgId)) {
      writeStderr('Skipping entry with non-numeric id: ' + JSON.stringify(a.id));
      failed++;
      continue;
    }

    try {
      var account = mail.accounts.byName(a.account);
      var mailbox = account.mailboxes.byName(a.mailbox);
      // whose() returns a specifier; calling it as a function materialises the array.
      var matches = mailbox.messages.whose({id: msgId})();
      if (matches.length === 0) {
        writeStderr('Not found: id=' + msgId + ' (' + a.account + '/' + a.mailbox + ')');
        failed++;
        continue;
      }
      var msg = matches[0];
      // Setting flagIndex directly is sufficient — a non-zero value marks the
      // message as flagged with that color; zero clears the flag. Avoid setting
      // flaggedStatus first: doing so triggers an IMAP sync which appears to
      // put the message in a transitional state that causes the subsequent
      // flagIndex assignment to throw "AppleEvent handler failed".
      msg.flagIndex = a.flagIndex;
      ok++;
      // Yield 1 s between messages so Mail's event loop can breathe and
      // handle user interactions between flag-set operations.
      if (i < assignments.length - 1) {
        $.NSThread.sleepForTimeInterval(1.0);
      }
    } catch (e) {
      writeStderr('Error flagging id=' + msgId + ': ' + e.message);
      failed++;
    }
  }

  writeStderr(ok + ' flagged, ' + failed + ' failed');
}

function writeStderr(line) {
  var handle = $.NSFileHandle.fileHandleWithStandardError;
  var data = $(line + '\n').dataUsingEncoding($.NSUTF8StringEncoding);
  handle.writeData(data);
}
