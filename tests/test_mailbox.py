"""Mailbox storage and header-parsing tests."""

import tempfile
import unittest
from pathlib import Path

from smtp_server import Mailbox, MessageTooLarge, mailbox_name, parse_headers


class HeaderParsingTests(unittest.TestCase):
    def test_extracts_standard_headers_and_body(self):
        raw = (
            b"From: alice@example.org\r\n"
            b"To: bob@example.net\r\n"
            b"Subject: Dot test\r\n"
            b"Date: Mon, 14 Nov 2026 12:00:00 +0000\r\n"
            b"X-Long: first part\r\n"
            b" second part\r\n"
            b"\r\n"
            b"Hello Bob.\r\n"
        )
        headers, body = parse_headers(raw)
        self.assertEqual(headers["from"], "alice@example.org")
        self.assertEqual(headers["to"], "bob@example.net")
        self.assertEqual(headers["subject"], "Dot test")
        self.assertEqual(headers["date"], "Mon, 14 Nov 2026 12:00:00 +0000")
        # folded continuation lines are unfolded with a single space
        self.assertEqual(headers["x-long"], "first part second part")
        self.assertEqual(body, b"Hello Bob.\r\n")

    def test_edge_cases(self):
        # no blank line at all → everything is body
        headers, body = parse_headers(b"just some bytes\r\nwith no headers")
        self.assertEqual(headers, {})
        self.assertEqual(body, b"just some bytes\r\nwith no headers")
        # completely empty input
        self.assertEqual(parse_headers(b""), ({}, b""))
        # duplicate headers keep the first occurrence (documented policy)
        headers, _ = parse_headers(b"Received: one\r\nReceived: two\r\n\r\nbody")
        self.assertEqual(headers["received"], "one")
        # header-looking lines without a colon are ignored
        headers, _ = parse_headers(b"Broken line\r\nSubject: ok\r\n\r\n")
        self.assertEqual(headers, {"subject": "ok"})
        # header names are case-insensitive
        headers, _ = parse_headers(b"SUBJECT: shout\r\n\r\n")
        self.assertEqual(headers["subject"], "shout")


class MailboxStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(self._tmp.name, max_message_bytes=4096)

    def test_deliver_writes_one_file_per_unique_recipient(self):
        data = b"From: a@b\r\n\r\nbody\r\n"
        paths = self.mailbox.deliver(
            "alice@example.org", ["bob@example.net", "carol@example.net", "bob@example.net"],
            data,
        )
        self.assertEqual(len(paths), 2)  # duplicates collapse
        for path in paths:
            self.assertEqual(path.read_bytes(), data)
            self.assertTrue(path.suffix == ".eml")
        self.assertEqual(self.mailbox.delivered_count, 2)

    def test_stored_message_roundtrip(self):
        data = (
            b"From: alice@example.org\r\n"
            b"Subject: Round trip\r\n"
            b"Date: Tue, 15 Nov 2026 08:30:00 +0000\r\n"
            b"\r\n"
            b"Payload line.\r\n"
        )
        self.mailbox.deliver("alice@example.org", ["bob@example.net"], data)
        messages = self.mailbox.messages_for("bob@example.net")
        self.assertEqual(len(messages), 1)
        stored = messages[0]
        self.assertEqual(stored.raw, data)
        self.assertEqual(stored.subject, "Round trip")
        self.assertEqual(stored.from_header, "alice@example.org")
        self.assertEqual(stored.date, "Tue, 15 Nov 2026 08:30:00 +0000")
        self.assertEqual(stored.body, b"Payload line.\r\n")
        self.assertEqual(stored.recipients, ["bob@example.net"])
        # another mailbox sees nothing
        self.assertEqual(self.mailbox.paths_for("nobody@example.org"), [])

    def test_oversized_message_stores_nothing(self):
        with self.assertRaises(MessageTooLarge):
            self.mailbox.deliver(
                "alice@example.org", ["bob@example.net"], b"x" * 5000
            )
        self.assertEqual(self.mailbox.paths_for("bob@example.net"), [])
        self.assertEqual(self.mailbox.delivered_count, 0)

    def test_recipient_addresses_are_sanitized(self):
        self.mailbox.deliver(
            "a@b", ["weird/../addr@example.org", ".."], b"Subject: x\r\n\r\n\r\n"
        )
        # no directory escaped the spool root
        root = Path(self._tmp.name).resolve()
        for path in root.rglob("*.eml"):
            self.assertTrue(path.resolve().is_relative_to(root), msg=str(path))
        # dot-only names cannot become ".."
        self.assertEqual(mailbox_name(".."), "_")
        self.assertEqual(mailbox_name("."), "_")
        self.assertNotIn("/", mailbox_name("a/b@example.org"))
        self.assertNotIn("\\", mailbox_name("a\\b@example.org"))
        # distinct addresses stay distinct mailboxes
        self.assertNotEqual(
            mailbox_name("bob@example.net"), mailbox_name("bob+tag@example.net")
        )

    def test_empty_recipient_list_rejected(self):
        with self.assertRaises(ValueError):
            self.mailbox.deliver("a@b", [], b"data")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
