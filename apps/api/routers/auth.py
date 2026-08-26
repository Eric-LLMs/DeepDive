"""User authentication routes: login / session / profile / email verification.

Opaque ``dd_`` API tokens for the desktop workbench; stateless ``cc_`` console sessions for
the web; self-service account lifecycle with one-time email tokens.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from api.account_email import (
    _consume_verification,
    _html_page,
    _issue_verification,
    _send_account_email,
)
from api.auth import USER_ROLE, AuthUser, require_user, sign_console_token
from api.routers._shared import (
    _auth_rate_limit,
    _login_expiry,
    _masked_model,
    _pick_credential,
    _usage_report,
    _verify_user_login,
)
from api.schemas import (
    ForgotPasswordRequest,
    ProfileUpdateRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UserLoginRequest,
)
from core.infrastructure.billing import get_balance
from core.infrastructure.db import (
    AccessTokenModel,
    LLMModelModel,
    LoginTokenModel,
    SessionLocal,
    UserModel,
    UserWalletModel,
)
from core.infrastructure.security import (
    generate_token,
    get_role,
    hash_password,
    role_to_dict,
    verify_password,
)
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select, update

router = APIRouter(tags=["auth"])
AVATAR_DIR = Path("data/avatars")


async def _login_token(
    session,
    *,
    user_id: UUID,
    name: str,
    credential_id: UUID | None,
    expires_at: datetime,
) -> str:
    """Return a login credential for (user, channel), reusing the existing row when possible.

    One row per (user, pinned channel): a re-login on the same channel rotates the secret
    and bumps the timestamps instead of inserting a new row, so ``login_tokens`` stops
    growing with every login (enforced by the partial unique indexes on the table).

    This always mints — a disabled key grant never blocks the login. ``_pick_credential``
    has already excluded banned keys, so the channel passed here is always one the user may
    use (or None); a banned key is simply not re-picked.

    When a channel is pinned, its ``access_tokens`` grant row is created **lazily** the
    first time: the per-user key-grant matrix is what the Tokens page toggles, and the ban
    must be sticky (a re-login must not revive it, so an existing disabled grant is left
    alone).
    """
    if credential_id is not None:
        grant = (
            await session.execute(
                select(AccessTokenModel).where(
                    AccessTokenModel.user_id == user_id,
                    AccessTokenModel.credential_id == credential_id,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            session.add(AccessTokenModel(user_id=user_id, credential_id=credential_id))
    existing = (
        await session.execute(
            select(LoginTokenModel)
            .where(
                LoginTokenModel.user_id == user_id,
                (
                    LoginTokenModel.credential_id.is_(None)
                    if credential_id is None
                    else LoginTokenModel.credential_id == credential_id
                ),
            )
            .order_by(LoginTokenModel.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raw, token_hash = generate_token()
        existing.token_hash = token_hash
        existing.is_active = True
        existing.last_used_at = datetime.now(UTC)
        existing.expires_at = expires_at
        await session.commit()
        return raw
    raw, token_hash = generate_token()
    session.add(
        LoginTokenModel(
            user_id=user_id,
            name=name,
            token_hash=token_hash,
            role="user",
            role_id=None,
            credential_id=credential_id,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return raw


@router.post("/auth/login")
async def user_login(body: UserLoginRequest, request: Request) -> dict:
    """Verify a user's credentials and mint an opaque API token (``dd_``).

    The token is one row in ``login_tokens`` per (user, channel); the next login for the
    same channel rotates its secret, so it is meant for clients that log in fresh (the
    desktop) or mint their own tokens. The web console uses ``/auth/session-login``
    instead, whose stateless session survives re-logins.
    """
    await _auth_rate_limit(request, getattr(request.app.state, "redis", None), "login")
    async with SessionLocal() as session:
        row = await _verify_user_login(session, body.username, body.password)
        role = await get_role(session, row.role_id)
        credential_id = await _pick_credential(session, row.role_id, row.id)
        token = await _login_token(
            session,
            user_id=row.id,
            name=row.username,
            credential_id=credential_id,
            expires_at=_login_expiry(),
        )
    return {
        "access_token": token,
        "username": row.username,
        "display_name": row.display_name,
        "role_id": row.role_id,
        "role_name": role.role_name if role else row.role_id,
    }


@router.post("/auth/session-login")
async def user_session_login(body: UserLoginRequest, request: Request) -> dict:
    """Verify credentials and mint a stateless web-console session token (``cc_``).

    Console sessions are signed, self-contained strings held in the browser's
    localStorage — nothing is written to login_tokens, so a desktop re-login (which
    rotates the ``dd_`` API token) cannot invalidate the web console's session.
    """
    await _auth_rate_limit(request, getattr(request.app.state, "redis", None), "login")
    async with SessionLocal() as session:
        row = await _verify_user_login(session, body.username, body.password)
        role = await get_role(session, row.role_id)
    token = await sign_console_token(body.username, USER_ROLE, _login_expiry())
    return {
        "access_token": token,
        "username": row.username,
        "display_name": row.display_name,
        "role_id": row.role_id,
        "role_name": role.role_name if role else row.role_id,
    }


@router.post("/auth/session")
async def auth_session(user: AuthUser = Depends(require_user)) -> dict:
    """Exchange the current API token for a stateless web-console session token.

    The desktop hands the web console its API token via ``?sso=``; converting it here
    keeps the browser session alive even if that API token is later rotated by a re-login.
    """
    token = await sign_console_token(user.username, USER_ROLE, _login_expiry())
    return {
        "access_token": token,
        "username": user.username,
        "display_name": user.display_name,
        "role_id": user.role.role_id,
        "role_name": user.role.role_name,
    }


@router.get("/auth/me")
async def auth_me(user: AuthUser = Depends(require_user)) -> dict:
    """Return the authenticated user's profile + role quota (desktop account panel)."""
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user.user_id))
        ).scalar_one_or_none()
        role = await get_role(session, user.role.role_id)
    return {
        "user_id": str(user.user_id),
        "username": user.username,
        "display_name": user.display_name,
        "email": row.email if row else None,
        "phone": row.phone if row else None,
        "avatar": row.avatar if row else None,
        "email_verified": row.email_verified if row else False,
        "role_id": user.role.role_id,
        "role_name": role.role_name if role else user.role.role_id,
        "quota": role_to_dict(user.role),
    }


