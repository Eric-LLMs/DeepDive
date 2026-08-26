"""Account email + verification helpers shared by auth and config routes.

Mints / consumes one-time verification tokens, builds the HTML landing pages, and sends
account email via SMTP (with a dev fallback that returns the raw link when SMTP is absent).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from core.infrastructure.db import VerificationTokenModel
from core.infrastructure.mailer import MailNotConfigured, send_email
from core.infrastructure.security import generate_token, hash_token
from fastapi.responses import HTMLResponse
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _html_page(title: str, body: str) -> HTMLResponse:
    """Self-contained Chinese HTML page served by verify/reset email links."""
    html = (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>"
        "body{font-family:-apple-system,'Segoe UI',Roboto,'Microsoft YaHei',sans-serif;"
        "background:#f5f7fa;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}"
        ".card{background:#fff;border:1px solid #e3e6ec;border-radius:12px;"
        "box-shadow:0 8px 30px rgba(0,0,0,.06);padding:36px 40px;max-width:420px;width:100%;text-align:center}"
        "h1{font-size:20px;margin:0 0 14px}p{color:#555;font-size:14px;line-height:1.7;margin:0 0 16px;word-break:break-all}"
        "input{width:100%;box-sizing:border-box;padding:10px 12px;margin:4px 0 10px;"
        "border:1px solid #d5dae2;border-radius:8px;font-size:14px}"
        "button{width:100%;padding:10px 12px;border:0;border-radius:8px;background:#2f6fed;color:#fff;font-size:14px;cursor:pointer}"
        "button:hover{background:#2457c8}.err{color:#c0392b;font-size:13px;margin:8px 0 0}.ok{color:#1e8449;font-size:13px}"
        "</style></head><body><div class='card'>" + body + "</div></body></html>"
    )
    return HTMLResponse(content=html)


async def _smtp_config() -> dict:
    from api.routers.config import _load_config  # lazy: avoid a config cycle
    return (await _load_config()).get("smtp") or {}


async def _issue_verification(session, user_id: UUID, kind: str, ttl_minutes: int) -> str:
    """Mint a one-time verification/reset token row; returns the raw token (hash stored)."""
    raw, token_hash_ = generate_token()
    session.add(
        VerificationTokenModel(
            user_id=user_id,
            kind=kind,
            token_hash=token_hash_,
            expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
        )
    )
    await session.commit()
    return raw


async def _consume_verification(session, raw: str, kind: str) -> VerificationTokenModel | None:
    """Validate (kind, unused, unexpired) a one-time token and mark it used; None otherwise."""
    row = (
        await session.execute(
            select(VerificationTokenModel).where(
                VerificationTokenModel.token_hash == hash_token(raw)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.kind != kind or row.used_at is not None:
        return None
    if row.expires_at is not None and row.expires_at < datetime.now(UTC):
        return None
    row.used_at = datetime.now(UTC)
    return row


async def _send_account_email(subject: str, to: str, base: str, raw_token: str, path: str) -> dict:
    """Send an account email; on failure (or no SMTP) return the raw link as a dev fallback."""
    smtp = await _smtp_config()
    link = f"{base.rstrip('/')}/{path}?token={raw_token}"
    html = (
        "<p>您好,</p><p>请点击下方链接完成操作(链接 24 小时内有效,只能使用一次):</p>"
        f'<p><a href="{link}" style="color:#2f6fed">{link}</a></p>'
        "<p>如果不是您本人操作,请忽略此邮件。</p>"
    )
    try:
        await send_email(smtp, to, subject, html)
        return {}
    except MailNotConfigured:
        logger.warning("SMTP 未配置,邮件链接(仅开发用): %s", link)
        return {"debug_verify_url": link}
    except Exception as err:  # noqa: BLE001 — surface the failure instead of a 500
        logger.error("发送邮件失败(%s): %s", to, err)
        return {"debug_verify_url": link, "email_error": str(err)}
