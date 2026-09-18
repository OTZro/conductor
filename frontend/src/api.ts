import type {
  AuthenticationResponseJSON,
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
  RegistrationResponseJSON,
} from "@simplewebauthn/browser";
import { currentTerminalTheme, currentTmuxStyle, type Theme } from "./theme";
import type { AwaitingInput, BoardLane, Card, HostInfo, TerminalSession, TmuxSession } from "./types";

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    if (r.status === 401) {
      // session missing/expired — App listens and swaps in the login screen
      window.dispatchEvent(new CustomEvent("conductor:unauthorized"));
    }
    let detail = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.detail)
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

// the board's lane registry — fetched once per page load (memoized promise): the set
// only changes when lanes.json / plugins change, which comes with a reload anyway
let _lanes: Promise<BoardLane[]> | null = null;
export const getBoardLanes = () => (_lanes ??= fetch("/api/board/lanes").then(j<BoardLane[]>));

// jira status transition from a chip hover — the picker list + the move itself
export const getJiraStatuses = () =>
  fetch("/api/jira/statuses").then(j<{ statuses: string[] }>).then((r) => r.statuses);
export const transitionJira = (key: string, status: string) =>
  fetch(`/api/jira/${key}/transition`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ status }),
  }).then(j<{ ok: boolean }>);

export const listCards = (includeDismissed = false) =>
  fetch(`/api/cards?include_dismissed=${includeDismissed}`).then(j<Card[]>);

export const getCard = (id: string) => fetch(`/api/cards/${id}`).then(j<Card>);

export const patchState = (id: string, body: Record<string, unknown>) =>
  fetch(`/api/cards/${id}/state`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }).then(j<Card>);

// M2
export const getAwaiting = (id: string) =>
  fetch(`/api/cards/${id}/awaiting`).then(j<AwaitingInput | null>);

export const getBody = (id: string) =>
  fetch(`/api/cards/${id}/body`).then(j<{ kind: string; content: string }>);

export type TSlackMsg = { user: string; text: string; ts: string; is_me: boolean };
export const getSlackThread = (id: string) =>
  fetch(`/api/cards/${id}/slack-thread`).then(j<TSlackMsg[]>);

export const resumeCard = (id: string, answer: string) =>
  fetch(`/api/cards/${id}/resume`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ answer }),
  }).then(j<{ ok: boolean }>);

export const prAction = (id: string, action: "approve" | "merge") =>
  fetch(`/api/cards/${id}/pr/${action}`, { method: "POST" }).then(
    j<{ ok: boolean; detail?: string }>,
  );

// on-demand PR refresh (bypasses the poll throttle) — call when a detail opens
export const refreshCardPrs = (id: string) =>
  fetch(`/api/cards/${id}/refresh-prs`, { method: "POST" }).then(j<{ ok: boolean }>);

// group a card UNDER a representative card (primaryId), or ungroup (null). A grouped
// member is hidden from the board and listed under its representative.
export const groupCard = (id: string, primaryId: string | null) =>
  fetch(`/api/cards/${id}/group`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ primary_id: primaryId || undefined }),
  }).then(j<{ ok: boolean; group: string | null }>);

// M3
export const openTerminal = (
  id: string,
  kind: "attach" | "own" | "resume",
  opts?: {
    cwd?: string;
    claude_session_id?: string;
    font_size?: number;
    host?: string;
    initial_prompt?: string;
    // resume-attach a RUNNING session only; the backend fails instead of reviving a
    // killed one (used by card-open auto-load, not the explicit Resume button).
    attach_only?: boolean;
    // named launch profile (own sessions only): supplies host/cwd defaults + injects
    // its env into the launched claude. See /api/launch-profiles.
    profile?: string;
  },
) =>
  fetch(`/api/cards/${id}/terminal`, {
    method: "POST",
    headers: JSON_HEADERS,
    // term_theme is attached HERE rather than passed by each caller: the palette is a
    // global display preference, and every one of the four terminal-opening call sites
    // would otherwise have to thread it through unrelated props.
    body: JSON.stringify({
      kind,
      term_theme: currentTerminalTheme(),
      tmux_style: currentTmuxStyle(),
      ...opts,
    }),
  }).then(j<TerminalSession>);

