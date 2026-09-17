"""updater.service: turn git output into the widget's status, and gate apply."""

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from conductor.config import settings
from conductor.main import app
from conductor.plugins.updater import service


def fake_git(monkeypatch, replies: dict[tuple, tuple[int, str]]):
    """Stub _git, keyed by the git subcommand tuple. Unlisted commands → (0, "")."""

    async def _git(*args, timeout=30):
        return replies.get(args, (0, ""))

    monkeypatch.setattr(service, "_git", _git)


UP = ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
HEAD = ("rev-parse", "--short", "HEAD")
BR = ("rev-parse", "--abbrev-ref", "HEAD")
DIRTY = ("status", "--porcelain", "--untracked-files=no")
LOG = ("log", "--format=%h%x09%s", "HEAD..@{u}")
AHEAD = ("rev-list", "--count", "@{u}..HEAD")
FETCH = ("fetch", "--quiet")


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Start from a clean cache, and route every key a test may write through
    monkeypatch.setitem so the module-level state is restored afterwards — the file stays
    order-independent as it grows."""
    for key, value in {
        "applying": False, "applying_since": None, "checked_at": None,
        "current": None, "behind": 0, "fetch_error": False, "git_error": False,
    }.items():
        monkeypatch.setitem(service._state, key, value)


@pytest.mark.asyncio
async def test_behind_upstream_is_updatable(monkeypatch):
    fake_git(monkeypatch, {
        BR: (0, "master"), HEAD: (0, "aaaaaaa"), UP: (0, "origin/master"),
        DIRTY: (0, ""),  # clean
        LOG: (0, "ccccccc\tfeat: two\nbbbbbbb\tfix: one"),
        AHEAD: (0, "0"),
    })
    st = await service._compute()
    assert st["behind"] == 2
    assert st["commits"][0] == {"sha": "ccccccc", "subject": "feat: two"}
    assert st["updatable"] is True and st["blocked_reason"] is None


@pytest.mark.asyncio
async def test_up_to_date_is_not_updatable(monkeypatch):
    fake_git(monkeypatch, {
        BR: (0, "master"), HEAD: (0, "aaaaaaa"), UP: (0, "origin/master"),
        DIRTY: (0, ""), LOG: (0, ""), AHEAD: (0, "0"),
    })
    st = await service._compute()
    assert st["behind"] == 0 and st["updatable"] is False
    assert st["blocked_reason"] is None  # not blocked — just current


@pytest.mark.asyncio
async def test_diverged_branch_is_blocked(monkeypatch):
    """Both sides moved → --ff-only can't apply it, so the button must not offer it."""
    fake_git(monkeypatch, {
        BR: (0, "master"), HEAD: (0, "aaaaaaa"), UP: (0, "origin/master"),
        DIRTY: (0, ""),
        LOG: (0, "ccccccc\tfeat: incoming"),  # behind 1
        AHEAD: (0, "2"),  # ahead 2 → diverged
    })
    st = await service._compute()
    assert st["behind"] == 1 and st["ahead"] == 2
    assert st["updatable"] is False
    assert "diverge" in st["blocked_reason"]


@pytest.mark.asyncio
async def test_dirty_tree_blocks_update(monkeypatch):
    fake_git(monkeypatch, {
        BR: (0, "master"), HEAD: (0, "aaaaaaa"), UP: (0, "origin/master"),
        DIRTY: (0, " M backend/conductor/x.py"),
        LOG: (0, "bbbbbbb\tfix: one"), AHEAD: (0, "0"),
    })
    st = await service._compute()
    assert st["dirty"] is True and st["updatable"] is False
    assert "local changes" in st["blocked_reason"]


@pytest.mark.asyncio
async def test_no_upstream_blocks_update(monkeypatch):
    fake_git(monkeypatch, {
        BR: (0, "feature"), HEAD: (0, "aaaaaaa"),
        UP: (128, ""), DIRTY: (0, ""),
    })
    st = await service._compute()
    assert st["has_upstream"] is False and st["updatable"] is False
    assert "no upstream" in st["blocked_reason"]


@pytest.mark.asyncio
async def test_failed_fetch_raises_and_keeps_prior_status(monkeypatch):
    """A failed `git fetch` must not report the checkout as freshly-confirmed current: it
    raises (so the poller records failure) and leaves the cached status untouched."""
    service._state.update({"current": "aaaaaaa", "behind": 3, "checked_at": "2026-01-01T00:00:00+00:00"})
    fake_git(monkeypatch, {FETCH: (1, "")})  # fetch fails
    with pytest.raises(RuntimeError):
        await service.refresh()
    assert service._state["fetch_error"] is True
    assert service._state["behind"] == 3  # prior status retained, not zeroed


@pytest.mark.asyncio
async def test_apply_refused_when_not_updatable(monkeypatch):
    fake_git(monkeypatch, {  # no upstream → not updatable
        BR: (0, "feature"), HEAD: (0, "aaaaaaa"), UP: (128, ""), DIRTY: (0, ""),
    })
    res = await service.apply()
    assert res["started"] is False
    assert "no upstream" in res["reason"]


