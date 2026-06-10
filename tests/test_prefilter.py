"""
Tests for prefilter.filter_message.

Each drop rule is verified with a minimal message that triggers ONLY that
rule (so the ordering guarantee is implicitly validated — earlier rules are
always False in that message).
"""

import pytest
from prefilter import filter_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(
    *,
    subject: str = "Hello",
    sender: str = "Alice <alice@example.com>",
    junk: bool = False,
    headers: str = "",
) -> dict:
    """Construct a minimal valid message dict."""
    return {"subject": subject, "sender": sender, "junk": junk, "headers": headers}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestNormalMessage:
    def test_keep_plain_message(self):
        msg = _make_msg(
            subject="Lunch tomorrow?",
            sender="Bob <bob@example.com>",
            junk=False,
            headers=(
                "From: Bob <bob@example.com>\r\n"
                "Subject: Lunch tomorrow?\r\n"
                "To: alice@example.com\r\n"
            ),
        )
        keep, reason = filter_message(msg)
        assert keep is True
        assert reason is None


# ---------------------------------------------------------------------------
# Rule 1 — junk_flag
# ---------------------------------------------------------------------------

class TestJunkFlag:
    def test_junk_true_drops(self):
        msg = _make_msg(junk=True)
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "junk_flag"

    def test_junk_false_passes(self):
        msg = _make_msg(junk=False)
        keep, reason = filter_message(msg)
        # May be kept or dropped for other reasons; just not junk_flag
        assert reason != "junk_flag"

    def test_junk_flag_wins_over_list_unsubscribe(self):
        """junk_flag is checked before list_unsubscribe (rule order)."""
        msg = _make_msg(
            junk=True,
            headers="List-Unsubscribe: <mailto:unsub@example.com>\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "junk_flag"


# ---------------------------------------------------------------------------
# Rule 2 — own_sender
# ---------------------------------------------------------------------------

class TestOwnSender:
    @pytest.mark.parametrize("sender", [
        "martin.aryee@gmail.com",
        "Martin Aryee <martin.aryee@gmail.com>",
        "martin.aryee@ds.dfci.harvard.edu",
        "Martin Aryee <martin.aryee@ds.dfci.harvard.edu>",
        "MARTIN.ARYEE@GMAIL.COM",
    ])
    def test_own_address_drops(self, sender):
        msg = _make_msg(sender=sender)
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "own_sender"

    def test_other_sender_passes(self):
        msg = _make_msg(sender="Alice <alice@example.com>")
        keep, reason = filter_message(msg)
        assert reason != "own_sender"

    def test_own_sender_wins_over_list_unsubscribe(self):
        """own_sender is checked before list_unsubscribe."""
        msg = _make_msg(
            sender="martin.aryee@gmail.com",
            headers="List-Unsubscribe: <mailto:unsub@example.com>\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "own_sender"


# ---------------------------------------------------------------------------
# Rule 3 — auto_submitted
# ---------------------------------------------------------------------------

class TestAutoSubmitted:
    def test_auto_generated_drops(self):
        msg = _make_msg(
            headers="Auto-Submitted: auto-generated\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "auto_submitted"

    def test_auto_replied_drops(self):
        msg = _make_msg(
            headers="Auto-Submitted: auto-replied\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "auto_submitted"

    def test_auto_submitted_no_passes(self):
        """'auto-submitted: no' explicitly means NOT auto-submitted (RFC 3834)."""
        msg = _make_msg(
            headers="Auto-Submitted: no\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is True
        assert reason is None

    def test_auto_submitted_case_insensitive(self):
        msg = _make_msg(
            headers="Auto-Submitted: AUTO-GENERATED\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "auto_submitted"

    def test_auto_submitted_header_name_case_insensitive(self):
        msg = _make_msg(
            headers="auto-submitted: auto-notified\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "auto_submitted"


# ---------------------------------------------------------------------------
# Rule 4 — precedence_bulk
# ---------------------------------------------------------------------------

class TestPrecedenceBulk:
    def test_precedence_bulk_drops(self):
        msg = _make_msg(
            headers="Precedence: bulk\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "precedence_bulk"

    def test_precedence_list_drops(self):
        msg = _make_msg(
            headers="Precedence: list\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "precedence_bulk"

    def test_precedence_junk_drops(self):
        msg = _make_msg(
            headers="Precedence: junk\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "precedence_bulk"

    def test_precedence_normal_passes(self):
        msg = _make_msg(
            headers="Precedence: normal\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is True
        assert reason is None

    def test_precedence_case_insensitive(self):
        msg = _make_msg(
            headers="Precedence: BULK\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "precedence_bulk"

    def test_precedence_bulk_wins_over_list_unsubscribe(self):
        """precedence_bulk is checked before list_unsubscribe."""
        msg = _make_msg(
            headers=(
                "Precedence: bulk\r\n"
                "List-Unsubscribe: <mailto:unsub@example.com>\r\n"
            ),
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "precedence_bulk"


# ---------------------------------------------------------------------------
# Rule 5 — list_unsubscribe
# ---------------------------------------------------------------------------

class TestListUnsubscribe:
    def test_list_unsubscribe_drops(self):
        msg = _make_msg(
            headers="List-Unsubscribe: <mailto:unsub@newsletter.com>\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "list_unsubscribe"

    def test_list_unsubscribe_case_insensitive(self):
        msg = _make_msg(
            headers="list-unsubscribe: <mailto:unsub@newsletter.com>\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "list_unsubscribe"

    def test_list_unsubscribe_wins_over_no_reply(self):
        """list_unsubscribe is checked before no_reply_sender."""
        msg = _make_msg(
            sender="noreply@example.com",
            headers="List-Unsubscribe: <mailto:unsub@example.com>\r\n",
        )
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "list_unsubscribe"


# ---------------------------------------------------------------------------
# Rule 6 — no_reply_sender
# ---------------------------------------------------------------------------

class TestNoReplySender:
    @pytest.mark.parametrize("sender", [
        "noreply@example.com",
        "no-reply@example.com",
        "no_reply@example.com",
        "donotreply@example.com",
        "do-not-reply@example.com",
        "mailer-daemon@example.com",
        "notifications@example.com",
        "notification@example.com",
        "mail-noreply@example.com",
        "bounces@example.com",
    ])
    def test_exact_local_parts_drop(self, sender):
        msg = _make_msg(sender=sender)
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "no_reply_sender"

    def test_noreply_mixed_case_drops(self):
        """<NoReply@example.com> must be caught (case-insensitive)."""
        msg = _make_msg(sender="<NoReply@example.com>")
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "no_reply_sender"

    def test_notifications_svc_prefix_drops(self):
        """notifications-svc@ matches because 'notifications' is a known prefix."""
        msg = _make_msg(sender="notifications-svc@example.com")
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "no_reply_sender"

    def test_noreply_underscore_prefix_drops(self):
        msg = _make_msg(sender="noreply_alerts@example.com")
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "no_reply_sender"

    def test_replytome_passes(self):
        """replytome@ must NOT be dropped — it's not a known pattern."""
        msg = _make_msg(sender="replytome@example.com")
        keep, reason = filter_message(msg)
        assert keep is True
        assert reason is None

    def test_noreply_with_angle_brackets_and_display_name(self):
        msg = _make_msg(sender="GitHub <noreply@github.com>")
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "no_reply_sender"

    def test_bounces_prefix_drops(self):
        """bounces-abc123@example.com should match (prefix rule)."""
        msg = _make_msg(sender="bounces-abc123@example.com")
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "no_reply_sender"

    def test_normal_sender_passes(self):
        msg = _make_msg(sender="Alice Smith <alice@company.com>")
        keep, reason = filter_message(msg)
        assert keep is True
        assert reason is None


# ---------------------------------------------------------------------------
# Header parsing edge cases
# ---------------------------------------------------------------------------

class TestHeaderParsing:
    def test_folded_header_does_not_crash(self):
        """Folded (continuation) lines must be handled without errors."""
        headers = (
            "Subject: This is a very long subject that\r\n"
            "  continues on the next line\r\n"
            "List-Unsubscribe: <mailto:unsub@example.com>,\r\n"
            "  <https://example.com/unsub>\r\n"
        )
        msg = _make_msg(headers=headers)
        keep, reason = filter_message(msg)
        # Should not raise; List-Unsubscribe is present so should drop.
        assert keep is False
        assert reason == "list_unsubscribe"

    def test_empty_headers_string_does_not_crash(self):
        msg = _make_msg(headers="")
        keep, reason = filter_message(msg)
        assert keep is True
        assert reason is None

    def test_crlf_and_lf_headers(self):
        """Both \\r\\n and \\n line endings are accepted."""
        headers_lf = "List-Unsubscribe: <mailto:unsub@example.com>\n"
        msg = _make_msg(headers=headers_lf)
        keep, reason = filter_message(msg)
        assert keep is False
        assert reason == "list_unsubscribe"

    def test_headers_with_multiple_values_uses_first(self):
        """Only the first occurrence of a header is used."""
        headers = (
            "Precedence: normal\r\n"
            "Precedence: bulk\r\n"  # second occurrence — should be ignored
        )
        msg = _make_msg(headers=headers)
        keep, reason = filter_message(msg)
        # First 'Precedence' is 'normal' → no drop from precedence rule
        assert reason != "precedence_bulk"
