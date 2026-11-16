"""smtp-server — a real SMTP receiving server (mail drop) on raw sockets.

Public API::

    from smtp_server import SMTPServer, Mailbox, SMTPSession, parse_headers

    with SMTPServer(spool_dir="./spool") as server:
        print(f"smtp://127.0.0.1:{server.port}")
"""

import logging

from .mailbox import (
    DEFAULT_MAX_MESSAGE_BYTES,
    Mailbox,
    MessageTooLarge,
    StoredMail,
    mailbox_name,
    parse_headers,
)
from .server import SMTPServer
from .session import (
    COMMANDS,
    DEFAULT_HOSTNAME,
    SMTPSession,
    multiline_reply,
    parse_path_argument,
    reply,
)

logging.getLogger("smtp_server").addHandler(logging.NullHandler())

__all__ = [
    "COMMANDS",
    "DEFAULT_HOSTNAME",
    "DEFAULT_MAX_MESSAGE_BYTES",
    "Mailbox",
    "MessageTooLarge",
    "SMTPServer",
    "SMTPSession",
    "StoredMail",
    "mailbox_name",
    "multiline_reply",
    "parse_headers",
    "parse_path_argument",
    "reply",
]

__version__ = "1.0.0"