@pytest.mark.asyncio
async def test_apply_is_single_flight(monkeypatch):
    """A second /apply while one is already running is rejected, not launched again."""
    monkeypatch.setitem(service._state, "applying", True)
    monkeypatch.setitem(service._state, "applying_since", time.monotonic())
    res = await service.apply()
    assert res == {"started": False, "reason": "an update is already in progress"}


@pytest.mark.asyncio
async def test_stale_applying_claim_does_not_wedge_the_button(monkeypatch):
    """Every conductorctl failure path exits before the restart that would reset module state.
    A claim older than the TTL is a leftover, so admission must not treat it as in-flight."""
    monkeypatch.setitem(service._state, "applying", True)
    monkeypatch.setitem(service._state, "applying_since", time.monotonic() - service._APPLY_CLAIM_TTL_S - 1)
    fake_git(monkeypatch, {  # no upstream → refused on the guard, not on the stale claim
        BR: (0, "feature"), HEAD: (0, "aaaaaaa"), UP: (128, ""), DIRTY: (0, ""),
    })
    res = await service.apply()
    assert res["started"] is False
    assert "no upstream" in res["reason"]


@pytest.mark.asyncio
async def test_updater_exit_releases_the_claim():
    """The watcher drops `applying` as soon as the detached updater exits, so a failed update
    re-arms the button instead of holding it for the life of the backend process."""
    service._state.update({"applying": True, "applying_since": time.monotonic()})
    proc = await asyncio.create_subprocess_exec("false")
    await service._release_when_updater_exits(proc)
    assert service._state["applying"] is False
    assert service._state["applying_since"] is None


@pytest.mark.asyncio
async def test_failed_git_is_not_reported_as_clean_and_current(monkeypatch):
    """`_git` returns (1, "") on failure/timeout, which looks exactly like a clean tree with no
    incoming commits. Unchecked, the widget says "you're on the latest" over no information."""
    fake_git(monkeypatch, {
        BR: (0, "master"), HEAD: (0, "aaaaaaa"), UP: (0, "origin/master"),
        DIRTY: (1, ""),  # git status failed / timed out
        LOG: (0, ""), AHEAD: (0, "0"),
    })
    st = await service._compute()
    assert st["git_error"] is True and st["updatable"] is False
    assert "couldn't read" in st["blocked_reason"]


@pytest.mark.asyncio
async def test_failed_log_is_not_reported_as_up_to_date(monkeypatch):
    fake_git(monkeypatch, {
        BR: (0, "master"), HEAD: (0, "aaaaaaa"), UP: (0, "origin/master"),
        DIRTY: (0, ""), LOG: (1, ""), AHEAD: (0, "0"),
    })
    st = await service._compute()
    assert st["behind"] == 0 and st["git_error"] is True and st["updatable"] is False


@pytest.mark.asyncio
async def test_healthy_status_carries_no_git_error(monkeypatch):
    fake_git(monkeypatch, {
        BR: (0, "master"), HEAD: (0, "aaaaaaa"), UP: (0, "origin/master"),
        DIRTY: (0, ""), LOG: (0, "bbbbbbb\tfix: one"), AHEAD: (0, "0"),
    })
    st = await service._compute()
    assert st["git_error"] is False and st["updatable"] is True


# --- CSRF gate on the mutating endpoints ---------------------------------------------------
# /check and /apply take no body and no custom header, so they are CORS *simple* requests any
# page can fire cross-site, and auth_enabled defaults to False. The Origin check is what keeps
# a foreign page from triggering a pull-and-restart.


@pytest.fixture
async def api(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/update/check", "/api/update/apply"])
async def test_mutating_endpoints_reject_a_foreign_origin(api, path, monkeypatch):
    called = False

    async def _boom(*_a, **_k):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(service, "refresh", _boom)
    monkeypatch.setattr(service, "apply", _boom)
    r = await api.post(path, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert called is False  # rejected before any git ran


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/update/check", "/api/update/apply"])
async def test_mutating_endpoints_reject_a_missing_origin(api, path):
    assert (await api.post(path)).status_code == 403


@pytest.mark.asyncio
async def test_apply_accepts_the_widgets_own_origin(api, monkeypatch):
    """Browsers send Origin on every non-GET fetch, same-origin included — the widget works."""
    async def _apply():
        return {"started": False, "reason": "already up to date"}

    monkeypatch.setattr(service, "apply", _apply)
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post("/api/update/apply", headers={"Origin": origin})
    assert r.status_code == 200 and r.json()["started"] is False


@pytest.mark.asyncio
async def test_status_stays_open_to_a_plain_get(api, monkeypatch):
    """GET is a read and same-origin GETs carry no Origin, so the poll must not be gated."""
    async def _current():
        return {"behind": 0}

    monkeypatch.setattr(service, "current", _current)
    assert (await api.get("/api/update/status")).status_code == 200
