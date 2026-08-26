"""LLM provider / web-search / SMTP configuration routes (admin-only).

Config is stored as a JSON blob in app_settings; the flat legacy keys and the generic
``tools`` namespace are mirrored back onto ``settings`` for the live client.
"""
from __future__ import annotations

import json

from api.account_email import _smtp_config
from api.auth import AuthAdmin, require_admin
from api.deps import llm
from api.routers._shared import _fallback_model
from api.schemas import ProbeModelsRequest, ProvidersUpdateRequest, TestEmailRequest
from core.config import settings
from core.infrastructure.db import SessionLocal
from core.infrastructure.mailer import MailNotConfigured, send_email
from core.infrastructure.security import get_setting, list_roles, role_to_dict, set_setting
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(tags=["config"])


async def _load_config() -> dict:
    async with SessionLocal() as session:
        return await get_setting(session, "config") or {}


async def _save_config(data: dict) -> None:
    async with SessionLocal() as session:
        await set_setting(session, "config", data)


def _active_provider_from_cfg(cfg: dict) -> dict | None:
    providers = cfg.get("llm_providers", [])
    if not providers:
        return None
    active_id = cfg.get("llm_active_provider") or providers[0].get("id")
    return next((p for p in providers if p.get("id") == active_id), None)


def _merge_tools_into_legacy(cfg: dict) -> None:
    """Mirror the generic ``tools`` namespace onto the legacy flat ``web_search_*`` / ``smtp``
    keys so existing read paths (settings mirror, mailer, chat routing) keep working.

    Idempotent: safe to call on every load / save.
    """
    tools = cfg.get("tools") or {}
    ws = tools.get("web_search") or {}
    if ws.get("provider"):
        cfg["web_search_provider"] = ws["provider"]
    if ws.get("api_key"):
        cfg["web_search_api_key"] = ws["api_key"]
    if "engine_id" in ws:
        cfg["web_search_engine_id"] = ws["engine_id"] or ""
    if tools.get("smtp"):
        smtp = dict(tools["smtp"])
        smtp.setdefault("use_tls", True)
        smtp.setdefault("use_ssl", False)
        smtp.setdefault("enabled", True)
        cfg["smtp"] = smtp


def _tools_view(cfg: dict) -> dict:
    """Build the ``tools`` view for /config GET, backfilling from the legacy keys so a
    pre-tools config still shows its values in the Tools config page."""
    tools = dict(cfg.get("tools") or {})
    ws = dict(tools.get("web_search") or {})
    ws.setdefault("provider", cfg.get("web_search_provider", ""))
    if cfg.get("web_search_api_key"):
        ws.setdefault("api_key", cfg["web_search_api_key"])
    if cfg.get("web_search_engine_id") is not None:
        ws.setdefault("engine_id", cfg["web_search_engine_id"])
    tools["web_search"] = ws
    if cfg.get("smtp"):
        tools.setdefault("smtp", cfg["smtp"])
    return tools


def _apply_llm_settings(cfg: dict) -> None:
    """Mirror the active provider connection (base_url/api_key) + web-search onto settings.

    The model is intentionally NOT taken from here: it is resolved from the Model Catalog at
    chat time (see ``_fallback_model``), so the legacy config never carries a model id.
    """
    _merge_tools_into_legacy(cfg)
    # Keep the generic tools namespace available at runtime: any code can read
    # get_tool_config("<tool_id>").get("<param>") without hitting the DB per call.
    settings.tool_configs = _tools_view(cfg)
    active = _active_provider_from_cfg(cfg)
    base_url = cfg.get("llm_base_url") or (active or {}).get("base_url", "")
    api_key = cfg.get("llm_api_key") or (active or {}).get("api_key", "")
    if base_url:
        settings.llm_base_url = base_url
    if api_key:
        settings.llm_api_key = api_key
    if cfg.get("web_search_provider"):
        settings.web_search_provider = cfg["web_search_provider"]
    if cfg.get("web_search_api_key"):
        settings.web_search_api_key = cfg["web_search_api_key"]
    if "web_search_engine_id" in cfg:
        settings.web_search_engine_id = cfg["web_search_engine_id"] or ""
    llm.configure(settings.llm_api_key, settings.llm_base_url, settings.llm_model)


