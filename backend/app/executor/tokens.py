"""HMAC-signed, short-lived, single-use approval tokens.

The backend issues a token when a human approves an incident. It binds the approval id, the
incident id and the sha256 of the exact action, and expires quickly. The executor verifies the
signature, the expiry and the action hash, then records the approval id in its *own* store, so
a token can be used once no matter what the backend does afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import registry
from sqlmodel import Field, Session, SQLModel


class TokenError(ValueError):
    """The approval token is missing, malformed, forged, expired, mismatched or reused."""


class ApprovalClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str
    incident_id: str
    action_sha256: str
    approver: str
    iat: int
    exp: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, key: bytes) -> str:
    return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def issue_token(claims: ApprovalClaims, key: bytes) -> str:
    if len(key) < 32:
        raise ValueError("approval signing key must be at least 32 bytes")
    payload = _b64(json.dumps(claims.model_dump(), sort_keys=True).encode())
    return f"{payload}.{_sign(payload, key)}"


def new_claims(
    *,
    approval_id: str,
    incident_id: str,
    action_sha256: str,
    approver: str,
    ttl_seconds: int,
    now: float | None = None,
) -> ApprovalClaims:
    iat = int(now if now is not None else time.time())
    return ApprovalClaims(
        approval_id=approval_id,
        incident_id=incident_id,
        action_sha256=action_sha256,
        approver=approver,
        iat=iat,
        exp=iat + ttl_seconds,
    )


def verify_token(
    token: str, key: bytes, *, action_sha256: str, now: float | None = None
) -> ApprovalClaims:
    try:
        payload, signature = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed approval token") from exc
    if not hmac.compare_digest(signature, _sign(payload, key)):
        raise TokenError("approval token signature is invalid")
    try:
        claims = ApprovalClaims.model_validate_json(_unb64(payload))
    except (ValidationError, ValueError) as exc:
        raise TokenError("approval token payload is invalid") from exc
    if (now if now is not None else time.time()) >= claims.exp:
        raise TokenError("approval token has expired")
    if not hmac.compare_digest(claims.action_sha256, action_sha256):
        raise TokenError("approval token was issued for a different action")
    return claims


class _ExecutorTables(SQLModel, registry=registry()):
    """Separate metadata, so the executor's tables never mix with the backend's."""


class ConsumedApproval(_ExecutorTables, table=True):
    __tablename__ = "consumed_approval"

    approval_id: str = Field(primary_key=True)
    incident_id: str
    approver: str
    consumed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NonceStore:
    """The executor's own record of used approvals (separate DB from the backend's)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        _ExecutorTables.metadata.create_all(engine)

    def consume(self, claims: ApprovalClaims) -> None:
        with Session(self._engine) as session:
            session.add(
                ConsumedApproval(
                    approval_id=claims.approval_id,
                    incident_id=claims.incident_id,
                    approver=claims.approver,
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:
                raise TokenError("approval has already been used") from exc

    def is_consumed(self, approval_id: str) -> bool:
        with Session(self._engine) as session:
            return session.get(ConsumedApproval, approval_id) is not None
