import base64
import json
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from app.executor.tokens import (
    NonceStore,
    TokenError,
    issue_token,
    new_claims,
    verify_token,
)
from app.models.db import create_db_engine

KEY = b"s" * 32
NOW = 1_800_000_000.0


def _token(**overrides: object) -> str:
    claims = new_claims(
        approval_id="ap1",
        incident_id="inc1",
        action_sha256="abc",
        approver="alice",
        ttl_seconds=300,
        now=NOW,
    )
    return issue_token(claims.model_copy(update=overrides), KEY)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine("sqlite://")
    yield eng
    eng.dispose()


def test_round_trip() -> None:
    claims = verify_token(_token(), KEY, action_sha256="abc", now=NOW + 10)
    assert claims.approval_id == "ap1"
    assert claims.approver == "alice"
    assert claims.exp == int(NOW) + 300


def test_expired() -> None:
    with pytest.raises(TokenError, match="expired"):
        verify_token(_token(), KEY, action_sha256="abc", now=NOW + 300)


def test_bound_to_action() -> None:
    with pytest.raises(TokenError, match="different action"):
        verify_token(_token(), KEY, action_sha256="something-else", now=NOW)


def test_wrong_key() -> None:
    with pytest.raises(TokenError, match="signature"):
        verify_token(_token(), b"x" * 32, action_sha256="abc", now=NOW)


def test_tampered_payload() -> None:
    payload, sig = _token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["exp"] += 10_000
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(TokenError, match="signature"):
        verify_token(f"{forged}.{sig}", KEY, action_sha256="abc", now=NOW)


@pytest.mark.parametrize("token", ["", "no-dot", "a.b.c", ".", "xx.yy"])
def test_malformed(token: str) -> None:
    with pytest.raises(TokenError):
        verify_token(token, KEY, action_sha256="abc", now=NOW)


def test_signed_garbage_payload() -> None:
    from app.executor.tokens import _b64, _sign

    payload = _b64(b"not json")
    with pytest.raises(TokenError, match="payload"):
        verify_token(f"{payload}.{_sign(payload, KEY)}", KEY, action_sha256="abc", now=NOW)


def test_short_key_rejected() -> None:
    claims = new_claims(
        approval_id="a", incident_id="i", action_sha256="h", approver="x", ttl_seconds=1
    )
    with pytest.raises(ValueError, match="32 bytes"):
        issue_token(claims, b"short")


def test_nonce_store_is_single_use(engine: Engine) -> None:
    store = NonceStore(engine)
    claims = verify_token(_token(), KEY, action_sha256="abc", now=NOW)
    assert not store.is_consumed("ap1")
    store.consume(claims)
    assert store.is_consumed("ap1")
    with pytest.raises(TokenError, match="already been used"):
        store.consume(claims)
