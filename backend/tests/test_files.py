"""Files plugin: the SendUserFile capture endpoint (reads each sent path via `_run`,
stashes to the plugin's own store + sidecar), the download traversal guard, and the card
provider. The capture hook forwards the raw PostToolUse payload; we read the file paths
off it — so these tests write real files and pass their paths (host='' → read locally,
the same code path a remote session hits over ssh). sqlite-backed (no postgres)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor.config import settings
from conductor.main import app
from conductor.models import Card
from conductor.plugins.files import _card_data, storage


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/files-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(storage, "FILES_DIR", tmp_path / "files")  # plugin owns its store
    monkeypatch.setattr(settings, "auth_enabled", False)  # don't gate on a host env token
    monkeypatch.setattr(settings, "ingest_token", "")  # open unless a test sets it
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
    await engine.dispose()


async def _make_card(client) -> str:
    return (await client.post("/api/cards", json={"title": "file test"})).json()["id"]


def _src(tmp_path, name: str, content: bytes) -> str:
    """A source file on disk (what the session's claude would have sent); returns its path."""
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


async def _capture(client, card_id: str, paths, caption: str = "", token: str | None = None):
    """POST the PostToolUse/SendUserFile payload to the plugin capture endpoint."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.post(
        f"/api/plugins/files/capture?card_id={card_id}&host=",
        json={"tool_input": {"files": paths, "caption": caption}},
        headers=headers,
    )


async def test_capture_stashes_file_and_records_metadata(client, tmp_path):
    cid = await _make_card(client)
    src = _src(tmp_path, "report.json", b'{"ok":true}')
    r = await _capture(client, cid, [src], caption="the report")
    assert r.status_code == 200 and r.json()["saved"] == ["report.json"]
    dest = storage.FILES_DIR / cid / "report.json"
    assert dest.is_file() and dest.read_bytes() == b'{"ok":true}'
    files = storage.list_files(cid)
    assert len(files) == 1
    assert files[0]["name"] == "report.json" and files[0]["caption"] == "the report" and files[0]["size"] == 11


async def test_resend_replaces_not_duplicates(client, tmp_path):
    cid = await _make_card(client)
    for content in (b"v1", b"v2longer"):
        await _capture(client, cid, [_src(tmp_path, "x.txt", content)])
    files = storage.list_files(cid)
    assert len(files) == 1 and files[0]["size"] == 8  # latest wins, not duplicated
    assert (storage.FILES_DIR / cid / "x.txt").read_bytes() == b"v2longer"


async def test_multiple_files_in_one_capture(client, tmp_path):
    """A single SendUserFile can carry several files — all land under the card."""
    cid = await _make_card(client)
    paths = [_src(tmp_path, f"{n}.txt", f"c{n}".encode()) for n in range(3)]
    r = await _capture(client, cid, paths)
    assert r.status_code == 200 and sorted(r.json()["saved"]) == ["0.txt", "1.txt", "2.txt"]
    assert sorted(f["name"] for f in storage.list_files(cid)) == ["0.txt", "1.txt", "2.txt"]


async def test_absolute_path_stored_as_basename(client, tmp_path):
    cid = await _make_card(client)
    src = _src(tmp_path, "deep.txt", b"z")  # an absolute path; only its basename is stored
    r = await _capture(client, cid, [src])
    assert r.json()["saved"] == ["deep.txt"]
    assert (storage.FILES_DIR / cid / "deep.txt").is_file()


async def test_same_basename_from_different_dirs_both_survive(client, tmp_path):
    """Two DIFFERENT files that normalize to the same name must not overwrite each other —
    the collider is disambiguated so both blobs + both metadata entries survive."""
    cid = await _make_card(client)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    pa = tmp_path / "a" / "report.json"
    pa.write_bytes(b"AAA")
    pb = tmp_path / "b" / "report.json"
    pb.write_bytes(b"BBBB")
    saved = (await _capture(client, cid, [str(pa), str(pb)])).json()["saved"]
    assert len(saved) == 2 and len(set(saved)) == 2  # two DISTINCT stored names
    assert "report.json" in saved  # first keeps the clean name
    files = storage.list_files(cid)
    assert sorted(f["size"] for f in files) == [3, 4]  # neither overwritten
    for f in files:  # both blobs on disk under their (distinct) names
        assert (storage.FILES_DIR / cid / f["name"]).read_bytes() in (b"AAA", b"BBBB")


async def test_collision_suffix_avoids_a_preexisting_suffix_name(client, tmp_path):
    """The digest-suffixed candidate is itself checked: if a file already holds that exact
    name, the collider keeps incrementing so no bytes are overwritten."""
    import hashlib

    cid = await _make_card(client)
    for d in ("a", "x", "z"):
        (tmp_path / d).mkdir()
    src_a = tmp_path / "a" / "report.json"
    src_a.write_bytes(b"A")  # takes the clean name "report.json"
    collider = tmp_path / "x" / "report.json"  # would collide → wants report-<h>.json
    h = hashlib.sha256(str(collider).encode()).hexdigest()[:8]
    preexisting = tmp_path / "z" / f"report-{h}.json"
    preexisting.write_bytes(b"PRE")  # already occupies that very suffix name
    collider.write_bytes(b"CCCC")
    saved = (await _capture(client, cid, [str(src_a), str(preexisting), str(collider)])).json()["saved"]
    assert set(saved) == {"report.json", f"report-{h}.json", f"report-{h}-2.json"}
    assert sorted(f["size"] for f in storage.list_files(cid)) == [1, 3, 4]  # nothing overwritten


async def test_resend_same_source_replaces_even_after_a_collision(client, tmp_path):
    """Re-sending the SAME source path still replaces (dedup identity is the source, not
    the on-disk name) — a collision suffix must not turn a re-send into a duplicate."""
    cid = await _make_card(client)
    (tmp_path / "a").mkdir()
    p = tmp_path / "a" / "report.json"
    p.write_bytes(b"v1")
    await _capture(client, cid, [str(p)])
    p.write_bytes(b"v2!")
    await _capture(client, cid, [str(p)])
    files = storage.list_files(cid)
    assert len(files) == 1 and files[0]["size"] == 3  # replaced, not duplicated


def test_safe_name_strips_traversal():
    assert storage.safe_name("../../../../tmp/evil.txt") == "evil.txt"
    assert storage.safe_name("/abs/dir/x") == "x"
    assert storage.safe_name("") == "file"


async def test_file_named_like_the_sidecar_does_not_corrupt_metadata(client, tmp_path):
    """A sent file may take ANY name — incl. the metadata sidecar's — without clobbering
    it, because the sidecar lives as a sibling of the card dir, not inside it."""
    cid = await _make_card(client)
    await _capture(client, cid, [_src(tmp_path, "real.txt", b"keep me")], caption="first")
    # now send a file whose name would have collided with the old in-dir sidecar
    r = await _capture(client, cid, [_src(tmp_path, "_files.json", b'["not-metadata"]')])
    assert r.status_code == 200 and "_files.json" in r.json()["saved"]
    names = sorted(f["name"] for f in storage.list_files(cid))
    assert names == ["_files.json", "real.txt"]  # sidecar intact, both files listed
    # the download serves the blob's real bytes, not the metadata
    got = await client.get(f"/api/plugins/files/{cid}/_files.json")
    assert got.status_code == 200 and got.content == b'["not-metadata"]'


async def test_unknown_card_is_rejected_before_stashing(client, tmp_path):
    src = _src(tmp_path, "orphan.txt", b"x")
    r = await _capture(client, "no-such-card", [src])
    assert r.status_code == 404
    assert not (storage.FILES_DIR / "no-such-card").exists()  # no orphan dir


async def test_unreadable_path_is_skipped(client, tmp_path):
    cid = await _make_card(client)
    r = await _capture(client, cid, [str(tmp_path / "does-not-exist.txt")])
    assert r.status_code == 200 and r.json()["saved"] == []  # skipped, hook not failed
    assert storage.list_files(cid) == []


async def test_oversize_file_skipped(client, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "MAX_FILE", 8)  # tiny cap for the test
    cid = await _make_card(client)
    src = _src(tmp_path, "big.bin", b"0123456789ABCDEF")  # 16 bytes > cap
    r = await _capture(client, cid, [src])
    assert r.status_code == 200 and r.json()["saved"] == []
    assert not (storage.FILES_DIR / cid / "big.bin").exists()  # nothing written


async def test_download_roundtrip_and_traversal_guard(client, tmp_path):
    cid = await _make_card(client)
    await _capture(client, cid, [_src(tmp_path, "d.txt", b"hello")])
    r = await client.get(f"/api/plugins/files/{cid}/d.txt")
    assert r.status_code == 200 and r.content == b"hello"
    assert (await client.get(f"/api/plugins/files/{cid}/..%2f..%2fetc%2fpasswd")).status_code == 404
    assert (await client.get(f"/api/plugins/files/{cid}/nope.txt")).status_code == 404


async def test_ingest_token_gates_the_capture(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ingest_token", "ftok")
    cid = await _make_card(client)  # POST /api/cards is not token-gated
    src = _src(tmp_path, "g.txt", b"x")
    assert (await _capture(client, cid, [src])).status_code == 401
    assert (await _capture(client, cid, [src], token="ftok")).status_code == 200


async def test_capture_touches_card_for_live_refresh(client, tmp_path):
    """A captured file must bump updated_at so an open drawer / the board refreshes."""
    cid = await _make_card(client)
    before = (await client.get(f"/api/cards/{cid}")).json()["updated_at"]
    await _capture(client, cid, [_src(tmp_path, "t.txt", b"z")])
    after = (await client.get(f"/api/cards/{cid}")).json()["updated_at"]
    assert after is not None and after > before  # strict: a no-op touch must fail this


async def test_plugin_provider_returns_files_or_none(client, tmp_path):
    cid = await _make_card(client)
    async with db_mod.session_maker() as s:
        card = await s.get(Card, cid)
    ctx = {"origin": card.origin, "external_id": card.external_id, "links": []}
    assert await _card_data(ctx) is None  # no files yet → widget hidden
    await _capture(client, cid, [_src(tmp_path, "p.txt", b"z")])
    out = await _card_data(ctx)
    assert out and out["card_id"] == cid and out["files"][0]["name"] == "p.txt"
    # the sender's local path is a dedup-only detail — it must not reach the frontend
    assert set(out["files"][0]) == {"name", "caption", "size", "sent_at"}