@router.get("/auth/usage")
async def auth_usage(
    start: str | None = None,   # ISO date/datetime — logs from this moment (inclusive)
    end: str | None = None,     # ISO date/datetime — logs until this moment (inclusive)
    model: str | None = None,   # fuzzy match on model_name
    limit: int = 20,            # logs per page
    offset: int = 0,
    user: AuthUser = Depends(require_user),
) -> dict:
    """Return the signed-in user's own balance, daily counters, usage logs, and ledger.

    Mirrors the admin ``/admin/users/{id}/usage`` shape, but the user_id is always the
    caller's own — a user can never query anyone else's data.
    """
    async with SessionLocal() as session:
        balance = await get_balance(session, user.user_id)
        wallet = await session.get(UserWalletModel, user.user_id)
        report = await _usage_report(session, user.user_id, start, end, model, limit, offset)
    report["balance"] = float(balance)
    report["currency"] = wallet.currency if wallet else "USD"
    return report


@router.get("/auth/models")
async def auth_models(user: AuthUser = Depends(require_user)) -> dict:
    """Return the model catalog so a user can inspect any model they used in the UI.

    Read-only, no sensitive data — reuses the same masked shape as the admin
    ``/admin/models`` endpoint, so regular users see only display fields + prices.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(LLMModelModel).order_by(LLMModelModel.created_at))
        ).scalars().all()
    return {"models": [_masked_model(m) for m in rows]}


@router.post("/auth/register")
async def register(body: RegisterRequest, request: Request) -> dict:
    """Self-service signup: create a regular account gated on email verification."""
    await _auth_rate_limit(request, getattr(request.app.state, "redis", None), "register")
    username = body.username.strip()
    email = body.email.strip().lower()
    if not username or not email or not body.password:
        raise HTTPException(status_code=400, detail="用户名、邮箱和密码不能为空")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    async with SessionLocal() as session:
        dup = (
            await session.execute(
                select(UserModel).where(
                    (UserModel.username == username) | (UserModel.email == email)
                )
            )
        ).scalars().first()
        if dup is not None:
            raise HTTPException(status_code=409, detail="用户名或邮箱已被注册")
        row = UserModel(
            username=username,
            email=email,
            password_hash=hash_password(body.password),
            display_name=body.display_name or None,
            role_id="regular",
            is_active=True,
            email_verified=False,
        )
        session.add(row)
        await session.flush()
        raw = await _issue_verification(session, row.id, "verify", 1440)
    base = str(request.base_url).rstrip("/")
    dev = await _send_account_email("DeepDive 邮箱验证", email, base, raw, "auth/verify-email")
    return {"status": "ok", "message": "注册成功,请查收邮件完成邮箱验证。", **dev}


@router.get("/auth/verify-email")
async def verify_email(token: str) -> HTMLResponse:
    """Email-link landing: mark the account verified (one-time token)."""
    async with SessionLocal() as session:
        vrow = await _consume_verification(session, token, "verify")
        if vrow is None:
            return _html_page(
                "验证失败",
                "<h1>链接无效或已过期</h1><p>请重新注册,或在个人资料里重新发送验证邮件。</p>",
            )
        user = (
            await session.execute(select(UserModel).where(UserModel.id == vrow.user_id))
        ).scalar_one_or_none()
        if user is None:
            return _html_page("验证失败", "<h1>账号不存在</h1>")
        user.email_verified = True
        await session.commit()
    return _html_page("验证成功", "<h1>✓ 邮箱已验证</h1><p>现在可以返回客户端登录了。</p>")


@router.post("/auth/resend-verification")
async def resend_verification(body: ResendVerificationRequest, request: Request) -> dict:
    """Re-send the verification email (60 s Redis cooldown per address)."""
    await _auth_rate_limit(request, getattr(request.app.state, "redis", None), "recovery")
    email = body.email.strip().lower()
    key = f"verify:{email}"
    redis = getattr(request.app.state, "redis", None)
    if redis is not None and await redis.get(key):
        raise HTTPException(status_code=429, detail="发送过于频繁,请稍后再试(60 秒)。")
    async with SessionLocal() as session:
        user = (
            await session.execute(select(UserModel).where(UserModel.email == email))
        ).scalar_one_or_none()
        if user is None or user.email_verified:
            return {"status": "ok", "message": "如果该邮箱已注册且未验证,验证邮件将重新发送。"}
        raw = await _issue_verification(session, user.id, "verify", 1440)
    if redis is not None:
        await redis.setex(key, 60, "1")
    base = str(request.base_url).rstrip("/")
    dev = await _send_account_email("DeepDive 邮箱验证", email, base, raw, "auth/verify-email")
    return {"status": "ok", "message": "验证邮件已重新发送,请查收。", **dev}


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request) -> dict:
    """Email a one-time password-reset link (does not reveal whether the email exists)."""
    await _auth_rate_limit(request, getattr(request.app.state, "redis", None), "recovery")
    email = body.email.strip().lower()
    async with SessionLocal() as session:
        user = (
            await session.execute(select(UserModel).where(UserModel.email == email))
        ).scalar_one_or_none()
        if user is None:
            return {"status": "ok", "message": "如果该邮箱已注册,重置邮件将发送到您的邮箱。"}
        raw = await _issue_verification(session, user.id, "reset", 60)
    base = str(request.base_url).rstrip("/")
    dev = await _send_account_email("DeepDive 密码重置", email, base, raw, "auth/reset-password")
    return {"status": "ok", "message": "重置邮件已发送,请查收(1 小时内有效)。", **dev}


@router.get("/auth/reset-password")
async def reset_password_page(token: str) -> HTMLResponse:
    """Browser landing for the reset link: a small form that POSTs JSON to /auth/reset-password."""
    body = (
        "<h1>重置密码</h1><p>请输入新密码(至少 6 位)。</p>"
        '<form id="reset-form"><input type="hidden" id="reset-token" value="'
        + token
        + '">'
        '<input type="password" id="reset-password" placeholder="新密码" minlength="6" autocomplete="new-password" required>'
        '<input type="password" id="reset-password2" placeholder="确认新密码" required>'
        '<button type="submit">重置密码</button>'
        '<p class="err" id="reset-err"></p></form>'
        "<script>"
        "document.getElementById('reset-form').addEventListener('submit',async e=>{e.preventDefault();"
        "const p=document.getElementById('reset-password').value,p2=document.getElementById('reset-password2').value,"
        "err=document.getElementById('reset-err');"
        "if(p!==p2){err.textContent='两次输入的密码不一致';return;}"
        "const r=await fetch('/auth/reset-password',{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({token:document.getElementById('reset-token').value,password:p})});"
        "const d=await r.json().catch(()=>({}));"
        "if(r.ok){err.className='ok';err.textContent=d.message||'密码已重置,请用新密码登录。';}"
        "else{err.className='err';err.textContent=d.detail||'重置失败,请重试。';}});"
        "</script>"
    )
    return _html_page("重置密码", body)


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request) -> dict:
    """Apply a new password from a valid reset token and revoke the user's login tokens."""
    await _auth_rate_limit(request, getattr(request.app.state, "redis", None), "recovery")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    async with SessionLocal() as session:
        vrow = await _consume_verification(session, body.token, "reset")
        if vrow is None:
            raise HTTPException(status_code=400, detail="链接无效或已过期,请重新发起重置。")
        user = (
            await session.execute(select(UserModel).where(UserModel.id == vrow.user_id))
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=400, detail="账号不存在")
        user.password_hash = hash_password(body.password)
        # Old password stops working immediately: revoke every login token for this user.
        await session.execute(
            update(LoginTokenModel)
            .where(LoginTokenModel.user_id == user.id)
            .values(is_active=False)
        )
        await session.commit()
    return {"status": "ok", "message": "密码已重置,请用新密码登录。"}


