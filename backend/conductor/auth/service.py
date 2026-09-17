"""Auth core: session tokens, allowlist, Google id-token verify, WebAuthn ceremonies.

Sessions are opaque `secrets.token_urlsafe` values stored ONLY as sha256 hashes.
WebAuthn challenges live in a module-level in-memory store — Conductor is a
single-process app, so that's sufficient (a uvicorn reload only voids in-flight
login/register ceremonies, never persisted credentials)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.concurrency import run_in_threadpool
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..config import settings
from ..models import AuthSession, User, WebAuthnCredential, utcnow

SESSION_COOKIE = "conductor_session"
RP_NAME = "Conductor"
_LAST_SEEN_BUMP_S = 300  # write last_seen_at at most once per 5 min (not per request)
_CHALLENGE_TTL_S = 300


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(dt):  # noqa: ANN001 — datetime | None; sqlite returns tz-naive datetimes
    """Normalize DB datetimes: sqlite (tests) hands back naive UTC, postgres aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- allowlist / token compares ---


def email_allowed(email: str) -> bool:
    return email.strip().lower() in settings.allowed_email_set


def setup_token_ok(candidate: str) -> bool:
    """Constant-time compare; an EMPTY configured token disables the setup flow."""
    return bool(settings.auth_setup_token) and hmac.compare_digest(
        candidate or "", settings.auth_setup_token
    )


def ingest_token_ok(candidate: str) -> bool:
    return bool(settings.ingest_token) and hmac.compare_digest(
        candidate or "", settings.ingest_token
    )


def require_ingest_token(request: Request) -> None:
    """Guard for the session-exempt machine-push endpoints — the core claude hooks
    (/agent, /notify) and every plugin capture hook. AuthMiddleware never gives these a
    session cookie (bare curl from hooks), so this Bearer check is their only guard:
    when CONDUCTOR_INGEST_TOKEN is set they require `Authorization: Bearer <token>`; an
    empty configured token keeps them open. Shared so a plugin reuses the exact same
    check as core rather than re-deriving the RFC-7235 parse."""
    if not settings.ingest_token:
        return
    auth_header = request.headers.get("authorization") or ""
    # auth-scheme names are case-insensitive (RFC 7235) — accept "bearer" too
    m = re.match(r"bearer\s+(.+)", auth_header, re.I)
    if not ingest_token_ok(m.group(1).strip() if m else ""):
        raise HTTPException(status_code=401, detail="invalid ingest token")


# --- sessions ---


async def create_session(db: AsyncSession, user: User, user_agent: str = "") -> str:
    """Create a login session; returns the RAW token (only its hash is stored)."""
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        AuthSession(
            token_hash=_hash_token(raw),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(days=settings.session_ttl_days),
            last_seen_at=now,
            user_agent=(user_agent or "")[:400],
        )
    )
    await db.commit()
    return raw


async def validate_session_token(db: AsyncSession, raw: str) -> User | None:
    """Resolve a raw cookie token to its User, or None (missing/expired/unknown)."""
    if not raw:
        return None
    row = (
        await db.execute(select(AuthSession).where(AuthSession.token_hash == _hash_token(raw)))
    ).scalars().first()
    if row is None:
        return None
    now = utcnow()
    expires = _aware(row.expires_at)
    if expires is None or expires <= now:
        return None
    last_seen = _aware(row.last_seen_at)
    if last_seen is None or (now - last_seen).total_seconds() > _LAST_SEEN_BUMP_S:
        row.last_seen_at = now
        db.add(row)
        await db.commit()
    return await db.get(User, row.user_id)


async def delete_session(db: AsyncSession, raw: str) -> None:
    if not raw:
        return
    row = (
        await db.execute(select(AuthSession).where(AuthSession.token_hash == _hash_token(raw)))
    ).scalars().first()
    if row is not None:
        await db.delete(row)
        await db.commit()


# --- Google sign-in ---


