"""Live SMTP session tests over real sockets."""

import tempfile
import unittest

from smtp_server import SMTPServer
from tests.helpers import SMTPClient, connect

HOSTNAME = "mail.example.org"


class LiveSMTPTest(unittest.TestCase):
    def start_server(self, **kwargs) -> SMTPServer:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        kwargs.setdefault("hostname", HOSTNAME)
        server = SMTPServer(spool_dir=self._tmp.name, **kwargs).start()
        self.addCleanup(server.shutdown)
        self.server = server
        return server

    def stored_bytes(self, recipient: str):
        paths = self.server.mailbox.paths_for(recipient)
        return [path.read_bytes() for path in paths]


class BasicCommandTests(LiveSMTPTest):
    def test_banner_helo_noop_quit(self):
        server = self.start_server()
        client = connect(server)
        self.addCleanup(client.close)

        code, lines = client.banner
        self.assertEqual(code, 220)
        self.assertIn(HOSTNAME, lines[0])
        self.assertIn("ESMTP", lines[0])

        code, lines = client.cmd("HELO client.example.net")
        self.assertEqual(code, 250)
        self.assertEqual(lines[0], HOSTNAME)

        self.assertEqual(client.cmd("NOOP")[0], 250)

        code, lines = client.cmd("QUIT")
        self.assertEqual(code, 221)
        self.assertEqual(lines[0], "Bye")
        client.expect_eof()
        self.assertEqual(server.connection_count, 1)

    def test_ehlo_multiline_advertises_size(self):
        server = self.start_server()
        client = connect(server)
        self.addCleanup(client.close)
        code, lines = client.cmd("EHLO client.example.net")
        self.assertEqual(code, 250)
        self.assertEqual(lines[0], HOSTNAME)  # first line: the hostname
        size_lines = [line for line in lines if line.startswith("SIZE ")]
        self.assertEqual(len(size_lines), 1)
        advertised = int(size_lines[0].split()[1])
        self.assertEqual(advertised, server.mailbox.max_message_bytes)
        self.assertIn("8BITMIME", lines)
        # the last line ends the multi-line block with "250 "
        self.assertTrue(client.cmd("NOOP")[0] == 250)

    def test_unknown_and_malformed_commands(self):
        self.start_server()
        client = connect(self.server)
        self.addCleanup(client.close)
        client.cmd("HELO client.example.net")

        self.assertEqual(client.cmd("FROBNICATE everything")[0], 500)
        self.assertEqual(client.cmd("HELO")[0], 501)  # missing argument
        self.assertEqual(client.cmd("DATA extra-arg")[0], 501)
        self.assertEqual(client.cmd("NOOP really")[0], 501)
        self.assertEqual(client.cmd("")[0], 500)  # blank line
        self.assertEqual(client.cmd("MAIL FROM:no-brackets")[0], 501)
        # with an open transaction, RCPT syntax is checked next...
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 250)
        self.assertEqual(client.cmd("RCPT TO:no-brackets")[0], 501)
        # ...and a duplicate MAIL is a state error
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 503)

    def test_line_too_long_gets_500_then_close(self):
        self.start_server()
        client = connect(self.server)
        self.addCleanup(client.close)
        client.send_raw(b"X" * 5000 + b"\r\n")
        code, _lines = client.read_reply()
        self.assertEqual(code, 500)
        client.expect_eof()