def _default_config() -> dict:
    """Starter provider card seeded on first boot.

    No model is stored here — the chat model always comes from the Model Catalog
    (``_fallback_model``), so the two sources can never drift.
    """
    return {
        "llm_providers": [{"id": "default", "name": "Default", "base_url": "", "api_key": ""}],
        "llm_active_provider": "default",
    }


async def _bootstrap_config(session) -> None:
    """Seed app_settings['config'] on first boot (legacy JSON, else a default provider), then apply."""
    if await get_setting(session, "config") is None:
        legacy: dict = {}
        if settings.config_path.exists():
            try:
                legacy = json.loads(settings.config_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                legacy = {}
        await set_setting(session, "config", legacy or _default_config())
    cfg = await get_setting(session, "config") or {}
    _apply_llm_settings(cfg)


def _legacy_provider() -> list[dict]:
    """Synthesize a single provider card from the pre-multi-provider flat fields."""
    if not settings.llm_base_url and not settings.llm_model:
        return []
    return [
        {
            "id": "default",
            "name": "Default",
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "models": [settings.llm_model] if settings.llm_model else [],
            "model": settings.llm_model,
        }
    ]


async def _stored_providers() -> list[dict]:
    providers = (await _load_config()).get("llm_providers", [])
    return providers or _legacy_provider()


def _masked_provider(p: dict) -> dict:
    return {
        "id": p.get("id", ""),
        "name": p.get("name", ""),
        "base_url": p.get("base_url", ""),
        "api_key_set": bool(p.get("api_key")),
    }


def _masked_smtp(smtp: dict) -> dict:
    return {
        "host": smtp.get("host", ""),
        "port": smtp.get("port", 587),
        "user": smtp.get("user", ""),
        "password_set": bool(smtp.get("password")),
        "from_email": smtp.get("from_email", ""),
        "use_tls": smtp.get("use_tls", True),
        "use_ssl": smtp.get("use_ssl", False),
        "enabled": smtp.get("enabled", True),
    }


@router.get("/config")
async def get_config(_: AuthAdmin = Depends(require_admin)) -> dict:
    """Return the provider-card list (keys masked), active selection, and role list."""
    providers = await _stored_providers()
    cfg = await _load_config()
    active = cfg.get("llm_active_provider") or (providers[0]["id"] if providers else "")
    async with SessionLocal() as session:
        roles = await list_roles(session)
        fallback_model = await _fallback_model(session, "anonymous")
    return {
        "providers": [_masked_provider(p) for p in providers],
        "active_provider": active,
        "web_search_provider": settings.web_search_provider,
        "web_search_api_key_set": bool(settings.web_search_api_key),
        "web_search_engine_id": settings.web_search_engine_id,
        "smtp": _masked_smtp(cfg.get("smtp") or {}),
        "tools": _tools_view(cfg),
        "roles": [role_to_dict(r) for r in roles],
        "fallback_model": fallback_model,
    }


@router.post("/config")
async def update_config(body: ProvidersUpdateRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Persist provider cards (only when supplied) + web-search settings.

    The provider list is written only when ``body.providers`` is non-empty, so a
    web-search-only save from the Chat Test tab cannot wipe the stored cards. A blank
    ``api_key`` on a card means "keep the previously stored key" for that id.
    """
    cfg = await _load_config()
    previous = {p["id"]: p for p in cfg.get("llm_providers", [])}

    providers: list[dict] = []
    active_id = cfg.get("llm_active_provider") or ""
    if body.providers:
        for p in body.providers:
            data = p.model_dump()
            if not data.get("api_key") and previous.get(data["id"]):
                data["api_key"] = previous[data["id"]].get("api_key", "")
            data.pop("models", None)   # model id is resolved from the Catalog at chat time
            data.pop("model", None)
            providers.append(data)
        active_id = body.active_provider or (providers[0]["id"] if providers else "")
        active = next((p for p in providers if p["id"] == active_id), None)

        cfg["llm_providers"] = providers
        cfg["llm_active_provider"] = active_id
        # Mirror the active card's connection to the flat settings keys so the live client
        # picks them up without a restart. The model is deliberately not mirrored.
        if active:
            cfg["llm_base_url"] = active["base_url"]
            cfg["llm_api_key"] = active["api_key"]

    if body.web_search_provider:
        cfg["web_search_provider"] = body.web_search_provider
    if body.web_search_api_key:
        cfg["web_search_api_key"] = body.web_search_api_key
    # engine id is not a secret: a provided value (even empty) overwrites the stored one
    if body.web_search_engine_id is not None:
        cfg["web_search_engine_id"] = body.web_search_engine_id
    if body.web_search_provider:
        settings.web_search_provider = body.web_search_provider
    if body.web_search_api_key:
        settings.web_search_api_key = body.web_search_api_key
    if body.web_search_engine_id is not None:
        settings.web_search_engine_id = body.web_search_engine_id

    if body.smtp is not None:
        cur = cfg.get("smtp") or {}
        s = body.smtp.model_dump()
        if not s.get("password"):   # empty password = keep the stored one
            s["password"] = cur.get("password", "")
        cfg["smtp"] = s

    # Generic tools namespace: tools.<tool_id>.<param>. A blank secret keeps the stored value;
    # results are mirrored onto the legacy web_search_* / smtp keys below.
    if body.tools:
        stored = dict(cfg.get("tools") or {})
        for tool_id, params in body.tools.items():
            if not isinstance(params, dict):
                continue
            # The UI submits the tool's full intended state, so the stored dict is
            # REPLACED per tool (keys absent from the submission are dropped = deletion),
            # except a blank secret, which keeps the previously stored value. On first
            # migration the legacy flat keys seed the previous state so nothing is lost.
            prev = stored.get(tool_id)
            if not prev:
                if tool_id == "smtp":
                    prev = cfg.get("smtp") or {}
                elif tool_id == "web_search":
                    prev = {"provider": cfg.get("web_search_provider", "")}
                    if cfg.get("web_search_api_key"):
                        prev["api_key"] = cfg["web_search_api_key"]
                    if cfg.get("web_search_engine_id") is not None:
                        prev["engine_id"] = cfg["web_search_engine_id"]
            prev = dict(prev or {})
            merged = {}
            for k, v in params.items():
                if k in ("password", "api_key", "secret") and v == "":
                    if k in prev:
                        merged[k] = prev[k]
                    continue
                merged[k] = v
            stored[tool_id] = merged
        cfg["tools"] = stored
        _merge_tools_into_legacy(cfg)

    await _save_config(cfg)
    _apply_llm_settings(cfg)

    return {
        "status": "ok",
        "providers": [_masked_provider(p) for p in providers],
        "active_provider": active_id,
        "smtp": _masked_smtp(cfg.get("smtp") or {}),
    }


@router.post("/config/test-email")
async def test_email(body: TestEmailRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """Send a probe email through the configured SMTP (for the admin Settings card)."""
    smtp = await _smtp_config()
    try:
        await send_email(
            smtp,
            body.to_email.strip(),
            "DeepDive 测试邮件",
            "<p>这是一封来自 DeepDive 的测试邮件,SMTP 配置正常。</p>",
        )
        return {"status": "ok", "message": "测试邮件已发送。"}
    except MailNotConfigured:
        raise HTTPException(status_code=400, detail="SMTP 未配置:请先在 Settings 里填写 SMTP 信息。")
    except Exception as err:  # noqa: BLE001 — surface the smtplib error to the admin
        raise HTTPException(status_code=400, detail=f"发送失败:{err}")


@router.post("/config/probe-models")
async def probe_models(body: ProbeModelsRequest, _: AuthAdmin = Depends(require_admin)) -> dict:
    """List model ids from an OpenAI-compatible endpoint (for the settings UI's connectivity test).

    A blank ``api_key`` falls back to the stored key of the provider whose ``base_url``
    matches, so the Live Chat card can test a configured (masked) key without retyping it.
    """
    from openai import AsyncOpenAI

    api_key = body.api_key
    if not api_key:
        cfg = await _load_config()
        want = (body.base_url or "").rstrip("/")
        for p in cfg.get("llm_providers", []):
            if p.get("base_url") and p["base_url"].rstrip("/") == want:
                api_key = p.get("api_key", "")
                break

    client = AsyncOpenAI(
        base_url=body.base_url or None,
        api_key=api_key or "sk-placeholder",
        timeout=15.0,    # fail a connectivity probe fast instead of the SDK's 10-minute read timeout
        max_retries=0,   # the SDK retries timeouts 2x by default (3 x 15s = 45s); one attempt is enough
    )
    try:
        models = await client.models.list()
    except Exception as err:  # noqa: BLE001 - surface the provider's failure verbatim
        raise HTTPException(status_code=400, detail=str(err))
    ids = [m.id for m in models.data]
    return {"models": ids}