async def verify_google_token(credential: str) -> dict[str, Any]:
    """Verify a Google Identity Services id token. Pins audience to our client id
    (google-auth also enforces the accounts.google.com issuer). Sync/network call,
    so it runs in the threadpool."""

    def _verify() -> dict[str, Any]:
        return id_token.verify_oauth2_token(
            credential, google_requests.Request(), settings.google_client_id
        )

    try:
        claims = await run_in_threadpool(_verify)
    except Exception as exc:  # noqa: BLE001 — google-auth raises ValueError and friends
        raise HTTPException(status_code=401, detail="invalid google token") from exc
    if claims.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="google email not verified")
    return claims


# --- WebAuthn ceremony state (in-memory challenge store) ---

_challenges: dict[str, dict[str, Any]] = {}


def _prune_challenges() -> None:
    now = time.time()
    for key in [k for k, v in _challenges.items() if v["expires"] < now]:
        _challenges.pop(key, None)


def stash_challenge(
    *, challenge: bytes, purpose: str, user_id: str | None, rp_id: str, origin: str
) -> str:
    _prune_challenges()
    state_id = secrets.token_urlsafe(16)
    _challenges[state_id] = {
        "challenge": challenge,
        "purpose": purpose,  # register | login
        "user_id": user_id,
        "rp_id": rp_id,
        "origin": origin,
        "expires": time.time() + _CHALLENGE_TTL_S,
    }
    return state_id


def pop_challenge(state_id: str, purpose: str) -> dict[str, Any] | None:
    _prune_challenges()
    state = _challenges.pop(state_id or "", None)
    if state is None or state["purpose"] != purpose:
        return None
    return state


def require_origin(request: Request) -> tuple[str, str]:
    """The browser Origin header must exactly match an allowed origin — it anchors
    both the WebAuthn ceremony and the rp_id. Returns (origin, rp_id)."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin or origin not in settings.allowed_origin_set:
        raise HTTPException(status_code=403, detail="origin not allowed")
    hostname = urlparse(origin).hostname or ""
    if not hostname:
        raise HTTPException(status_code=403, detail="origin not allowed")
    return origin, hostname


# --- WebAuthn ceremonies ---


def registration_options(user: User, rp_id: str, origin: str, existing: list[WebAuthnCredential]) -> dict[str, Any]:
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=str(user.id).encode(),
        user_name=user.email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,  # discoverable → login without typing email
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing
        ],
    )
    state_id = stash_challenge(
        challenge=opts.challenge, purpose="register", user_id=user.id, rp_id=rp_id, origin=origin
    )
    return {"state_id": state_id, "options": opts}


def verify_registration(state: dict[str, Any], credential: dict[str, Any]):
    try:
        return verify_registration_response(
            credential=credential,
            expected_challenge=state["challenge"],
            expected_rp_id=state["rp_id"],
            expected_origin=state["origin"],
        )
    except Exception as exc:  # noqa: BLE001 — InvalidRegistrationResponse or malformed input
        raise HTTPException(status_code=400, detail="passkey registration failed") from exc


def authentication_options(rp_id: str, origin: str) -> dict[str, Any]:
    # no allow_credentials → discoverable-credential flow (browser offers stored passkeys)
    opts = generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.PREFERRED
    )
    state_id = stash_challenge(
        challenge=opts.challenge, purpose="login", user_id=None, rp_id=rp_id, origin=origin
    )
    return {"state_id": state_id, "options": opts}


def verify_authentication(state: dict[str, Any], credential: dict[str, Any], stored: WebAuthnCredential):
    try:
        return verify_authentication_response(
            credential=credential,
            expected_challenge=state["challenge"],
            expected_rp_id=state["rp_id"],
            expected_origin=state["origin"],
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=False,
        )
    except Exception as exc:  # noqa: BLE001 — InvalidAuthenticationResponse or malformed input
        raise HTTPException(status_code=401, detail="passkey login failed") from exc


def options_json(opts) -> dict[str, Any]:  # noqa: ANN001 — py_webauthn options struct
    return json.loads(options_to_json(opts))


def b64url(data: bytes) -> str:
    return bytes_to_base64url(data)