class TransactionTests(LiveSMTPTest):
    def test_full_session_stores_eml_with_dot_stuffing(self):
        self.start_server()
        client = connect(self.server)
        self.addCleanup(client.close)

        self.assertEqual(client.cmd("HELO client.example.net")[0], 250)
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 250)
        self.assertEqual(client.cmd("RCPT TO:<bob@example.net>")[0], 250)
        self.assertEqual(client.cmd("RCPT TO:<carol@example.net>")[0], 250)
        self.assertEqual(client.cmd("DATA")[0], 354)

        sent_lines = [
            "From: alice@example.org",
            "To: bob@example.net",
            "Subject: Dot test",
            "Date: Mon, 14 Nov 2026 12:00:00 +0000",
            "X-Note: folded",
            "  continuation",
            "",
            "Hello Bob.",
            "",
            "..leading dots stay one",  # sender dot-stuffed: one dot removed
        ]
        code, _ = client.send_data(sent_lines)
        self.assertEqual(code, 250)

        # what the server was supposed to store (dot-unstuffed, CRLF lines)
        stored_lines = sent_lines[:-1] + [".leading dots stay one"]
        expected = "".join(line + "\r\n" for line in stored_lines).encode("utf-8")

        for recipient in ("bob@example.net", "carol@example.net"):
            stored = self.stored_bytes(recipient)
            self.assertEqual(len(stored), 1, msg=recipient)
            self.assertEqual(stored[0], expected, msg=recipient)

        from smtp_server import parse_headers

        headers, body = parse_headers(expected)
        self.assertEqual(headers["from"], "alice@example.org")
        self.assertEqual(headers["to"], "bob@example.net")
        self.assertEqual(headers["subject"], "Dot test")
        self.assertEqual(headers["date"], "Mon, 14 Nov 2026 12:00:00 +0000")
        self.assertEqual(
            headers["x-note"], "folded continuation"
        )  # obs-fold unfolded
        self.assertEqual(body, b"Hello Bob.\r\n\r\n.leading dots stay one\r\n")
        self.assertEqual(self.server.mailbox.delivered_count, 2)

    def test_data_without_rcpt_is_503(self):
        self.start_server()
        client = connect(self.server)
        self.addCleanup(client.close)
        client.cmd("HELO client.example.net")
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 250)
        code, _ = client.cmd("DATA")
        self.assertEqual(code, 503)

    def test_out_of_order_commands_return_503(self):
        self.start_server()
        client = connect(self.server)
        self.addCleanup(client.close)
        # MAIL before HELO
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 503)
        # RCPT before MAIL
        client.cmd("HELO client.example.net")
        self.assertEqual(client.cmd("RCPT TO:<bob@example.net>")[0], 503)
        # duplicate MAIL inside one transaction
        client.cmd("MAIL FROM:<alice@example.org>")
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 503)

    def test_rset_clears_transaction_state(self):
        self.start_server()
        client = connect(self.server)
        self.addCleanup(client.close)
        client.cmd("HELO client.example.net")
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 250)
        self.assertEqual(client.cmd("RCPT TO:<bob@example.net>")[0], 250)

        self.assertEqual(client.cmd("RSET")[0], 250)
        # the transaction is gone: RCPT has no MAIL to attach to...
        self.assertEqual(client.cmd("RCPT TO:<bob@example.net>")[0], 503)
        # ...and a fresh MAIL FROM is not a duplicate
        self.assertEqual(client.cmd("MAIL FROM:<alice@example.org>")[0], 250)
        # ...and DATA has no recipients again
        self.assertEqual(client.cmd("DATA")[0], 503)

        # the session still works end to end after RSET
        self.assertEqual(client.cmd("RCPT TO:<bob@example.net>")[0], 250)
        self.assertEqual(client.cmd("DATA")[0], 354)
        code, _ = client.send_data(["Subject: after reset", "", "still here"])
        self.assertEqual(code, 250)
        stored = self.stored_bytes("bob@example.net")
        self.assertEqual(len(stored), 1)
        self.assertIn(b"still here", stored[0])

    def test_two_sessions_keep_independent_transactions(self):
        self.start_server()
        first = connect(self.server)
        second = connect(self.server)
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        first.cmd("HELO one.example")
        first.cmd("MAIL FROM:<one@example.org>")
        first.cmd("RCPT TO:<one-recipient@example.net>")

        # second client completes a whole delivery mid-transaction
        second.cmd("HELO two.example")
        second.cmd("MAIL FROM:<two@example.org>")
        second.cmd("RCPT TO:<two-recipient@example.net>")
        self.assertEqual(second.cmd("DATA")[0], 354)
        code, _ = second.send_data(["Subject: from two", "", "second body"])
        self.assertEqual(code, 250)

        # first client's transaction survived untouched
        self.assertEqual(first.cmd("DATA")[0], 354)
        code, _ = first.send_data(["Subject: from one", "", "first body"])
        self.assertEqual(code, 250)

        one = self.stored_bytes("one-recipient@example.net")
        two = self.stored_bytes("two-recipient@example.net")
        self.assertEqual(len(one), 1)
        self.assertEqual(len(two), 1)
        self.assertIn(b"Subject: from one", one[0])
        self.assertNotIn(b"Subject: from two", one[0])
        self.assertIn(b"Subject: from two", two[0])
        self.assertEqual(self.server.mailbox.delivered_count, 2)


class LimitsAndUsersTests(LiveSMTPTest):
    def test_size_limit_enforced_with_552(self):
        self.start_server(max_message_bytes=512)
        client = connect(self.server)
        self.addCleanup(client.close)

        code, lines = client.cmd("EHLO client.example.net")
        self.assertEqual(code, 250)
        self.assertIn("SIZE 512", lines)

        client.cmd("MAIL FROM:<alice@example.org>")
        client.cmd("RCPT TO:<bob@example.net>")
        client.cmd("DATA")
        big_body = ["Subject: huge", "", "x" * 600]
        code, lines = client.send_data(big_body)
        self.assertEqual(code, 552)
        self.assertEqual(self.stored_bytes("bob@example.net"), [])
        self.assertEqual(self.server.mailbox.delivered_count, 0)

        # the session recovers and can deliver a small message afterwards
        client.cmd("MAIL FROM:<alice@example.org>")
        client.cmd("RCPT TO:<bob@example.net>")
        self.assertEqual(client.cmd("DATA")[0], 354)
        code, _ = client.send_data(["Subject: small", "", "tiny"])
        self.assertEqual(code, 250)
        self.assertEqual(len(self.stored_bytes("bob@example.net")), 1)

    def test_vrfy_with_known_users(self):
        self.start_server(users=["alice@example.org"])
        client = connect(self.server)
        self.addCleanup(client.close)

        code, lines = client.cmd("VRFY alice@example.org")
        self.assertEqual(code, 250)
        self.assertEqual(lines[0], "alice@example.org")

        code, _ = client.cmd("VRFY mallory@example.org")
        self.assertEqual(code, 550)
        self.assertEqual(client.cmd("VRFY")[0], 501)

        # recipient validation shares the same known-user set
        client.cmd("HELO client.example.net")
        client.cmd("MAIL FROM:<alice@example.org>")
        code, _ = client.cmd("RCPT TO:<mallory@example.org>")
        self.assertEqual(code, 550)
        code, _ = client.cmd("RCPT TO:<alice@example.org>")
        self.assertEqual(code, 250)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