// handover: inject the write-prompt into the active claude so it writes a handover doc
export const handoverPrep = (id: string, tmux: string, host?: string | null) =>
  fetch(`/api/cards/${id}/handover/prep`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ tmux, host: host || undefined }),
  }).then(j<{ ok: boolean }>);

// handover: once the doc exists on from_host, copy it → to_host; `ready` is false while
// claude is still writing it (poll), the pickup prompt seeds a session on the target.
export const handoverTransfer = (id: string, fromHost: string | null, toHost: string | null) =>
  fetch(`/api/cards/${id}/handover/transfer`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ from_host: fromHost || undefined, to_host: toHost || undefined }),
  }).then(j<{ ok: boolean; ready: boolean; to_host?: string | null; pickup_prompt?: string }>);

export const listHosts = () => fetch("/api/hosts").then(j<HostInfo[]>);

// the dir a NEW session defaults to for this card (CONDUCTOR_DEFAULT_WORKSPACE_ROOT)
// — pre-fills the New-session field so it reflects real config.
export const getDefaultCwd = (id: string) =>
  fetch(`/api/cards/${id}/default-cwd`).then(j<{ cwd: string }>);

// directory existence + completions for the New-session working-dir field, checked on
// the chosen run-on host (base local / roam over ssh).
export const getDirs = (path: string, host?: string) =>
  fetch(
    `/api/dirs?path=${encodeURIComponent(path)}${host ? `&host=${encodeURIComponent(host)}` : ""}`,
  ).then(j<{ valid: boolean; dirs: string[] }>);

// named launch presets from ~/.conductor/profiles.json for the New-session picker.
export type TLaunchProfile = {
  name: string;
  host: string | null;
  cwd: string | null;
  env: Record<string, string>;
};
export const getLaunchProfiles = () =>
  fetch("/api/launch-profiles").then(j<TLaunchProfile[]>);

// ── plugins ────────────────────────────────────────────────────────────────────────
// A plugin declares contributions (a tab / a card-widget) via its manifest; the FE
// renders them through named layouts. Data rides generic endpoints keyed by plugin id.
export type TPluginManifest = {
  id: string;
  label: string;
  icon: string;
  order: number;
  // Per-RENDER-SLOT effective order (user overrides from the manager panel's
  // "版面排序" section applied server-side, defaulting to `order`). A plugin's nav
  // position, card-widget position, and menu-bar position are independent sequences,
  // so each FE slot consumer sorts by its own key here rather than by `order`.
  orders: { tab: number; card_widget: number; menu_bar: number };
  tab: { layout: string; self_contained: boolean; config: Record<string, unknown> } | null;
  // slot: body | above-body | below-body | actions | menu
  card_widget: { title: string; layout: string; slot?: string } | null;
  menu_bar: { layout: string; config: Record<string, unknown> } | null;
};
// manifest is fixed for a session (plugins don't change at runtime) — fetch once, but
// never cache a failure (a transient error during a backend restart must not stick).
let _pluginManifest: Promise<TPluginManifest[]> | null = null;
export const getPluginManifest = () => {
  if (!_pluginManifest) {
    _pluginManifest = fetch("/api/plugins/manifest").then(j<TPluginManifest[]>);
    _pluginManifest.catch(() => {
      _pluginManifest = null;
    });
  }
  return _pluginManifest;
};
export const getPluginTab = (id: string) => fetch(`/api/plugins/${id}/tab`).then(j<unknown>);
export const getPluginCard = (id: string, ctx: unknown, signal?: AbortSignal) =>
  fetch(`/api/plugins/${id}/card`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(ctx),
    signal,
  }).then(j<{ data: unknown }>);

