# Plugin Marketplace

HACS-style discovery, install, update and removal of **third-party** conductor
plugins from the UI (Marketplace tab, next to the Plugin Manager). This doc is for
**plugin authors** and for whoever runs a curated index for their team/org.

## Distribution unit: a git repo

A marketplace plugin is a git repo. Its root carries a manifest, `conductor-plugin.json`:

```json
{
  "name": "my-widget",
  "version": "v1.2.0",
  "description": "One sentence on what this plugin does.",
  "author": "Your Name <you@example.com>",
  "backend": true,
  "frontend": true,
  "min_conductor": "0.9.0"
}
```

| field | required | notes |
|---|---|---|
| `name` | yes | `^[a-z][a-z0-9_-]{1,40}$` — becomes the module/directory name on install |
| `version` | yes | must match a git tag on the repo (`vX.Y.Z`), or the literal `"dev"` |
| `description` | yes | one sentence, shown in Browse/Installed |
| `author` | yes | free text |
| `backend` | yes | `true`/`false` — does this repo carry a `backend/` dir? |
| `frontend` | yes | `true`/`false` — does this repo carry a `frontend/` dir? |
| `min_conductor` | no | advisory only in v1 — not yet enforced by the installer |

At least one of `backend` / `frontend` must be `true`.

### Repo layout

```
my-widget/
  conductor-plugin.json
  backend/            # optional — installs to backend/conductor/plugins/local/my-widget/
    __init__.py        #   must expose PLUGIN = Plugin(...); may also expose ROUTER
    router.py
  frontend/           # optional — installs to frontend/src/plugins/local/my-widget/
    index.tsx          #   must export TAB_LAYOUTS / CARD_LAYOUTS / MENU_LAYOUTS
```

This is **exactly** the shape of a hand-written local plugin (see `docs/plugins.md`) —
the marketplace installer just copies `backend/` and `frontend/` verbatim into the two
`plugins/local/` roots and lets the normal loader pick them up on next restart. There is
nothing marketplace-specific about the plugin code itself; any plugin you'd drop into
`local/` by hand already qualifies, once it's in a repo with the manifest above and a
semver tag.

### Versioning

Tag releases `vX.Y.Z` (plain SemVer, no pre-release/build suffixes in v1 — a tag that
doesn't parse as `vX.Y.Z` is ignored when the marketplace picks "latest"). Installing
resolves to the highest such tag; a repo with no matching tags installs at the default
branch's HEAD and is recorded as version `"dev"` (updates for a `dev` install just track
HEAD, since there's no tag to compare against).

Bump `version` in `conductor-plugin.json` to match the tag you cut — the field is
informational only (not the source of truth, the tag is), but it's what authors and the
Browse tab show as the "current" description of a release.

## The index (for running a curated list)

An index is a static `index.json`:

```json
{
  "plugins": [
    {
      "name": "my-widget",
      "repo": "your-org/my-widget",
      "description": "One sentence on what this plugin does.",
      "tags": ["productivity"]
    }
  ]
}
```

See `index.example.json` in this directory for a fuller example. An entry's `repo`
accepts `owner/name` (expands to `https://github.com/owner/name.git`) or a full git
URL (any host, including a private/self-hosted one your `git` can already clone).

Point conductor at one or more index **sources** via `CONDUCTOR_MARKETPLACE_INDEX_URLS`
(comma-separated). No index configured is a supported state — the Browse tab still
works entirely off custom repos the user pastes in.

### Two kinds of index source

Each comma-separated entry in `CONDUCTOR_MARKETPLACE_INDEX_URLS` is classified purely
by URL scheme (`backend/conductor/plugins/marketplace/service.py::is_http_index_url`):

| entry looks like | treated as | how `index.json` is fetched |
|---|---|---|
| starts with `http://` or `https://` | **HTTP source** | plain GET (e.g. a GitHub `raw.githubusercontent.com` link, or anything else reachable without auth) |
| anything else — `owner/name`, an ssh remote (`git@host:owner/name.git`), a self-hosted URL your `git` already has creds for | **git source** | shallow-cloned with the system `git` into `~/.conductor/marketplace/index/<slug>/`, then `index.json` is read off the checked-out tree |

This is the same classification the brief asked for and the same trust model as
installing a plugin: a git index source works for a **private repo** exactly because
it goes through your own `git`/SSH/credential-helper auth, the same as `git clone`
does for any other private repo on this machine — conductor never handles credentials
itself.

A git source is refreshed with `git pull --ff-only` at most once every 10 minutes
(mirrors the plugin update-check cache); between refreshes, and on a refresh failure
(revoked auth, network blip, deleted repo), the Browse tab keeps serving whatever was
last successfully pulled rather than erroring out. A source that has never been
reachable at all (first clone fails) is skipped with a logged warning — one bad
source never takes down `/api/plugins/marketplace/index` for the others.

### Shipping a default index

A conductor distribution (fork, internal build, whatever) can bake a default straight
into the `marketplace_index_urls` setting's default value in
`backend/conductor/config.py`, so a fresh install already has a curated Browse tab
with zero configuration — `CONDUCTOR_MARKETPLACE_INDEX_URLS` in `.env` only needs
setting to ADD to or REPLACE that default, same as every other comma-separated
`CONDUCTOR_*` setting in this codebase.

### Publishing to someone's index

Open a PR against their `index.json` adding your `{name, repo, description, tags}`
entry — that's the whole submission process; there's no registry service, build step,
or approval bot beyond whatever the index's own repo requires. This is identical
whether their index lives as a plain file (HTTP source) or as its own git repo (git
source) — either way it's just a JSON file under version control somewhere.

## Installing without an index

The Browse tab's "加入自訂 repo" (add custom repo) field takes the same `owner/name`
or full-URL shapes as an index entry — paste a repo and install directly, no index
entry needed. This is how you use the marketplace machinery for a private/internal
plugin that never goes in any public index.

## Trust

Installing a plugin — from an index or pasted directly — clones and runs its code
with conductor's own privileges the moment you hit Apply. There is no sandboxing. Read
what you're installing, the same as you would before running any other script someone
handed you.

## Example plugin

`example-plugin/` in this directory is a minimal, complete skeleton (manifest +
backend + frontend) meant to be copied as a starting point — it is **not** installed
anywhere and never discovered by conductor's own plugin loader (it lives under `docs/`,
outside `backend/conductor/plugins/`).
