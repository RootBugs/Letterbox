"""Mailbox storage: one ``.eml`` file per recipient, headers parsed.

Envelopes are stored as plain RFC 5322-ish files: the headers the sender
transmitted (From/To/Subject/Date/...) followed by a blank line and the
body, exactly as it arrived (after dot-unstuffing, which the session
performs).  Addresses are sanitized into safe directory names, so hostile
recipient strings can never escape the spool directory.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: Default cap on a single message (10 MiB), advertised via EHLO SIZE.
DEFAULT_MAX_MESSAGE_BYTES = 10 * 1024 * 1024

_SAFE = re.compile(r"[^A-Za-z0-9._+=-]+")


class MessageTooLarge(Exception):
    """The message exceeds the configured size limit; nothing is stored."""


def mailbox_name(address: str) -> str:
    """Map an email address to a safe spool directory name.

    Every run of characters outside ``[A-Za-z0-9._+=-]`` collapses to a
    single ``_``; an address that sanitizes to nothing becomes ``_``.
    """
    safe = _SAFE.sub("_", address.strip())
    # a name made only of dots ("." / "..") would escape the spool root
    if not safe or set(safe) <= {"."}:
        return "_"
    return safe


def parse_headers(data: bytes) -> Tuple[Dict[str, str], bytes]:
    """Split raw message bytes into ``(headers, body)``.

    * Header block ends at the first blank line (``\\r\\n\\r\\n`` or
      ``\\n\\n``); anything without a blank line is treated as all body.
    * Obsolete line folding is unfolded: a line starting with
      whitespace continues the previous header value.
    * Duplicate headers keep the **first** occurrence (documented
      policy); names are lower-cased for lookup.
    * Lines without a colon are ignored (they are not headers).
    """
    separator = None
    for marker in (b"\r\n\r\n", b"\n\n"):
        index = data.find(marker)
        if index != -1 and (separator is None or index < separator[0]):
            separator = (index, marker)
    if separator is None:
        return {}, data
    index, marker = separator
    head, body = data[:index], data[index + len(marker) :]

    headers_dict: Dict[str, str] = {}
    current_name: Optional[str] = None
    for raw_line in head.split(b"\n"):
        line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
        text = line.decode("latin-1")
        if text[:1] in (" ", "\t"):
            if current_name is not None:
                headers_dict[current_name] = (
                    headers_dict[current_name] + " " + text.strip()
                )
            continue
        if not text:
            continue
        if ":" not in text:
            current_name = None
            continue
        name, _, value = text.partition(":")
        name = name.strip().lower()
        if not name:
            current_name = None
            continue
        current_name = name
        if name not in headers_dict:
            headers_dict[name] = value.strip()
    return headers_dict, body


class StoredMail:
    """One delivered message as read back from disk.

    ``raw`` is the exact bytes that were stored (the sender's headers
    plus body, after dot-unstuffing); ``headers``/``body`` are the
    parsed view of it.  The SMTP envelope is *not* part of the file, so
    the stored bytes match what was sent — envelope information lives on
    the session that accepted the message.
    """

    __slots__ = ("path", "recipients", "headers", "body", "raw")

    def __init__(
        self,
        path: Path,
        recipients: Sequence[str],
        raw: bytes,
    ):
        self.path = path
        self.recipients = list(recipients)
        self.raw = raw
        self.headers, self.body = parse_headers(raw)

    @property
    def from_header(self) -> Optional[str]:
        return self.headers.get("from")

    @property
    def subject(self) -> Optional[str]:
        return self.headers.get("subject")

    @property
    def date(self) -> Optional[str]:
        return self.headers.get("date")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StoredMail {self.path.name} subject={self.subject!r}>"


class Mailbox:
    """Writes one ``.eml`` file per recipient under a spool directory.

    Args:
        spool_dir: root directory for per-recipient mailboxes.
        max_message_bytes: hard cap enforced on delivery; exceeding it
            raises :class:`MessageTooLarge` and stores nothing.
    """

    def __init__(self, spool_dir, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES):
        self.root = Path(spool_dir)
        self.max_message_bytes = max_message_bytes
        self.delivered_count = 0

    def directory_for(self, address: str) -> Path:
        """The (created on demand) mailbox directory for an address."""
        path = self.root / mailbox_name(address)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def deliver(
        self, envelope_from: str, recipients: Sequence[str], data: bytes
    ) -> List[Path]:
        """Store ``data`` once per unique recipient; returns written paths.

        Raises:
            MessageTooLarge: when ``data`` exceeds the size limit; no
                files are written in that case.
        """
        if len(data) > self.max_message_bytes:
            raise MessageTooLarge(
                f"message is {len(data)} bytes, limit is {self.max_message_bytes}"
            )
        unique: List[str] = []
        for recipient in recipients:
            if recipient not in unique:
                unique.append(recipient)
        if not unique:
            raise ValueError("delivery requires at least one recipient")
        paths: List[Path] = []
        stamp = f"{int(time.time())}"
        for recipient in unique:
            directory = self.directory_for(recipient)
            filename = f"{stamp}-{uuid.uuid4().hex[:12]}.eml"
            path = directory / filename
            path.write_bytes(data)
            paths.append(path)
        self.delivered_count += len(paths)
        return paths

    def paths_for(self, address: str) -> List[Path]:
        """Every stored message path for one recipient, oldest first."""
        directory = self.root / mailbox_name(address)
        if not directory.is_dir():
            return []
        return sorted(directory.glob("*.eml"))

    def messages_for(self, address: str) -> List[StoredMail]:
        """Read back every stored message for one recipient."""
        out = []
        for path in self.paths_for(address):
            out.append(StoredMail(path, [address], path.read_bytes()))
        return out
