"""Auth endpoints: Google login, WebAuthn passkeys, sessions.

Every endpoint here works even while settings.auth_enabled is false — the whole
point is that the owner registers a passkey and test-drives login BEFORE flipping
enforcement on (lockout safety). The middleware exempts /api/auth/* accordingly."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import service
from ..config import settings
from ..db import get_session
from ..models import User, WebAuthnCredential, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _iso(dt) -> str | None:  # noqa: ANN001 — datetime | None
    return dt.isoformat() if dt else None


def _user_payload(user: User) -> dict[str, Any]:
    return {"email": user.email, "name": user.name, "picture": user.picture}


def _set_session_cookie(response: Response, request: Request, raw_token: str) -> None:
    # Secure when the original request was https (tailscale serve terminates TLS and
    # forwards to loopback with X-Forwarded-Proto: https).
    secure = (
        request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    )
    response.set_cookie(
        service.SESSION_COOKIE,
        raw_token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


async def _current_user(request: Request, db: AsyncSession) -> User | None:
    raw = request.cookies.get(service.SESSION_COOKIE, "")
    return await service.validate_session_token(db, raw)


async def _own_credentials(db: AsyncSession, user_id: str) -> list[WebAuthnCredential]:
    rows = await db.execute(
        select(WebAuthnCredential).where(WebAuthnCredential.user_id == user_id)
    )
    return list(rows.scalars().all())


@router.get("/me")
async def me(request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    """Auth status probe — never 401s; the SPA calls this first on load."""
    user = await _current_user(request, db)
    if user is not None:
        passkey_count = len(await _own_credentials(db, user.id))
    else:
        rows = await db.execute(select(WebAuthnCredential))
        passkey_count = len(list(rows.scalars().all()))
    return {
        "auth_enabled": settings.auth_enabled,
        "authenticated": user is not None,
        "user": _user_payload(user) if user else None,
        "google_client_id": settings.google_client_id or None,
        "passkey_count": passkey_count,
        "setup_available": bool(settings.auth_setup_token),
    }


@router.post("/google")
async def google_login(
    payload: dict, request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> dict:
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="google login not configured")
    credential = payload.get("credential") or ""
    if not credential:
        raise HTTPException(status_code=400, detail="missing credential")
    claims = await service.verify_google_token(credential)
    if claims.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="google email not verified")
    email = (claims.get("email") or "").strip().lower()
    if not service.email_allowed(email):
        raise HTTPException(status_code=403, detail="email not allowed")
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalars().first()
    if user is None:
        user = User(email=email, created_at=utcnow())
    user.name = claims.get("name") or user.name
    user.picture = claims.get("picture") or user.picture
    user.last_login_at = utcnow()
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = await service.create_session(db, user, request.headers.get("user-agent", ""))
    _set_session_cookie(response, request, raw)
    return {"user": _user_payload(user)}


@router.post("/webauthn/register/options")
async def webauthn_register_options(
    payload: dict, request: Request, db: AsyncSession = Depends(get_session)
) -> dict:
    origin, rp_id = service.require_origin(request)
    user = await _current_user(request, db)
    if user is None:
        # bootstrap path: setup token + allowlisted email, no session needed
        if not service.setup_token_ok(payload.get("setup_token") or ""):
            raise HTTPException(status_code=403, detail="setup token invalid")
        email = (payload.get("email") or "").strip().lower()
        if not service.email_allowed(email):
            raise HTTPException(status_code=403, detail="email not allowed")
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalars().first()
        if user is None:
            user = User(email=email, created_at=utcnow())
            db.add(user)
            await db.commit()
            await db.refresh(user)
    existing = await _own_credentials(db, user.id)
    result = service.registration_options(user, rp_id, origin, existing)
    return {"state_id": result["state_id"], "options": service.options_json(result["options"])}


@router.post("/webauthn/register/verify")
async def webauthn_register_verify(
    payload: dict, request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> dict:
    state = service.pop_challenge(payload.get("state_id") or "", "register")
    if state is None:
        raise HTTPException(status_code=400, detail="unknown or expired registration state")
    session_user = await _current_user(request, db)
    if session_user is not None:
        if session_user.id != state["user_id"]:
            raise HTTPException(status_code=403, detail="state does not match session")
        user = session_user
    else:
        # bootstrap path: the setup token must accompany the verify step too
        if not service.setup_token_ok(payload.get("setup_token") or ""):
            raise HTTPException(status_code=403, detail="setup token invalid")
        user = await db.get(User, state["user_id"])
        if user is None or not service.email_allowed(user.email):
            raise HTTPException(status_code=403, detail="email not allowed")
    credential = payload.get("credential") or {}
    verified = service.verify_registration(state, credential)
    transports = credential.get("response", {}).get("transports") or []
    db.add(
        WebAuthnCredential(
            user_id=user.id,
            credential_id=service.b64url(verified.credential_id),
            public_key=service.b64url(verified.credential_public_key),
            sign_count=verified.sign_count,
            transports=",".join(transports),
            rp_id=state["rp_id"],
            nickname=(payload.get("nickname") or "")[:100],
            created_at=utcnow(),
        )
    )
    await db.commit()
    result: dict[str, Any] = {"ok": True, "user": _user_payload(user)}
    if session_user is None:
        raw = await service.create_session(db, user, request.headers.get("user-agent", ""))
        _set_session_cookie(response, request, raw)
    return result


@router.post("/webauthn/login/options")
async def webauthn_login_options(request: Request) -> dict:
    origin, rp_id = service.require_origin(request)
    result = service.authentication_options(rp_id, origin)
    return {"state_id": result["state_id"], "options": service.options_json(result["options"])}


@router.post("/webauthn/login/verify")
async def webauthn_login_verify(
    payload: dict, request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> dict:
    state = service.pop_challenge(payload.get("state_id") or "", "login")
    if state is None:
        raise HTTPException(status_code=400, detail="unknown or expired login state")
    credential = payload.get("credential") or {}
    cred_id = credential.get("rawId") or credential.get("id") or ""
    stored = (
        await db.execute(
            select(WebAuthnCredential).where(WebAuthnCredential.credential_id == cred_id)
        )
    ).scalars().first()
    if stored is None:
        raise HTTPException(status_code=401, detail="unknown credential")
    verified = service.verify_authentication(state, credential, stored)
    stored.sign_count = verified.new_sign_count
    stored.last_used_at = utcnow()
    db.add(stored)
    user = await db.get(User, stored.user_id)
    if user is None or not service.email_allowed(user.email):
        # allowlist can shrink after a passkey was registered — re-check at login
        await db.commit()
        raise HTTPException(status_code=403, detail="email not allowed")
    user.last_login_at = utcnow()
    db.add(user)
    await db.commit()
    raw = await service.create_session(db, user, request.headers.get("user-agent", ""))
    _set_session_cookie(response, request, raw)
    return {"user": _user_payload(user)}


@router.post("/logout")
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> dict:
    await service.delete_session(db, request.cookies.get(service.SESSION_COOKIE, ""))
    response.delete_cookie(service.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/passkeys")
async def list_passkeys(request: Request, db: AsyncSession = Depends(get_session)) -> list[dict]:
    user = await _current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return [
        {
            "id": c.id,
            "nickname": c.nickname,
            "rp_id": c.rp_id,
            "created_at": _iso(c.created_at),
            "last_used_at": _iso(c.last_used_at),
        }
        for c in await _own_credentials(db, user.id)
    ]


@router.delete("/passkeys/{credential_id}")
async def delete_passkey(
    credential_id: str, request: Request, db: AsyncSession = Depends(get_session)
) -> dict:
    user = await _current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    cred = await db.get(WebAuthnCredential, credential_id)
    if cred is None or cred.user_id != user.id:
        raise HTTPException(status_code=404, detail="passkey not found")
    await db.delete(cred)
    await db.commit()
    return {"ok": True}
