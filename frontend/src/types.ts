// lanes are registry-driven (built-ins + ~/.conductor/lanes.json + plugin stages),
// so the key set is open — the four built-ins are just the guaranteed members.
export type Lane = string;
export type Ball = "human" | "ai" | "none";

// one entry of GET /api/board/lanes — the board renders its columns from these
export interface BoardLane {
  key: string;
  title: string;
  tone: "human" | "ai" | "done" | "backlog";
  order: number;
  rank: number;
  within: string | null; // ball space a custom stage subdivides ("human"); null = terminal
  aging: boolean; // show the waiting-hours aging accent on cards here
  collapsible: boolean;
  builtin: boolean;
}

export interface CardLink {
  kind: string; // jira | pr | slack | url
  ref: string;
  url: string;
  title?: string | null;
  auto: boolean;
}

export interface LocalState {
  read: boolean;
  dismissed: boolean;
  pinned: boolean; // keep this job prominent — sorts to the top of its lane, any origin
  snoozed_until?: string | null;
  manual_stage?: string | null; // user's manual board-stage override (human-space stage key)
  picked_option?: string | null;
  picked_at?: string | null;
  note?: string | null;
  workdir?: string | null;
  claude_session_id?: string | null;
  host?: string | null; // machine the remembered claude session lives on (null = local)
}

export interface Card {
  id: string;
  origin: string; // jira | github | slack | manual
  external_id: string;
  title: string;
  summary: string;
  url?: string | null;
  ball: Ball;
  lane: Lane;
  driver: string; // plugin-assigned owner | none
  agent_state?: string | null;
  cached: Record<string, any>;
  created_at?: string | null;
  updated_at?: string | null;
  last_seen_at?: string | null;
  local: LocalState;
  links: CardLink[];
}

export interface AwaitingInput {
  question: string;
  options: string[];
  raw: string;
  comment_id?: string | null;
}

export interface TerminalSession {
  id: string;
  card_id: string;
  kind: string;
  url?: string | null;
  status: string;
  cwd?: string | null;
  claude_session_id?: string | null;
  tmux_session?: string | null; // the tmux this terminal shows — named by the backend
  host?: string | null; // null = local (base)
}

export interface SessionStatus {
  state: "running" | "waiting";
  bg?: string | null; // background task label, e.g. "2 shells running"
  model?: string | null; // e.g. "Opus 4.8"
  ctx_pct?: number | null; // context window used %
  task?: string | null; // last prompt (statusline), fallback when no summary
  summary?: string | null; // claude's own task summary (the terminal title, like Warp shows)
  is_claude?: boolean; // pane really is claude (gates status on card-less 'other' sessions)
}

export interface TmuxSession {
  name: string;
  kind: string; // conductor | other
  attached: boolean;
  activity?: number; // tmux #{session_activity} epoch — for "recent" sort
  host?: string | null; // null = local (base); else remote host name (roam)
  socket?: string | null; // null/absent = default tmux socket; else a named `-L` socket (e.g. "agents")
  readonly?: boolean; // named-socket session attaches read-only (CONDUCTOR_EXTRA_TMUX_READONLY) — badge/UX hint
  card_id?: string | null;
  external_id?: string | null;
  title?: string | null;
  origin?: string | null;
  status?: SessionStatus | null; // live pane status (only when fetched with ?status=1)
}

export interface HostInfo {
  name: string; // base / roam
  local: boolean;
  online: boolean;
}
