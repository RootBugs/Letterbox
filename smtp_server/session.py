"""SMTP session state machine (RFC 5321 command loop, minus the auth).

States::

    GREETING ──HELO/EHLO──► READY ──MAIL FROM──► TRANSACTION
        │                     ▲                     │
        │                     │  RSET / delivery     │ RCPT TO (repeatable)
        │                     └─────────────────────┘
        └── VRFY/NOOP/RSET/QUIT allowed before greeting completes

The session is socket-agnostic: ``handle_line``/``feed_data_line`` take
one line of input and return the reply bytes to write (or ``None`` while
a DATA payload is still arriving), so the protocol logic is unit-testable
and the TCP server stays a thin shell.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from .mailbox import Mailbox, MessageTooLarge

#: Default hostname used in greetings and HELO replies.
DEFAULT_HOSTNAME = "localhost"

#: Commands accepted by the server, with their required argument policy:
#: ``True`` = argument mandatory, ``False`` = argument forbidden/ignored.
COMMANDS: Dict[str, bool] = {
    "HELO": True,
    "EHLO": True,
    "MAIL": True,
    "RCPT": True,
    "DATA": False,
    "RSET": False,
    "NOOP": False,
    "QUIT": False,
    "VRFY": True,
    "HELP": False,
}


def reply(code: int, text: str) -> bytes:
    """Single-line reply bytes."""
    return f"{code} {text}\r\n".encode("utf-8")


def multiline_reply(code: int, lines: Sequence[str]) -> bytes:
    """Multi-line reply: ``code-`` on all but the last line."""
    out = bytearray()
    for index, line in enumerate(lines):
        separator = "-" if index < len(lines) - 1 else " "
        out.extend(f"{code}{separator}{line}\r\n".encode("utf-8"))
    return bytes(out)


def parse_path_argument(arg: str, keyword: str) -> Optional[str]:
    """Extract ``<address>`` after ``FROM:``/``TO:`` (case-insensitive).

    Returns the bare address (possibly empty for the null sender) or
    ``None`` when the syntax is wrong.
    """
    if not arg:
        return None
    if not arg.upper().startswith(keyword):
        return None
    rest = arg[len(keyword) :].lstrip()
    if not rest.startswith("<"):
        return None
    end = rest.find(">")
    if end == -1:
        return None
    if rest[end + 1 :].strip():
        return None
    return rest[1:end]


class SMTPSession:
    """One client conversation: command parsing, transaction, delivery.

    Args:
        mailbox: where delivered messages are stored.
        hostname: server name for greetings/HELO replies.
        max_message_bytes: DATA size cap (advertised via EHLO ``SIZE``).
        users: optional set of known addresses for ``VRFY``; without it
            ``VRFY`` accepts any syntactically valid address.
        on_delivery: callback ``(envelope_from, recipients, paths)``
            fired after a successful delivery.
    """

    def __init__(
        self,
        mailbox: Mailbox,
        hostname: str = DEFAULT_HOSTNAME,
        max_message_bytes: Optional[int] = None,
        users: Optional[Sequence[str]] = None,
        on_delivery: Optional[Callable] = None,
    ):
        self.mailbox = mailbox
        self.hostname = hostname
        self.max_message_bytes = max_message_bytes or mailbox.max_message_bytes
        self.users = set(users) if users is not None else None
        self.on_delivery = on_delivery
        self.greeted = False
        self.client_name = ""
        self.envelope_from: Optional[str] = None
        self.recipients: List[str] = []
        self.in_data = False
        self.close_requested = False
        self.messages_delivered = 0
        self._data_lines: List[bytes] = []
        self._data_size = 0
        self._data_oversize = False

    # -- helpers -----------------------------------------------------------
    def greeting(self) -> bytes:
        """The 220 banner sent as soon as the connection opens."""
        return reply(220, f"{self.hostname} ESMTP ready")

    @property
    def transaction_open(self) -> bool:
        return self.envelope_from is not None

    def _reset_transaction(self) -> None:
        self.envelope_from = None
        self.recipients = []

    # -- command mode ------------------------------------------------------
    def handle_line(self, line: bytes) -> List[bytes]:
        """Handle one command line; returns the replies to send."""
        text = line.decode("latin-1")
        if not text.strip():
            return [reply(500, "Error: bad syntax")]
        verb, _, arg = text.partition(" ")
        verb = verb.upper()
        arg = arg.strip()
        if verb not in COMMANDS:
            return [reply(500, "Error: command not recognized")]
        if COMMANDS[verb] and not arg:
            return [reply(501, f"Error: {verb} requires an argument")]
        if not COMMANDS[verb] and arg and verb in ("DATA", "RSET", "NOOP", "QUIT"):
            return [reply(501, f"Error: {verb} does not take an argument")]

        handler = getattr(self, f"_cmd_{verb.lower()}")
        return handler(arg)

    def _cmd_helo(self, arg: str) -> List[bytes]:
        return self._greet(arg)

    def _cmd_ehlo(self, arg: str) -> List[bytes]:
        return self._greet(arg, ehlo=True)

    def _greet(self, arg: str, ehlo: bool = False) -> List[bytes]:
        # A fresh HELO/EHLO abandons any half-open transaction.
        self._reset_transaction()
        self.in_data = False
        self.greeted = True
        self.client_name = arg
        if ehlo:
            lines = [
                self.hostname,
                f"SIZE {self.max_message_bytes}",
                "8BITMIME",
                "HELP",
            ]
            return [multiline_reply(250, lines)]
        return [reply(250, self.hostname)]

    def _cmd_mail(self, arg: str) -> List[bytes]:
        if not self.greeted:
            return [reply(503, "Error: send HELO/EHLO first")]
        if self.transaction_open:
            return [reply(503, "Error: duplicate MAIL FROM command")]
        address = parse_path_argument(arg, "FROM:")
        if address is None:
            return [reply(501, "Error: expected 'MAIL FROM:<address>'")]
        self.envelope_from = address
        self.recipients = []
        return [reply(250, "OK")]

    def _cmd_rcpt(self, arg: str) -> List[bytes]:
        if not self.greeted:
            return [reply(503, "Error: send HELO/EHLO first")]
        if not self.transaction_open:
            return [reply(503, "Error: need MAIL FROM first")]
        address = parse_path_argument(arg, "TO:")
        if address is None:
            return [reply(501, "Error: expected 'RCPT TO:<address>'")]
        if not address:
            return [reply(501, "Error: empty recipient address")]
        if self.users is not None and address not in self.users:
            return [reply(550, f"Error: no such user {address}")]
        if address not in self.recipients:
            self.recipients.append(address)
        return [reply(250, "OK")]

    def _cmd_data(self, arg: str) -> List[bytes]:
        if not self.greeted:
            return [reply(503, "Error: send HELO/EHLO first")]
        if not self.transaction_open:
            return [reply(503, "Error: need MAIL FROM first")]
        if not self.recipients:
            return [reply(503, "Error: need RCPT TO before DATA")]
        self.in_data = True
        self._data_lines = []
        self._data_size = 0
        self._data_oversize = False
        return [reply(354, "End data with <CR><LF>.<CR><LF>")]

    def _cmd_rset(self, arg: str) -> List[bytes]:
        self._reset_transaction()
        self.in_data = False
        return [reply(250, "OK")]

    def _cmd_noop(self, arg: str) -> List[bytes]:
        return [reply(250, "OK")]

    def _cmd_quit(self, arg: str) -> List[bytes]:
        self.close_requested = True
        return [reply(221, "Bye")]

    def _cmd_vrfy(self, arg: str) -> List[bytes]:
        address = arg
        if not address or any(ch.isspace() for ch in address):
            return [reply(501, "Error: VRFY needs an address")]
        if self.users is not None:
            if address in self.users:
                return [reply(250, address)]
            return [reply(550, f"Error: no such user {address}")]
        return [reply(250, address)]

    def _cmd_help(self, arg: str) -> List[bytes]:
        commands = " ".join(sorted(COMMANDS))
        return [reply(214, commands)]

    # -- data mode ---------------------------------------------------------
    def feed_data_line(self, line: bytes) -> Optional[bytes]:
        """Feed one line of the DATA payload.

        Returns ``None`` while the message is still arriving, or the
        final reply (``250`` on delivery, ``552`` when oversized) once
        the terminating ``.`` line arrives.
        """
        if line == b".":
            return self._finish_data()
        if line.startswith(b"."):
            line = line[1:]  # undo the sender's dot-stuffing
        self._data_lines.append(line)
        self._data_size += len(line) + 2  # CRLF that will be re-added
        if self._data_size > self.max_message_bytes:
            self._data_oversize = True
        return None

    def mark_data_oversize(self) -> None:
        """Flag an oversized payload whose line could not even be buffered."""
        self._data_oversize = True

    def _finish_data(self) -> bytes:
        data = b"\r\n".join(self._data_lines) + (b"\r\n" if self._data_lines else b"")
        oversize = self._data_oversize or len(data) > self.max_message_bytes
        envelope_from = self.envelope_from or ""
        recipients = list(self.recipients)
        self._reset_transaction()
        self.in_data = False
        self._data_lines = []
        self._data_size = 0
        self._data_oversize = False
        if oversize:
            return reply(552, "Error: message size exceeds limit")
        try:
            paths = self.mailbox.deliver(envelope_from, recipients, data)
        except MessageTooLarge:
            return reply(552, "Error: message size exceeds limit")
        self.messages_delivered += len(paths)
        if self.on_delivery is not None:
            self.on_delivery(envelope_from, recipients, paths)
        reference = paths[0].stem if paths else "queue"
        return reply(250, f"OK: queued as {reference}")
