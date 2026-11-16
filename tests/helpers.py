"""Minimal SMTP test client: replies, commands, DATA payloads."""

from __future__ import annotations

import socket
from typing import List, Tuple, Union

TIMEOUT = 5.0


class SMTPClient:
    """One SMTP conversation over a raw socket."""

    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        self._buf = bytearray()
        self.banner = self.read_reply()

    # -- reading -----------------------------------------------------------
    def _recv_some(self) -> None:
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("server closed the connection")
        self._buf.extend(data)

    def _read_line(self) -> bytes:
        while True:
            index = self._buf.find(b"\n")
            if index != -1:
                line = bytes(self._buf[:index])
                del self._buf[: index + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                return line
            self._recv_some()

    def read_reply(self) -> Tuple[int, List[str]]:
        """Read one (possibly multi-line) SMTP reply: (code, lines)."""
        first = self._read_line().decode("latin-1")
        code = int(first[:3])
        lines = [first[4:]]
        while first[3] == "-":
            continuation = self._read_line().decode("latin-1")
            self._assert_reply_shape(continuation, code)
            lines.append(continuation[4:])
            first = continuation
        return code, lines

    @staticmethod
    def _assert_reply_shape(text: str, expected: int) -> None:
        if len(text) < 4 or not text[:3].isdigit() or int(text[:3]) != expected:
            raise AssertionError(f"malformed reply line: {text!r}")
        if text[3] not in ("-", " "):
            raise AssertionError(f"malformed reply line: {text!r}")

    # -- writing -----------------------------------------------------------
    def cmd(self, command: str) -> Tuple[int, List[str]]:
        """Send one command line and read its reply."""
        self.sock.sendall(command.encode("utf-8") + b"\r\n")
        return self.read_reply()

    def send_data(self, lines: Union[str, List[str]]) -> Tuple[int, List[str]]:
        """Send a DATA payload (list of lines or one string) + terminator."""
        if isinstance(lines, str):
            payload = lines
        else:
            payload = "".join(line + "\r\n" for line in lines)
        self.sock.sendall(payload.encode("utf-8") + b".\r\n")
        return self.read_reply()

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def expect_eof(self, timeout: float = 5.0) -> None:
        """Assert the server closes the connection from here on."""
        self.sock.settimeout(timeout)
        try:
            got = self.sock.recv(4096)
        except (socket.timeout, TimeoutError):
            raise AssertionError("expected EOF, connection still open") from None
        finally:
            self.sock.settimeout(TIMEOUT)
        if got != b"":
            raise AssertionError(f"expected EOF, got {got!r}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "SMTPClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(server) -> SMTPClient:
    return SMTPClient("127.0.0.1", server.port)