// ── mobile terminal soft-keys (optional ~/.conductor/termkeys.json; empty → FE defaults) ──
export type TTermKey = {
  label: string;
  title?: string;
  seq?: string; // present → inject raw bytes (tmux-style); else a synthetic keydown
  key?: string;
  code?: string;
  keyCode?: number;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  metaKey?: boolean;
};
let _termKeys: Promise<TTermKey[]> | null = null;
export const getTermKeys = () => {
  if (!_termKeys) {
    _termKeys = fetch("/api/termkeys").then(j<TTermKey[]>);
    _termKeys.catch(() => {
      _termKeys = null;
    });
  }
  return _termKeys;
};

let _hotkeys: Promise<Record<string, string>> | null = null;
export const getHotkeys = () => {
  if (!_hotkeys) {
    _hotkeys = fetch("/api/hotkeys").then(j<Record<string, string>>);
    _hotkeys.catch(() => {
      _hotkeys = null;
    });
  }
  return _hotkeys;
};

// `expired`: resets_at has passed but no Claude session has reported the new window
// yet, so used_percentage is null instead of the stale pre-reset number.
export type UsageWindow = {
  used_percentage: number | null;
  resets_at: number | null;
  expired?: boolean;
} | null;
export type Usage = { session: UsageWindow; weekly: UsageWindow; captured_at: number | null };
export const getUsage = () => fetch("/api/usage").then(j<Usage>);

// user-defined colour themes from ~/.conductor/themes.json (see api/themes.py). Empty
// when the file is absent — the built-in dark/light pair always exists client-side.
export const getThemes = () => fetch("/api/themes").then(j<Theme[]>);

export const stopTerminal = (sessionId: string) =>
  fetch(`/api/terminals/${sessionId}`, { method: "DELETE" }).then(j<{ ok: boolean }>);

export const killTerminal = (sessionId: string) =>
  fetch(`/api/terminals/${sessionId}/kill`, { method: "POST" }).then(
    j<{ ok: boolean; killed: boolean }>,
  );

// `lines` batches a whole gesture into one request; the reply says where tmux
// actually landed (it clamps at the oldest line), so the caller needn't count.
export const scrollTerminal = (sessionId: string, dir: "up" | "down" | "exit", lines = 1) =>
  fetch(`/api/terminals/${sessionId}/scroll`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ dir, lines }),
  }).then(j<{ ok: boolean; in_mode: boolean; scroll: number; alt?: boolean }>);

export const captureTerminal = (sessionId: string) =>
  fetch(`/api/terminals/${sessionId}/capture`).then(j<{ text: string }>);

// paste a clipboard image into the session's host + inject its path (for Claude).
// dataUrl is a base64 data-URL from the browser clipboard (viewer's machine).
export const pasteTerminalImage = (sessionId: string, dataUrl: string, mime: string) =>
  fetch(`/api/terminals/${sessionId}/paste-image`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ data: dataUrl, mime }),
  }).then(j<{ ok: boolean; path: string }>);

export const listTmux = (status = false) =>
  fetch(`/api/tmux${status ? "?status=1" : ""}`).then(j<TmuxSession[]>);

export type TConversation = {
  claude_session_id: string;
  host: string | null;
  live: boolean;
  last_used: string | null;
};
export const listConversations = (cardId: string) =>
  fetch(`/api/cards/${cardId}/conversations`).then(j<TConversation[]>);

export const openTmux = (
  name: string,
  font_size?: number,
  host?: string | null,
  socket?: string | null,
) =>
  fetch("/api/tmux/open", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({
      name, font_size, host: host || undefined, socket: socket || undefined,
      term_theme: currentTerminalTheme(),
      tmux_style: currentTmuxStyle(),
    }),
  }).then(j<{ id: string; url: string; tmux_session: string; host?: string | null }>);

// create a fresh non-conductor shell tmux session on a host and open it
export const newTmux = (host?: string | null) =>
  fetch("/api/tmux/new", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ host: host || undefined }),
  }).then(j<{ id: string; url: string; tmux_session: string; host?: string | null }>);

