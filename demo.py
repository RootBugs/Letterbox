"""Runnable example: send a mail drop session to a live server.

Run with:  python demo.py
"""

import shutil
import tempfile
from pathlib import Path

from smtp_server import SMTPServer

TRANSCRIPT = [
    "HELO demo.client",
    "MAIL FROM:<alice@example.org>",
    "RCPT TO:<bob@example.net>",
    "RCPT TO:<carol@example.net>",
    "DATA",
]


def read_reply(sock_file) -> tuple:
    """Read one (possibly multi-line) reply; returns (code, lines)."""
    first = sock_file.readline().decode("utf-8").rstrip("\r\n")
    code = int(first[:3])
    lines = [first[4:]]
    while first[3] == "-":
        first = sock_file.readline().decode("utf-8").rstrip("\r\n")
        lines.append(first[4:])
    return code, lines


def main() -> None:
    spool = Path(tempfile.mkdtemp(prefix="smtp-demo-"))
    try:
        with SMTPServer(spool_dir=spool, hostname="mail.example.org") as server:
            print(f"smtp://127.0.0.1:{server.port}  spool={spool}")

            import socket

            sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            reader = sock.makefile("rb")

            code, lines = read_reply(reader)
            print(f"<- {code} {lines[0]}")

            for command in TRANSCRIPT:
                sock.sendall(command.encode() + b"\r\n")
                code, lines = read_reply(reader)
                print(f"> {command}")
                print(f"<- {code} {' | '.join(lines)}")
                if command == "DATA":
                    body = (
                        "From: alice@example.org\r\n"
                        "To: bob@example.net\r\n"
                        "Subject: Demo message\r\n"
                        "Date: Sat, 14 Nov 2026 12:00:00 +0000\r\n"
                        "\r\n"
                        "Hello from the demo!\r\n"
                        "..a dot-stuffed line\r\n"
                    )
                    sock.sendall(body.encode() + b".\r\n")
                    code, lines = read_reply(reader)
                    print("<- %d %s" % (code, lines[0]))

            sock.sendall(b"QUIT\r\n")
            code, lines = read_reply(reader)
            print(f"> QUIT\n<- {code} {lines[0]}")
            sock.close()

            for recipient in ("bob@example.net", "carol@example.net"):
                files = server.mailbox.paths_for(recipient)
                print(f"{recipient}: {len(files)} file(s), "
                      f"{files[0].name if files else '-'}")
                if files:
                    stored = files[0].read_text(encoding="utf-8")
                    print("  first line:", stored.splitlines()[0])
    finally:
        shutil.rmtree(spool, ignore_errors=True)


if __name__ == "__main__":
    main()
