"""Raw mail messages shared by the mail parse and agent-mail-turn suites (file-based
samples live in tests/fixtures/mail/).
"""
from __future__ import annotations

_NUL_MAIL = (
    b"From: \"Ev\x00e\" <eve@example.org>\r\n"
    b"To: ada@marshellis.com\r\n"
    b"Subject: hi\x00there\r\n"
    b"Message-ID: <a\x00b@example.org>\r\n"
    b"X-Odd: v\x00al\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
    b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nbo\x00dy\r\n"
    b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>ht\x00ml</p>\r\n"
    b"--B\r\nContent-Type: application/pdf\r\n"
    b"Content-Disposition: attachment; filename=\"a\x00.pdf\"\r\n\r\nPDF\r\n"
    b"--B--\r\n"
)
