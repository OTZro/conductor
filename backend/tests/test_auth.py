"""Auth tests: middleware gating, Google login, setup-token passkey bootstrap,
sessions, WebAuthn option generation, and the ingest token.

The app is driven through httpx.ASGITransport with the db engine swapped to a
throwaway sqlite file — no docker/postgres needed. Full passkey ceremonies
(actual authenticator signatures) are covered by the E2E script, not here."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conductor import db as db_mod
from conductor.auth import service
from conductor.config import settings
from conductor.main import app
from conductor.models import AuthSession, User, utcnow

ORIGIN = f"http://127.0.0.1:{settings.port}"
ALLOWED = "you@example.com"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/auth-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # middleware + get_session both resolve these module attrs at call time
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(settings, "allowed_emails", ALLOWED)
    monkeypatch.setattr(settings, "allowed_origins", "")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    await engine.dispose()


async def _make_user(email: str = ALLOWED) -> User:
    async with db_mod.session_maker() as s:
        user = User(email=email, created_at=utcnow())
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def _make_session(user: User) -> str:
    async with db_mod.session_maker() as s:
        return await service.create_session(s, user)


def _cookie(raw: str) -> dict[str, str]:
    return {"Cookie": f"{service.SESSION_COOKIE}={raw}"}


# --- 1. auth disabled: everything open ---


async def test_disabled_api_open_without_cookie(client):
    assert settings.auth_enabled is False
    r = await client.get("/api/cards")
    assert r.status_code == 200


async def test_disabled_me_reports_auth_off(client):
    r = await client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_enabled"] is False
    assert body["authenticated"] is False
    assert body["user"] is None


async def test_passkey_list_requires_session_even_when_disabled(client):
    r = await client.get("/api/auth/passkeys")
    assert r.status_code == 401


# --- 2. auth enabled: gating map ---


async def test_enabled_gates_api_but_exempts_auth_ingest_static(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "ingest_token", "")  # isolate from a host env token
    assert (await client.get("/api/cards")).status_code == 401
    assert (await client.get("/term/nonexistent")).status_code == 401

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["auth_enabled"] is True
    assert me.json()["authenticated"] is False

    assert (await client.get("/")).status_code != 401  # static SPA stays open
    # claude-hook posts are bare curl (no session cookie) — must not 401.
    # 404 (unknown card) is the expected pass-through here.
    assert (await client.post("/api/cards/nope/agent", json={"state": "working"})).status_code == 404
    assert (await client.post("/api/cards/nope/notify", json={"message": "m"})).status_code == 404


# --- 3. google login ---


async def test_google_login_allowlisted_sets_session(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")

    async def fake_verify(credential: str) -> dict:
        return {
            "email": "You@Example.com",  # mixed case → normalized
            "email_verified": True,
            "name": "You",
            "picture": "https://example.com/p.png",
        }

    monkeypatch.setattr(service, "verify_google_token", fake_verify)
    r = await client.post("/api/auth/google", json={"credential": "tok"})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == ALLOWED
    assert service.SESSION_COOKIE in r.cookies

    monkeypatch.setattr(settings, "auth_enabled", True)
    assert (await client.get("/api/cards")).status_code == 200  # cookie jar carries session


async def test_google_login_non_allowlisted_403_no_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")

    async def fake_verify(credential: str) -> dict:
        return {"email": "mallory@example.com", "email_verified": True}

    monkeypatch.setattr(service, "verify_google_token", fake_verify)
    r = await client.post("/api/auth/google", json={"credential": "tok"})
    assert r.status_code == 403
    assert r.json()["detail"] == "email not allowed"
    assert "set-cookie" not in {k.lower() for k in r.headers}
    async with db_mod.session_maker() as s:
        assert (await s.execute(select(AuthSession))).scalars().first() is None


async def test_google_login_unverified_email_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")

    async def fake_verify(credential: str) -> dict:
        return {"email": ALLOWED, "email_verified": False}

    monkeypatch.setattr(service, "verify_google_token", fake_verify)
    r = await client.post("/api/auth/google", json={"credential": "tok"})
    assert r.status_code == 401
    assert "set-cookie" not in {k.lower() for k in r.headers}


async def test_google_login_503_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    r = await client.post("/api/auth/google", json={"credential": "tok"})
    assert r.status_code == 503


# --- 4. setup-token passkey bootstrap ---


async def test_register_options_wrong_setup_token_403(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_setup_token", "right-token")
    r = await client.post(
        "/api/auth/webauthn/register/options",
        json={"setup_token": "wrong", "email": ALLOWED},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 403


async def test_register_options_non_allowlisted_email_403(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_setup_token", "right-token")
    r = await client.post(
        "/api/auth/webauthn/register/options",
        json={"setup_token": "right-token", "email": "mallory@example.com"},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 403


async def test_register_options_happy_path(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_setup_token", "right-token")
    r = await client.post(
        "/api/auth/webauthn/register/options",
        json={"setup_token": "right-token", "email": ALLOWED},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state_id"]
    assert body["options"]["challenge"]
    assert body["options"]["rp"]["id"] == "127.0.0.1"
    assert body["options"]["user"]["name"] == ALLOWED


async def test_register_options_missing_or_bad_origin_403(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_setup_token", "right-token")
    payload = {"setup_token": "right-token", "email": ALLOWED}
    r = await client.post("/api/auth/webauthn/register/options", json=payload)
    assert r.status_code == 403  # no Origin header
    r = await client.post(
        "/api/auth/webauthn/register/options",
        json=payload,
        headers={"Origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


async def test_register_verify_unknown_state_400(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_setup_token", "right-token")
    r = await client.post(
        "/api/auth/webauthn/register/verify",
        json={"state_id": "nope", "credential": {}, "setup_token": "right-token"},
    )
    assert r.status_code == 400


async def test_register_verify_garbage_credential_400(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_setup_token", "right-token")
    opts = await client.post(
        "/api/auth/webauthn/register/options",
        json={"setup_token": "right-token", "email": ALLOWED},
        headers={"Origin": ORIGIN},
    )
    r = await client.post(
        "/api/auth/webauthn/register/verify",
        json={
            "state_id": opts.json()["state_id"],
            "credential": {"id": "AAAA", "rawId": "AAAA", "type": "public-key", "response": {}},
            "setup_token": "right-token",
        },
    )
    assert r.status_code == 400
    assert "set-cookie" not in {k.lower() for k in r.headers}


# --- 5. webauthn login options / verify ---


async def test_login_options_discoverable(client):
    r = await client.post("/api/auth/webauthn/login/options", headers={"Origin": ORIGIN})
    assert r.status_code == 200
    body = r.json()
    assert body["state_id"]
    assert body["options"]["challenge"]
    assert not body["options"].get("allowCredentials")  # discoverable-credential flow


async def test_login_verify_unknown_credential_401_no_cookie(client):
    opts = await client.post("/api/auth/webauthn/login/options", headers={"Origin": ORIGIN})
    r = await client.post(
        "/api/auth/webauthn/login/verify",
        json={
            "state_id": opts.json()["state_id"],
            "credential": {"id": "AAAA", "rawId": "AAAA", "type": "public-key", "response": {}},
        },
    )
    assert r.status_code == 401
    assert "set-cookie" not in {k.lower() for k in r.headers}


# --- 6. sessions: expiry + logout ---


async def test_expired_session_is_401(client, monkeypatch):
    user = await _make_user()
    raw = await _make_session(user)
    async with db_mod.session_maker() as s:
        row = (await s.execute(select(AuthSession))).scalars().first()
        row.expires_at = utcnow() - timedelta(days=1)
        s.add(row)
        await s.commit()
    monkeypatch.setattr(settings, "auth_enabled", True)
    r = await client.get("/api/cards", headers=_cookie(raw))
    assert r.status_code == 401


async def test_valid_session_passes_then_logout_invalidates(client, monkeypatch):
    user = await _make_user()
    raw = await _make_session(user)
    monkeypatch.setattr(settings, "auth_enabled", True)
    assert (await client.get("/api/cards", headers=_cookie(raw))).status_code == 200

    me = await client.get("/api/auth/me", headers=_cookie(raw))
    assert me.json()["authenticated"] is True
    assert me.json()["user"]["email"] == ALLOWED

    assert (await client.post("/api/auth/logout", headers=_cookie(raw))).status_code == 200
    assert (await client.get("/api/cards", headers=_cookie(raw))).status_code == 401


# --- 7. ingest token ---


async def test_hook_posts_honor_ingest_token(client, monkeypatch):
    monkeypatch.setattr(settings, "ingest_token", "ing-tok")
    for path, body in (
        ("/api/cards/nope/agent", {"state": "working"}),
        ("/api/cards/nope/notify", {"message": "m"}),
    ):
        assert (await client.post(path, json=body)).status_code == 401
        assert (
            await client.post(path, json=body, headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        # correct token → past the gate; 404 = unknown card (the endpoint itself ran)
        assert (
            await client.post(path, json=body, headers={"Authorization": "Bearer ing-tok"})
        ).status_code == 404
        # scheme name is case-insensitive (RFC 7235)
        assert (
            await client.post(path, json=body, headers={"Authorization": "bearer ing-tok"})
        ).status_code == 404


async def test_notify_drops_generic_idle_nudge(client, monkeypatch):
    """The amber banner is reserved for concrete asks (permission etc.) — the generic
    ≥60s-idle nudge duplicates the pane poll's waiting line on every idle card."""
    monkeypatch.setattr(settings, "ingest_token", "")  # isolate from a host env token
    card = (await client.post("/api/cards", json={"title": "t"})).json()
    cid = card["id"]
    r = await client.post(f"/api/cards/{cid}/notify", json={"message": "Claude is waiting for your input"})
    assert r.json().get("ignored")
    detail = (await client.get(f"/api/cards/{cid}")).json()
    assert not ((detail.get("cached") or {}).get("agent") or {}).get("notification")
    # a concrete ask IS stored
    r = await client.post(f"/api/cards/{cid}/notify", json={"message": "Claude needs your permission to use Bash"})
    assert r.status_code == 200 and not r.json().get("ignored")
    detail = (await client.get(f"/api/cards/{cid}")).json()
    assert ((detail.get("cached") or {}).get("agent") or {}).get("notification") == "Claude needs your permission to use Bash"


# --- 8. websocket handshake rejected when unauthenticated ---


def test_ws_rejected_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    tc = TestClient(app)  # no context manager → lifespan (pollers) never starts
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with tc.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4401
