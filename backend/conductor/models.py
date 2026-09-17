from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt() -> Column:
    return Column(DateTime(timezone=True))


class Card(SQLModel, table=True):
    """A unit of work surfaced from any source. Mostly a projection of the source
    of truth (Jira / GitHub / Slack); only `id` and the joined LocalState are
    conductor-owned."""

    __tablename__ = "card"
    __table_args__ = (
        UniqueConstraint("origin", "external_id", name="uq_card_origin_external"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    origin: str = Field(index=True)  # jira | github | slack | manual
    external_id: str = Field(index=True)  # PROJ-123 | owner/repo#42 | slack ts | uuid
    title: str = ""
    summary: str = ""
    url: str | None = None
    ball: str = Field(default="none", index=True)  # human | ai | none
    driver: str = Field(default="none")  # none
    agent_state: str | None = None  # human-readable phase, e.g. "Building", "awaiting input"
    cached: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime | None = Field(default=None, sa_column=_dt())
    updated_at: datetime | None = Field(default=None, sa_column=_dt())
    last_seen_at: datetime | None = Field(default=None, sa_column=_dt())


class CardLink(SQLModel, table=True):
    """Related ticket / PR / Slack link on a card (auto-discovered or manual)."""

    __tablename__ = "card_link"
    __table_args__ = (
        UniqueConstraint("card_id", "kind", "ref", name="uq_link_card_kind_ref"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    card_id: str = Field(index=True, foreign_key="card.id")
    kind: str  # jira | pr | slack | url
    ref: str
    url: str
    title: str | None = None
    auto: bool = Field(default=True, sa_column=Column(Boolean))


class LocalState(SQLModel, table=True):
    """Conductor-owned per-card state that has no source of truth elsewhere."""

    __tablename__ = "local_state"

    card_id: str = Field(primary_key=True, foreign_key="card.id")
    read: bool = Field(default=False, sa_column=Column(Boolean))
    dismissed: bool = Field(default=False, sa_column=Column(Boolean))
    pinned: bool = Field(default=False, sa_column=Column(Boolean))  # keep a job prominent (any origin)
    snoozed_until: datetime | None = Field(default=None, sa_column=_dt())
    # manual board-stage override (a registered human-space stage key, e.g. "pending").
    # Applied only while ball == human (lanes.lane_for_card); auto-cleared when the card
    # reaches done so a reopened ticket doesn't silently resurrect into an old stage.
    manual_stage: str | None = None
    picked_option: str | None = None
    picked_at: datetime | None = Field(default=None, sa_column=_dt())
    note: str | None = None
    # remembered terminal context for this card. claude conversations live on the
    # machine that created them, so the session id only resumes on `host`
    # (None = local/base).
    workdir: str | None = None
    claude_session_id: str | None = None
    host: str | None = None


class Pin(SQLModel, table=True):
    """A manually-pinned Jira ticket key that always shows on the board, regardless
    of the board filter."""

    __tablename__ = "pin"

    key: str = Field(primary_key=True)
    created_at: datetime | None = Field(default=None, sa_column=_dt())


class TerminalSession(SQLModel, table=True):
    """A ttyd-backed terminal bound to a card (attach / takeover / own)."""

    __tablename__ = "terminal_session"

    id: str = Field(default_factory=new_id, primary_key=True)
    card_id: str = Field(index=True, foreign_key="card.id")
    kind: str  # attach | own | resume
    host: str | None = None  # None = local (base); else a remote host name (roam)
    tmux_session: str | None = None
    ttyd_port: int | None = None
    pid: int | None = None
    status: str = "starting"  # starting | live | stopped | error
    url: str | None = None
    cwd: str | None = None
    claude_session_id: str | None = None
    created_at: datetime | None = Field(default=None, sa_column=_dt())


class User(SQLModel, table=True):
    """An allowlisted human who can sign in (Google or WebAuthn passkey)."""

    __tablename__ = "user"

    id: str = Field(default_factory=new_id, primary_key=True)
    email: str = Field(unique=True, index=True)  # stored lowercase
    name: str = ""
    picture: str = ""
    created_at: datetime | None = Field(default=None, sa_column=_dt())
    last_login_at: datetime | None = Field(default=None, sa_column=_dt())


class WebAuthnCredential(SQLModel, table=True):
    """A registered passkey. Binary material is stored base64url-encoded so rows
    are portable across postgres (runtime) and sqlite (tests)."""

    __tablename__ = "webauthn_credential"

    id: str = Field(default_factory=new_id, primary_key=True)
    user_id: str = Field(index=True, foreign_key="user.id")
    credential_id: str = Field(unique=True, index=True)  # base64url
    public_key: str  # base64url
    sign_count: int = 0
    transports: str = ""  # comma-separated (e.g. "internal,hybrid")
    rp_id: str = ""  # relying-party id the passkey was registered under
    nickname: str = ""
    created_at: datetime | None = Field(default=None, sa_column=_dt())
    last_used_at: datetime | None = Field(default=None, sa_column=_dt())


class AuthSession(SQLModel, table=True):
    """A browser login session. Only the sha256 of the cookie token is stored, so
    a DB leak never leaks usable session tokens."""

    __tablename__ = "auth_session"

    id: str = Field(default_factory=new_id, primary_key=True)
    token_hash: str = Field(unique=True, index=True)  # sha256 hex of the raw token
    user_id: str = Field(index=True, foreign_key="user.id")
    created_at: datetime | None = Field(default=None, sa_column=_dt())
    expires_at: datetime | None = Field(default=None, sa_column=_dt())
    last_seen_at: datetime | None = Field(default=None, sa_column=_dt())
    user_agent: str = ""