// bind a card-less tmux session (a hand-run claude) onto a card. card_id omitted →
// a fresh manual card is created from the session's own claude summary.
export const adoptSession = (name: string, host?: string | null, card_id?: string) =>
  fetch("/api/tmux/adopt", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ name, host: host || undefined, card_id: card_id || undefined }),
  }).then(j<{ ok: boolean; card_id: string; tmux_session: string; host?: string | null }>);

// M5
export const addLink = (
  id: string,
  body: { kind: string; ref: string; url: string; title?: string },
) =>
  fetch(`/api/cards/${id}/links`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }).then(j<Card>);

export type TDashboard = { id: string; label: string; icon: string; origins?: string[] };
export const listDashboards = () => fetch("/api/dashboards").then(j<TDashboard[]>);

export const createManual = (body: { title: string; summary?: string; url?: string; board?: string }) =>
  fetch(`/api/cards`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }).then(j<Card>);

export const setDone = (id: string, done: boolean) =>
  fetch(`/api/cards/${id}/done`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ done }),
  }).then(j<Card>);

// add/remove the hold label on a jira card's ticket
export const setHold = (id: string, hold: boolean) =>
  fetch(`/api/cards/${id}/hold`, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ hold }),
  }).then(j<Card>);

export const listPins = () => fetch("/api/pins").then(j<string[]>);

export const addPin = (key: string) =>
  fetch("/api/pins", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ key }) }).then(
    j<{ ok: boolean; key: string }>,
  );

export const removePin = (key: string) =>
  fetch(`/api/pins/${encodeURIComponent(key)}`, { method: "DELETE" }).then(j<{ ok: boolean }>);

// Auth (Google login + WebAuthn passkeys)
export type AuthUser = { email: string; name: string; picture: string };

export type AuthMe = {
  auth_enabled: boolean;
  authenticated: boolean;
  user: AuthUser | null;
  google_client_id: string | null;
  passkey_count: number;
  setup_available: boolean;
};

export type Passkey = {
  id: string;
  nickname: string;
  rp_id: string;
  created_at: string | null;
  last_used_at: string | null;
};

// Never throws: an old backend without /api/auth (404 / non-JSON) means auth-off.
export const authMe = async (): Promise<AuthMe> => {
  try {
    const r = await fetch("/api/auth/me");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()) as AuthMe;
  } catch {
    return {
      auth_enabled: false,
      authenticated: false,
      user: null,
      google_client_id: null,
      passkey_count: 0,
      setup_available: false,
    };
  }
};

export const googleLogin = (credential: string) =>
  fetch("/api/auth/google", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ credential }),
  }).then(j<{ user: AuthUser }>);

export const waRegisterOptions = (body: { setup_token?: string; email?: string }) =>
  fetch("/api/auth/webauthn/register/options", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }).then(j<{ state_id: string; options: PublicKeyCredentialCreationOptionsJSON }>);

export const waRegisterVerify = (body: {
  state_id: string;
  credential: RegistrationResponseJSON;
  setup_token?: string;
  nickname?: string;
}) =>
  fetch("/api/auth/webauthn/register/verify", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }).then(j<{ ok: boolean; user: AuthUser }>);

export const waLoginOptions = () =>
  fetch("/api/auth/webauthn/login/options", { method: "POST" }).then(
    j<{ state_id: string; options: PublicKeyCredentialRequestOptionsJSON }>,
  );

export const waLoginVerify = (body: { state_id: string; credential: AuthenticationResponseJSON }) =>
  fetch("/api/auth/webauthn/login/verify", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }).then(j<{ user: AuthUser }>);

export const logout = () => fetch("/api/auth/logout", { method: "POST" }).then(j<{ ok: boolean }>);

export const listPasskeys = () => fetch("/api/auth/passkeys").then(j<Passkey[]>);

export const deletePasskey = (id: string) =>
  fetch(`/api/auth/passkeys/${encodeURIComponent(id)}`, { method: "DELETE" }).then(
    j<{ ok: boolean }>,
  );
