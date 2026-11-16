"""Threaded TCP server speaking SMTP on raw sockets.

Connection flow::

    accept ─► 220 banner ─► line loop
                │
                ├─ command mode: session.handle_line() → reply bytes
                └─ data mode:    session.feed_data_line() → None | final reply
        QUIT (221) / EOF / protocol abuse (500 + close) ─► socket closed

One handler thread per connection, an idle timeout so ``shutdown()``
never hangs, ephemeral port by default, and counters for connections and
delivered messages.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import List, Optional, Tuple

from .mailbox import DEFAULT_MAX_MESSAGE_BYTES, Mailbox
from .session import DEFAULT_HOSTNAME, SMTPSession, reply

logger = logging.getLogger("smtp_server")

#: Idle socket timeout — lets blocked handlers notice shutdown().
CLIENT_TIMEOUT = 1.0
#: Maximum command-line length (RFC 5321 §4.5.3.1 is 512; we allow 4096).
MAX_COMMAND_LINE = 4096


class LineTooLong(Exception):
    """A line exceeded the configured limit; the reader resynced past it."""


class LineReader:
    """Byte-oriented line reader with a hard per-line limit.

    Lines are returned **without** their terminator (``\\r\\n``, ``\\n``
    or bare ``\\r`` is stripped), so callers see plain command/payload
    text.  On overflow it consumes up to the newline first, so the next
    read always starts at a real line boundary.
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()

    def _fill(self) -> None:
        data = self._sock.recv(65536)
        if not data:
            raise ConnectionError("connection closed by peer")
        self._buf.extend(data)

    def readline(self, limit: int) -> bytes:
        """Read one line within ``limit`` bytes or raise :class:`LineTooLong`."""
        while True:
            index = self._buf.find(b"\n")
            if index != -1:
                if index + 1 > limit:
                    del self._buf[: index + 1]  # consume the oversized line
                    raise LineTooLong(f"line exceeds {limit} bytes")
                line = bytes(self._buf[:index])
                del self._buf[: index + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                return line
            if len(self._buf) > limit:
                del self._buf[:]
                raise LineTooLong(f"line exceeds {limit} bytes")
            self._fill()

    def drain_line(self) -> None:
        """Throw away input up to (and including) the next newline."""
        while True:
            index = self._buf.find(b"\n")
            if index != -1:
                del self._buf[: index + 1]
                return
            self._buf.clear()
            self._fill()


class SMTPServer:
    """A threading SMTP receiving server.

    Args:
        spool_dir: directory for per-recipient mailboxes.
        host: bind address (default ``127.0.0.1``).
        port: bind port; ``0`` selects an ephemeral port.
        hostname: server name used in banners and HELO replies.
        max_message_bytes: DATA size cap (also advertised via EHLO SIZE).
        users: optional known addresses for ``VRFY``.

    Example::

        with SMTPServer(spool_dir="./spool") as server:
            print(f"smtp://127.0.0.1:{server.port}")
    """

    def __init__(
        self,
        spool_dir,
        host: str = "127.0.0.1",
        port: int = 0,
        hostname: str = DEFAULT_HOSTNAME,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        users: Optional[List[str]] = None,
    ):
        self.host = host
        self.hostname = hostname
        self.mailbox = Mailbox(spool_dir, max_message_bytes)
        self.users = users
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients: List[socket.socket] = []
        self._threads: List[threading.Thread] = []
        self._bound_port = 0
        self.connection_count = 0

    # -- lifecycle ---------------------------------------------------------
    @property
    def port(self) -> int:
        return self._bound_port

    @property
    def address(self) -> Tuple[str, int]:
        return (self.host, self._bound_port)

    @property
    def running(self) -> bool:
        return self._sock is not None and not self._stop.is_set()

    def start(self) -> "SMTPServer":
        if self._sock is not None:
            raise RuntimeError("server already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, 0))
        sock.listen(32)
        self._sock = sock
        self._bound_port = sock.getsockname()[1]
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="smtp-accept", daemon=True
        )
        self._accept_thread.start()
        return self

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._sock is None:
            return
        self._stop.set()
        sock, self._sock = self._sock, None
        try:
            sock.close()
        except OSError:
            pass
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            for action in (
                lambda c=client: c.shutdown(socket.SHUT_RDWR),
                lambda c=client: c.close(),
            ):
                try:
                    action()
                except OSError:
                    pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout)
            self._accept_thread = None
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)
        with self._lock:
            self._threads.clear()

    def __enter__(self) -> "SMTPServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.shutdown()

    # -- internals ---------------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                break
            with self._lock:
                self.connection_count += 1
                index = self.connection_count
                self._clients.append(conn)
                thread = threading.Thread(
                    target=self._serve_client,
                    args=(conn,),
                    name=f"smtp-conn-{index}",
                    daemon=True,
                )
                self._threads.append(thread)
            thread.start()

    def _serve_client(self, conn: socket.socket) -> None:
        conn.settimeout(CLIENT_TIMEOUT)
        reader = LineReader(conn)
        session = SMTPSession(
            self.mailbox,
            hostname=self.hostname,
            max_message_bytes=self.mailbox.max_message_bytes,
            users=self.users,
        )
        try:
            conn.sendall(session.greeting())
            while not self._stop.is_set():
                try:
                    if session.in_data:
                        try:
                            line = reader.readline(self.mailbox.max_message_bytes + 65536)
                        except LineTooLong:
                            reader.drain_line()
                            session.mark_data_oversize()
                            continue
                        final = session.feed_data_line(line)
                        if final is not None:
                            conn.sendall(final)
                    else:
                        try:
                            line = reader.readline(MAX_COMMAND_LINE)
                        except LineTooLong:
                            conn.sendall(reply(500, "Error: line too long"))
                            break
                        for payload in session.handle_line(line):
                            conn.sendall(payload)
                        if session.close_requested:
                            break
                except TimeoutError:
                    continue  # idle connection; re-check stop flag
                except (ConnectionError, OSError):
                    break
        except OSError:
            pass  # socket torn down by shutdown()
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass
