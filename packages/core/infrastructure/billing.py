"""Wallet billing: model pricing, atomic topup/deduction, and the append-only ledger.

Prices live on ``llm_models`` (per 1k tokens); ``credential_models`` may override them per
credential later. The wallet balance is the authoritative PAYG source: topups credit it and
usage debits it, each writing a ``wallet_transactions`` row with a ``balance_after`` snapshot.

All functions here are transaction-scoped helpers: they mutate the session but never commit,
so a caller can bundle them with other writes (e.g. a usage-log row) in one transaction.
"""
from __future__ import annotations

import secrets
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.infrastructure.db import (
    LLMModelModel,
    UserWalletModel,
    WalletTransactionModel,
)

_SIX = Decimal("0.000001")


def _q(value) -> Decimal:
    """Quantize a money value to 6 decimal places (USD micro-cents)."""
    return Decimal(value).quantize(_SIX, rounding=ROUND_HALF_UP)


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    prompt_price_per_1k: Decimal | float | str = 0,
    completion_price_per_1k: Decimal | float | str = 0,
) -> Decimal:
    """Return the USD cost for a usage, given per-1k-token prices."""
    prompt = Decimal(prompt_price_per_1k or 0)
    completion = Decimal(completion_price_per_1k or 0)
    cost = (
        Decimal(prompt_tokens or 0) * prompt / Decimal(1000)
        + Decimal(completion_tokens or 0) * completion / Decimal(1000)
    )
    return _q(cost)


async def get_model_prices(session, model_name: str) -> tuple[Decimal, Decimal]:
    """Return ``(prompt_price, completion_price)`` for a catalog model (0/0 if unknown).

    Billing is keyed on the catalog display ``name`` — the business model name the usage
    log records. A provider-model-id fallback covers legacy raw model strings; when
    several catalog entries share one provider id, the active one created earliest wins
    so the lookup never errors on ambiguous rows.
    """
    model = (
        await session.execute(select(LLMModelModel).where(LLMModelModel.name == model_name))
    ).scalar_one_or_none()
    if model is None:
        model = (
            await session.execute(
                select(LLMModelModel)
                .where(LLMModelModel.provider_model_name == model_name)
                .order_by(LLMModelModel.is_active.desc(), LLMModelModel.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    if model is None:
        return Decimal("0"), Decimal("0")
    return model.prompt_price_per_1k, model.completion_price_per_1k


async def ensure_wallet(session, user_id: UUID) -> None:
    """Create the wallet row if missing (no-op when it already exists)."""
    await session.execute(
        pg_insert(UserWalletModel)
        .values(user_id=user_id, balance=Decimal("0"), currency="USD")
        .on_conflict_do_nothing(index_elements=["user_id"])
    )


async def get_balance(session, user_id: UUID) -> Decimal:
    """Return the current wallet balance (0 if no row yet)."""
    wallet = await session.get(UserWalletModel, user_id)
    return wallet.balance if wallet is not None else Decimal("0")


async def topup(
    session,
    user_id: UUID,
    amount: Decimal | float | str,
    description: str = "",
    idempotency_key: str | None = None,
) -> Decimal:
    """Credit the wallet and append a ``topup`` ledger entry; returns the new balance."""
    amount = _q(amount)
    await ensure_wallet(session, user_id)
    stmt = (
        update(UserWalletModel)
        .where(UserWalletModel.user_id == user_id)
        .values(balance=UserWalletModel.balance + amount, updated_at=func.now())
        .returning(UserWalletModel.balance)
    )
    balance_after = _q((await session.execute(stmt)).scalar_one())
    session.add(
        WalletTransactionModel(
            user_id=user_id,
            type="topup",
            amount=amount,
            balance_after=balance_after,
            description=description or "Wallet top-up",
            idempotency_key=idempotency_key or ("topup_" + secrets.token_hex(8)),
        )
    )
    return balance_after


async def deduct(
    session,
    user_id: UUID,
    amount: Decimal | float | str,
    description: str = "",
    meta: dict | None = None,
) -> Decimal | None:
    """Debit the wallet if funds suffice; returns the new balance or None (insufficient).

    The check and decrement happen in one atomic ``UPDATE ... WHERE balance >= cost`` so
    concurrent requests cannot overspend.
    """
    amount = _q(amount)
    await ensure_wallet(session, user_id)
    stmt = (
        update(UserWalletModel)
        .where(UserWalletModel.user_id == user_id, UserWalletModel.balance >= amount)
        .values(balance=UserWalletModel.balance - amount, updated_at=func.now())
        .returning(UserWalletModel.balance)
    )
    balance_after = (await session.execute(stmt)).scalar_one_or_none()
    if balance_after is None:
        return None
    balance_after = _q(balance_after)
    session.add(
        WalletTransactionModel(
            user_id=user_id,
            type="llm_consume",
            amount=-amount,
            balance_after=balance_after,
            description=description or "LLM usage",
            meta=meta or {},
        )
    )
    return balance_after


async def list_transactions(session, user_id: UUID, limit: int = 50):
    """Return the most recent ledger entries for a user (newest first)."""
    return (
        await session.execute(
            select(WalletTransactionModel)
            .where(WalletTransactionModel.user_id == user_id)
            .order_by(WalletTransactionModel.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
