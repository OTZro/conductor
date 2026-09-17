"""Plugin-owned file store — where a card's SendUserFile'd files live on disk, plus a
per-card metadata sidecar. Self-contained so the files plugin owns its storage end to
end: core keeps no ``files_dir`` setting and no ``cached.files`` shape. Separate module
(not ``__init__``/``router``) so both the router and the card provider import it without
the ``__init__ -> router`` cycle.

Concurrency: ``record_file`` is a plain SYNC function with no await points, so under the
single-process event loop its read-modify-write of the sidecar is atomic — two racing
``/capture`` requests can't clobber each other's entry (this is what the old per-card DB
lock bought; the sidecar gets it for free)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# One dir per card under the plugin's own root. A module attr (not a core setting) so the
# plugin owns its location; tests monkeypatch this to a tmp_path.
FILES_DIR = Path.home() / ".conductor" / "files"
MAX_FILE = 25 * 1024 * 1024  # a SendUserFile is a report/export, not a bulk transfer


def card_dir(card_id: str) -> Path:
    """Where a card's uploaded blobs live — holds ONLY sent files, so a file may take any
    name (incl. the old sidecar name) without collision."""
    return FILES_DIR / card_id


def _meta_path(card_id: str) -> Path:
    """The metadata sidecar — a SIBLING of the card dir (``<id>.meta.json``), not a file
    inside it. That keeps it out of the upload namespace (a sent file named like the
    sidecar can't clobber it) and unreachable via the ``/{card_id}/{name}`` download route
    (which only serves paths under ``card_dir``)."""
    return FILES_DIR / f"{card_id}.meta.json"


def safe_name(name: str) -> str:
    """A filesystem-safe basename — strip any directory parts + reject traversal, so a
    hostile path (the file name comes from the session's machine) can't escape the card
    dir."""
    base = Path(name).name.strip() or "file"  # drops any dir components incl. .. and /
    return base[:200]


def list_files(card_id: str) -> list[dict]:
    """The recorded metadata for a card's files (newest kept on re-send), or [] if none
    — a corrupt/absent sidecar reads as empty rather than raising into the provider."""
    meta = _meta_path(card_id)
    if not meta.is_file():
        return []
    try:
        data = json.loads(meta.read_text())
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def _unique_name(display: str, source: str, taken: set[str]) -> str:
    """A download/on-disk name that is unique among ``taken``. Two DIFFERENT source files
    can normalize to the same ``display`` (same basename from different dirs, or long names
    truncated to 200 chars) — without this the second would overwrite the first on disk and
    evict its metadata. The collider gets a short digest of its source path folded into the
    stem, so both survive; the first keeps the clean name. The digest candidate is itself
    checked against ``taken`` and a counter appended until it is actually free — an 8-hex
    digest can theoretically collide, or a file could already be named like the suffix, and
    neither may silently overwrite."""
    if display not in taken:
        return display
    h = hashlib.sha256(source.encode()).hexdigest()[:8]
    stem, dot, ext = display.partition(".")
    candidate = f"{stem}-{h}{dot}{ext}"
    counter = 2
    while candidate in taken:
        candidate = f"{stem}-{h}-{counter}{dot}{ext}"
        counter += 1
    return candidate


def record_file(card_id: str, data: bytes, *, source: str, caption: str, sent_at: str) -> dict:
    """Write ``data`` under the card dir and append/replace its sidecar entry. ``source`` is
    the file's full original path on the session's machine — the dedup IDENTITY: re-sending
    the same source replaces its entry, while two different sources that normalize to the
    same name both survive (the collider gets a disambiguating suffix). ``source`` is kept
    for dedup only and is projected out before reaching the frontend (see the provider).

    Sync + await-free so the sidecar read-modify-write is atomic under the single-process
    event loop (see module docstring)."""
    files = [f for f in list_files(card_id) if f.get("source") != source]  # re-send replaces
    name = _unique_name(safe_name(source.rsplit("/", 1)[-1]), source, {f["name"] for f in files})
    d = card_dir(card_id)
    d.mkdir(parents=True, exist_ok=True)  # also ensures FILES_DIR for the sidecar sibling
    (d / name).write_bytes(data)
    entry = {
        "name": name, "caption": caption or None, "size": len(data),
        "sent_at": sent_at, "source": source,
    }
    files.append(entry)
    _meta_path(card_id).write_text(json.dumps(files))
    return entry
