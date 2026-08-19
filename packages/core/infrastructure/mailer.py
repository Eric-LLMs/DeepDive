"""Minimal SMTP mailer for account emails (verification / password reset / test).

Uses the stdlib ``smtplib`` so no third-party mail dependency is needed. ``send_email`` is
async but delegates to a thread (``asyncio.to_thread``) because smtplib is blocking.

The ``smtp`` dict comes from ``app_settings['config']['smtp']`` and holds:
``host, port, user, password, from_email, use_tls, enabled``.
An empty ``host`` (or ``enabled`` False) means email is not configured — callers then fall
back to returning the raw link to the client (dev mode) instead of sending it.
"""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage


class MailNotConfigured(Exception):
    """Raised when no usable SMTP server is configured."""


def _smtp_is_configured(smtp: dict | None) -> bool:
    if not smtp:
        return False
    if not smtp.get("enabled", True):
        return False
    return bool((smtp.get("host") or "").strip())


def _send_blocking(smtp: dict, to: str, subject: str, html: str) -> None:
    """Blocking smtplib send; run inside a thread. Raises on failure."""
    host = (smtp.get("host") or "").strip()
    if not host:
        raise MailNotConfigured("SMTP host not configured")
    port = int(smtp.get("port") or (465 if smtp.get("use_ssl") else 587))
    user = (smtp.get("user") or "").strip()
    password = smtp.get("password") or ""
    from_email = (smtp.get("from_email") or user or "no-reply@localhost").strip()
    use_tls = bool(smtp.get("use_tls", True))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to
    msg.set_content(html)                      # fallback plain text
    msg.add_alternative(html, subtype="html")  # richer HTML body

    if smtp.get("use_ssl"):
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            if user:
                server.login(user, password)
            server.send_message(msg)
        return

    with smtplib.SMTP(host, port, timeout=15) as server:
        if use_tls:
            server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)


async def send_email(smtp: dict, to: str, subject: str, html: str) -> None:
    """Send one email; raises ``MailNotConfigured`` if SMTP is unset, else the smtplib error."""
    if not _smtp_is_configured(smtp):
        raise MailNotConfigured("SMTP not configured")
    await asyncio.to_thread(_send_blocking, smtp, to, subject, html)