@router.patch("/auth/me")
async def update_me(
    request: Request,
    body: ProfileUpdateRequest,
    user: AuthUser = Depends(require_user),
) -> dict:
    """Self-service profile edit: display name / username / email / phone / password."""
    if body.new_password and not body.current_password:
        raise HTTPException(status_code=400, detail="修改密码需要输入当前密码")
    email_changed = False
    new_email = None
    user_id = user.user_id
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user.user_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        if body.username is not None:
            username = body.username.strip()
            if not username:
                raise HTTPException(status_code=400, detail="用户名不能为空")
            dup = (
                await session.execute(
                    select(UserModel).where(
                        UserModel.username == username, UserModel.id != row.id
                    )
                )
            ).scalar_one_or_none()
            if dup is not None:
                raise HTTPException(status_code=409, detail="用户名已被使用")
            row.username = username
        if body.display_name is not None:
            row.display_name = body.display_name.strip() or None
        if body.phone is not None:
            row.phone = body.phone.strip() or None
        if body.email is not None:
            email = body.email.strip().lower()
            if not email:
                raise HTTPException(status_code=400, detail="邮箱不能为空")
            if email != row.email:
                dup = (
                    await session.execute(
                        select(UserModel).where(
                            UserModel.email == email, UserModel.id != row.id
                        )
                    )
                ).scalar_one_or_none()
                if dup is not None:
                    raise HTTPException(status_code=409, detail="该邮箱已被其他账号使用")
                row.email = email
                row.email_verified = False
                email_changed = True
                new_email = email
        if body.new_password:
            if not verify_password(body.current_password or "", row.password_hash):
                raise HTTPException(status_code=400, detail="当前密码不正确")
            if len(body.new_password) < 6:
                raise HTTPException(status_code=400, detail="新密码至少 6 位")
            row.password_hash = hash_password(body.new_password)
        await session.commit()
    if email_changed:
        async with SessionLocal() as session:
            raw = await _issue_verification(session, user_id, "verify", 1440)
        base = str(request.base_url).rstrip("/")
        dev = await _send_account_email("DeepDive 邮箱验证", new_email, base, raw, "auth/verify-email")
        return {"status": "ok", "message": "资料已更新,新邮箱需验证后才能再次登录。", **dev}
    return {"status": "ok", "message": "资料已更新。"}


@router.post("/auth/me/avatar")
async def upload_avatar(
    # B008: FastAPI requires the File() marker in the default — not a pre-executed call.
    file: UploadFile = File(...),  # noqa: B008
    user: AuthUser = Depends(require_user),
) -> dict:
    """Accept an avatar image (PNG/JPG/WEBP/GIF ≤ 2 MB), store it, and record its URL."""
    allowed = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
    ext = allowed.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(status_code=400, detail="仅支持 PNG / JPG / WEBP / GIF 图片")
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="头像图片不能超过 2MB")
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    fname = f"{user.user_id}.{ext}"
    (AVATAR_DIR / fname).write_bytes(data)
    avatar_url = f"/avatars/{fname}"
    async with SessionLocal() as session:
        row = (
            await session.execute(select(UserModel).where(UserModel.id == user.user_id))
        ).scalar_one_or_none()
        if row is not None:
            row.avatar = avatar_url
            await session.commit()
    return {"avatar": avatar_url}
