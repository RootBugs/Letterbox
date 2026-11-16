# smtp-server

A real SMTP receiving server (a mail drop) speaking RFC 5321 on raw
`socket` + threads: command loop, transaction state machine, dot-stuffing,
multi-recipient delivery, size limits, and one `.eml` file per recipient
mailbox with parsed headers.

## Why it's special

SMTP is deceptively simple — the hard parts are the *states*: which
commands are legal before `HELO`, what `RSET` destroys, why `DATA`
without `RCPT` is an error, and how a line starting with `.` quietly
means two different things depending on context. This server implements
all of it as an explicit state machine (`session.py`) that is
socket-agnostic: every command is a pure `line in → reply bytes out`
transition, which is why the tests can drive it end-to-end over real
sockets *and* assert exact codes for every illegal sequence.

## Architecture

```
smtp_server/
├── mailbox.py   .eml storage: one file per recipient, address
│                sanitization (no spool escapes), header parsing
│                (obs-fold unfolding, duplicate policy, body split)
├── session.py   state machine: HELO/EHLO, MAIL FROM, RCPT TO (repeat),
│                DATA + dot-stuffing, RSET, NOOP, QUIT, VRFY, HELP;
│                2xx/4xx/5xx codes; SIZE cap → 552
└── server.py    threaded TCP server: banner, line reader with hard
                 limits, data-mode draining, graceful shutdown
```

Protocol highlights:

- **States.** `MAIL`/`RCPT`/`DATA` before `HELO` → 503; `RCPT` before
  `MAIL` → 503; duplicate `MAIL` → 503; `DATA` without `RCPT` → 503.
  `RSET` (and a fresh `HELO`) abandon the transaction; a new `MAIL FROM`
  then succeeds.
- **Replies.** 220 banner, 250 OK, 354 start-data, 221 bye, 500 unknown
  command, 501 bad syntax/missing argument, 503 bad sequence, 550 no such
  user (VRFY/RCPT with a user list), 552 oversize (nothing stored, session
  stays usable).
- **Dot-stuffing.** A leading `.` is removed from payload lines; a lone
  `.` line terminates the message and is never stored.
- **Storage.** `spool/<recipient>/<timestamp>-<id>.eml`; the stored bytes
  are *exactly* the dot-unstuffed payload, and `parse_headers()` extracts
  From/To/Subject/Date (with folded headers unfolded).
- **Safety.** Recipient addresses are sanitized (`..`, `/`, `\` cannot
  escape the spool), command lines are capped at 4096 bytes (500 + close),
  and `EHLO` advertises the real `SIZE` limit.

## Runnable example

`demo.py` runs a complete session and prints the transcript:

```python
import socket
from smtp_server import SMTPServer

with SMTPServer(spool_dir="./spool", hostname="mail.example.org") as server:
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    reader = sock.makefile("rb")

    # <- 220 mail.example.org ESMTP ready
    for command in ["HELO demo.client",
                    "MAIL FROM:<alice@example.org>",
                    "RCPT TO:<bob@example.net>",
                    "DATA"]:
        sock.sendall(command.encode() + b"\r\n")
        ...  # read the reply line(s)

    sock.sendall(b"From: alice@example.org\r\n\r\nHello!\r\n.\r\n")
    # <- 250 OK: queued as ...
```

Actual output of `python demo.py` (port and file names vary per run):

```
smtp://127.0.0.1:54673  spool=C:\...\smtp-demo-4xmg8r49
<- 220 mail.example.org ESMTP ready
> HELO demo.client
<- 250 mail.example.org
> MAIL FROM:<alice@example.org>
<- 250 OK
> RCPT TO:<bob@example.net>
<- 250 OK
> RCPT TO:<carol@example.net>
<- 250 OK
> DATA
<- 354 End data with <CR><LF>.<CR><LF>
<- 250 OK: queued as 1794638918-a44cca017b14
> QUIT
<- 221 Bye
bob@example.net: 1 file(s), 1794638918-a44cca017b14.eml
  first line: From: alice@example.org
carol@example.net: 1 file(s), 1794638918-9493678911b3.eml
  first line: From: alice@example.org
```

## Tests

```
python -m unittest discover -s tests -v
```

18 tests across two modules, all against a live server on an ephemeral
port (threads, clean shutdown, no sleeps):

- full session transcript → stored `.eml` byte-exact, dot-stuffing
  verified (`..line` stored as `.line`, terminator never stored)
- both recipients receive identical files; duplicate RCPT collapses
- every illegal sequence returns the right code (503/501/500), including
  `DATA` without `RCPT`
- `RSET` clears the transaction; two concurrent clients keep independent
  transactions
- size limit → `552` with nothing stored, session recovers afterwards
- `VRFY` 250/550 against a known-user set; `EHLO` SIZE matches the cap
- mailbox unit tests: header parsing (folding, duplicates, no-body),
  oversize rejection, recipient sanitization never escapes the spool

## License

MIT — see [LICENSE](LICENSE).
