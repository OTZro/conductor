// Theme layer. A theme is nothing but a set of CSS custom properties on <html> —
// see src/index.css for the variables and tailwind.config.js for how every colour
// class resolves through them. Switching is one attribute write: no rebuild, no
// re-render, no `dark:` variants sprinkled through components.
//
// FUTURE — user-defined themes: the list below is deliberately DATA, not a union
// type, so a `~/.conductor/themes.json` (the same user-owned-local-file pattern as
// lanes.json / termkeys.json / prompts.json, served by a small backend endpoint)
// can be concatenated onto BUILTIN_THEMES with no change to this logic. A custom
// theme declares `base` (which built-in ramp to start from) plus a `vars` map of
// any variables it wants to override — so a user can retint just the accents, or
// replace the whole neutral ramp, without forking the app.

// xterm.js ITheme subset. The embedded terminal is an IFRAME running ttyd, so it
// canNOT inherit the page's CSS variables — its palette is fixed when ttyd spawns
// (`-t theme=<json>`), which is why changing theme has to RESPAWN the viewer, the
// same constraint font-size already lives with.
export type TerminalTheme = {
  background: string;
  foreground: string;
  cursor: string;
  selectionBackground: string;
  black: string; red: string; green: string; yellow: string;
  blue: string; magenta: string; cyan: string; white: string;
  brightBlack: string; brightRed: string; brightGreen: string; brightYellow: string;
  brightBlue: string; brightMagenta: string; brightCyan: string; brightWhite: string;
};

export type Theme = {
  id: string;
  label: string;
  base: "dark" | "light"; // which built-in ramp (index.css) to sit on
  vars?: Record<string, string>; // optional per-variable overrides, e.g. {"--a-sky": "56 189 248"}
  terminal?: Partial<TerminalTheme>; // xterm palette; merged over the base theme's
  // tmux styles for sessions conductor owns — status bar, borders. Applied on terminal
  // open so one theme choice re-skins the app, the terminal AND its status bar.
  // Omitted = leave tmux alone; {} = explicitly unstyle (restore the user's tmux.conf).
  tmux?: Record<string, string>;
  source?: "builtin" | "file" | "imported";
};

// ANSI colours are re-picked per theme, not just the background: programs inside the
// pane (claude's TUI, tmux) emit palette indices chosen for a DARK backdrop, so simply
// whitening the background leaves mid-tone text nearly invisible. The light palette
// therefore darkens every hue instead of remapping any single app.
const DARK_TERM: TerminalTheme = {
  background: "#09090b", foreground: "#e4e4e7", cursor: "#e4e4e7",
  selectionBackground: "#3f3f46",
  black: "#18181b", red: "#f87171", green: "#34d399", yellow: "#fbbf24",
  blue: "#60a5fa", magenta: "#c084fc", cyan: "#22d3ee", white: "#d4d4d8",
  brightBlack: "#52525b", brightRed: "#fca5a5", brightGreen: "#6ee7b7", brightYellow: "#fcd34d",
  brightBlue: "#93c5fd", brightMagenta: "#d8b4fe", brightCyan: "#67e8f9", brightWhite: "#fafafa",
};

const LIGHT_TERM: TerminalTheme = {
  background: "#fafafa", foreground: "#18181b", cursor: "#18181b",
  selectionBackground: "#d4d4d8",
  black: "#18181b", red: "#c62828", green: "#2e7d32", yellow: "#a16207",
  blue: "#1565c0", magenta: "#7b1fa2", cyan: "#00838f", white: "#52525b",
  brightBlack: "#71717a", brightRed: "#dc2626", brightGreen: "#059669", brightYellow: "#b45309",
  brightBlue: "#2563eb", brightMagenta: "#9333ea", brightCyan: "#0891b2", brightWhite: "#27272a",
};

// The ports below are the upstream palettes, not approximations: ANSI values come from
// mbadolato/iTerm2-Color-Schemes (the set iTerm/oh-my-zsh users already know), and each
// neutral ramp is built from that scheme's OWN published UI tones — Solarized's base03…
// base3, Catppuccin's base/surface/overlay/subtext, Primer's gray scale, gruvbox's
// light0…dark1, nord0…nord6, Dracula's bg/current-line/comment/fg. Only where a scheme
// publishes fewer than 11 neutrals (nord, dracula) are the gaps interpolated between its
// real anchors, never invented.
//
// The ramp runs 950 → 50 as BACKGROUND → strongest text in both bases, which is the
// invariant index.css relies on (light simply reverses which end is bright). So a light
// port sets --z-950 to its page colour and --z-50 to its darkest ink; a dark port does
// the reverse. Accents are pinned to each scheme's own hues rather than the stock
// tailwind ones — that is the point of picking a scheme.
export const BUILTIN_THEMES: Theme[] = [
  { id: "dark", label: "深色", base: "dark", terminal: DARK_TERM, source: "builtin" },
  { id: "light", label: "淺色", base: "light", terminal: LIGHT_TERM, source: "builtin" },
  {
    id: "solarized-light",
    label: "Solarized Light",
    base: "light",
    vars: {
      "--z-950": "253 246 227",
      "--z-900": "245 238 218",
      "--z-800": "238 232 213",
      "--z-700": "221 214 193",
      "--z-600": "195 189 169",
      "--z-500": "147 161 161",
      "--z-400": "131 148 150",
      "--z-300": "101 123 131",
      "--z-200": "88 110 117",
      "--z-100": "7 54 66",
      "--z-50": "0 43 54",
      "--a-blue": "38 139 210",
      "--a-purple": "211 54 130",
      "--a-emerald": "133 153 0",
      "--a-amber": "181 137 0",
      "--a-sky": "38 139 210",
      "--a-red": "220 50 47",
      "--a-cyan": "42 161 152",
      "--a-violet": "108 113 196",
    },
    terminal: {
      "background": "#fdf6e3",
      "foreground": "#657b83",
      "cursor": "#657b83",
      "selectionBackground": "#eee8d5",
      "black": "#073642",
      "red": "#dc322f",
      "green": "#859900",
      "yellow": "#b58900",
      "blue": "#268bd2",
      "magenta": "#d33682",
      "cyan": "#2aa198",
      "white": "#bbb5a2",
      "brightBlack": "#002b36",
      "brightRed": "#cb4b16",
      "brightGreen": "#586e75",
      "brightYellow": "#657b83",
      "brightBlue": "#839496",
      "brightMagenta": "#6c71c4",
      "brightCyan": "#93a1a1",
      "brightWhite": "#fdf6e3",
    },
    tmux: {
      "status-style": "fg=#586e75,bg=#f5eeda",
      "pane-border-style": "fg=#ddd6c1",
      "pane-active-border-style": "fg=#268bd2",
    },
    source: "builtin",
  },
  {
    id: "catppuccin-latte",
    label: "Catppuccin Latte",
    base: "light",
    vars: {
      "--z-950": "239 241 245",
      "--z-900": "230 233 239",
      "--z-800": "220 224 232",
      "--z-700": "204 208 218",
      "--z-600": "188 192 204",
      "--z-500": "156 160 176",
      "--z-400": "140 143 161",
      "--z-300": "124 127 147",
      "--z-200": "108 111 133",
      "--z-100": "92 95 119",
      "--z-50": "76 79 105",
      "--a-blue": "30 102 245",
      "--a-purple": "136 57 239",
      "--a-emerald": "64 160 43",
      "--a-amber": "223 142 29",
      "--a-sky": "4 165 229",
      "--a-red": "210 15 57",
      "--a-cyan": "23 146 153",
      "--a-violet": "114 135 253",
    },
    terminal: {
      "background": "#eff1f5",
      "foreground": "#4c4f69",
      "cursor": "#dc8a78",
      "selectionBackground": "#ccd0da",
      "black": "#bcc0cc",
      "red": "#d20f39",
      "green": "#40a02b",
      "yellow": "#df8e1d",
      "blue": "#1e66f5",
      "magenta": "#ea76cb",
      "cyan": "#179299",
      "white": "#5c5f77",
      "brightBlack": "#acb0be",
      "brightRed": "#e7103f",
      "brightGreen": "#46b02f",
      "brightYellow": "#e49931",
      "brightBlue": "#3878f6",
      "brightMagenta": "#ef95d7",
      "brightCyan": "#19a1a8",
      "brightWhite": "#6c6f85",
    },
    tmux: {
      "status-style": "fg=#6c6f85,bg=#e6e9ef",
      "pane-border-style": "fg=#ccd0da",
      "pane-active-border-style": "fg=#04a5e5",
    },
    source: "builtin",
  },
  {
    id: "github-light",
    label: "GitHub Light",
    base: "light",
    vars: {
      "--z-950": "255 255 255",
      "--z-900": "246 248 250",
      "--z-800": "234 238 242",
      "--z-700": "208 215 222",
      "--z-600": "175 184 193",
      "--z-500": "140 149 159",
      "--z-400": "110 119 129",
      "--z-300": "87 96 106",
      "--z-200": "66 74 83",
      "--z-100": "50 56 63",
      "--z-50": "31 35 40",
      "--a-blue": "9 105 218",
      "--a-purple": "130 80 223",
      "--a-emerald": "26 127 55",
      "--a-amber": "154 103 0",
      "--a-sky": "9 105 218",
      "--a-red": "207 34 46",
      "--a-cyan": "27 124 131",
      "--a-violet": "191 57 137",
    },
    terminal: {
      "background": "#ffffff",
      "foreground": "#1f2328",
      "cursor": "#0969da",
      "selectionBackground": "#d0d7de",
      "black": "#24292f",
      "red": "#cf222e",
      "green": "#116329",
      "yellow": "#4d2d00",
      "blue": "#0969da",
      "magenta": "#8250df",
      "cyan": "#1b7c83",
      "white": "#6e7781",
      "brightBlack": "#57606a",
      "brightRed": "#a40e26",
      "brightGreen": "#1a7f37",
      "brightYellow": "#633c01",
      "brightBlue": "#218bff",
      "brightMagenta": "#a475f9",
      "brightCyan": "#3192aa",
      "brightWhite": "#8c959f",
    },
    tmux: {
      "status-style": "fg=#424a53,bg=#f6f8fa",
      "pane-border-style": "fg=#d0d7de",
      "pane-active-border-style": "fg=#0969da",
    },
    source: "builtin",
  },
  {
    id: "gruvbox-light",
    label: "Gruvbox Light",
    base: "light",
    vars: {
      "--z-950": "249 245 215",
      "--z-900": "251 241 199",
      "--z-800": "242 229 188",
      "--z-700": "235 219 178",
      "--z-600": "213 196 161",
      "--z-500": "189 174 147",
      "--z-400": "168 153 132",
      "--z-300": "124 111 100",
      "--z-200": "102 92 84",
      "--z-100": "80 73 69",
      "--z-50": "60 56 54",
      "--a-blue": "7 102 120",
      "--a-purple": "143 63 113",
      "--a-emerald": "121 116 14",
      "--a-amber": "181 118 20",
      "--a-sky": "7 102 120",
      "--a-red": "157 0 6",
      "--a-cyan": "66 123 88",
      "--a-violet": "175 58 3",
    },
    terminal: {
      "background": "#f9f5d7",
      "foreground": "#3c3836",
      "cursor": "#3c3836",
      "selectionBackground": "#ebdbb2",
      "black": "#3c3836",
      "red": "#cc241d",
      "green": "#98971a",
      "yellow": "#d79921",
      "blue": "#458588",
      "magenta": "#b16286",
      "cyan": "#689d6a",
      "white": "#7c6f64",
      "brightBlack": "#928374",
      "brightRed": "#9d0006",
      "brightGreen": "#79740e",
      "brightYellow": "#b57614",
      "brightBlue": "#076678",
      "brightMagenta": "#8f3f71",
      "brightCyan": "#427b58",
      "brightWhite": "#282828",
    },
    tmux: {
      "status-style": "fg=#665c54,bg=#fbf1c7",
      "pane-border-style": "fg=#ebdbb2",
      "pane-active-border-style": "fg=#076678",
    },
    source: "builtin",
  },
  {
    id: "nord",
    label: "Nord",
    base: "dark",
    vars: {
      "--z-950": "46 52 64",
      "--z-900": "59 66 82",
      "--z-800": "67 76 94",
      "--z-700": "76 86 106",
      "--z-600": "104 113 131",
      "--z-500": "132 140 157",
      "--z-400": "160 168 182",
      "--z-300": "188 195 207",
      "--z-200": "216 222 233",
      "--z-100": "229 233 240",
      "--z-50": "236 239 244",
      "--a-blue": "129 161 193",
      "--a-purple": "180 142 173",
      "--a-emerald": "163 190 140",
      "--a-amber": "235 203 139",
      "--a-sky": "136 192 208",
      "--a-red": "191 97 106",
      "--a-cyan": "143 188 187",
      "--a-violet": "180 142 173",
    },
    terminal: {
      "background": "#2e3440",
      "foreground": "#d8dee9",
      "cursor": "#eceff4",
      "selectionBackground": "#434c5e",
      "black": "#3b4252",
      "red": "#bf616a",
      "green": "#a3be8c",
      "yellow": "#ebcb8b",
      "blue": "#81a1c1",
      "magenta": "#b48ead",
      "cyan": "#88c0d0",
      "white": "#e5e9f0",
      "brightBlack": "#596377",
      "brightRed": "#bf616a",
      "brightGreen": "#a3be8c",
      "brightYellow": "#ebcb8b",
      "brightBlue": "#81a1c1",
      "brightMagenta": "#b48ead",
      "brightCyan": "#8fbcbb",
      "brightWhite": "#eceff4",
    },
    tmux: {
      "status-style": "fg=#d8dee9,bg=#3b4252",
      "pane-border-style": "fg=#4c566a",
      "pane-active-border-style": "fg=#88c0d0",
    },
    source: "builtin",
  },
  {
    id: "dracula",
    label: "Dracula",
    base: "dark",
    vars: {
      "--z-950": "40 42 54",
      "--z-900": "47 49 64",
      "--z-800": "56 58 74",
      "--z-700": "68 71 90",
      "--z-600": "83 92 127",
      "--z-500": "98 114 164",
      "--z-400": "135 148 183",
      "--z-300": "173 181 203",
      "--z-200": "210 215 222",
      "--z-100": "248 248 242",
      "--z-50": "255 255 255",
      "--a-blue": "139 233 253",
      "--a-purple": "255 121 198",
      "--a-emerald": "80 250 123",
      "--a-amber": "255 184 108",
      "--a-sky": "139 233 253",
      "--a-red": "255 85 85",
      "--a-cyan": "139 233 253",
      "--a-violet": "189 147 249",
    },
    terminal: {
      "background": "#282a36",
      "foreground": "#f8f8f2",
      "cursor": "#f8f8f2",
      "selectionBackground": "#44475a",
      "black": "#21222c",
      "red": "#ff5555",
      "green": "#50fa7b",
      "yellow": "#f1fa8c",
      "blue": "#bd93f9",
      "magenta": "#ff79c6",
      "cyan": "#8be9fd",
      "white": "#f8f8f2",
      "brightBlack": "#6272a4",
      "brightRed": "#ff6e6e",
      "brightGreen": "#69ff94",
      "brightYellow": "#ffffa5",
      "brightBlue": "#d6acff",
      "brightMagenta": "#ff92df",
      "brightCyan": "#a4ffff",
      "brightWhite": "#ffffff",
    },
    tmux: {
      "status-style": "fg=#d2d7de,bg=#2f3140",
      "pane-border-style": "fg=#44475a",
      "pane-active-border-style": "fg=#8be9fd",
    },
    source: "builtin",
  },
];

export const BASE_TERM: Record<"dark" | "light", TerminalTheme> = {
  dark: DARK_TERM,
  light: LIGHT_TERM,
};

// ── the live theme set ───────────────────────────────────────────────────────────
// Built-ins + ~/.conductor/themes.json (GET /api/themes) + anything imported in the UI
// (localStorage). Held module-level because the api layer reads the active palette at
// request time, from code that has no React context to reach into.
const IMPORTED_KEY = "conductor.importedThemes";
let _all: Theme[] = BUILTIN_THEMES;

export function allThemes(): Theme[] {
  return _all;
}

export function setThemes(list: Theme[]): void {
  _all = list;
}

export function loadImported(): Theme[] {
  try {
    const raw = JSON.parse(localStorage.getItem(IMPORTED_KEY) || "[]");
    return Array.isArray(raw) ? raw.map((t) => ({ ...t, source: "imported" as const })) : [];
  } catch {
    return [];
  }
}

export function saveImported(list: Theme[]): void {
  try {
    localStorage.setItem(IMPORTED_KEY, JSON.stringify(list));
  } catch {
    /* quota / private mode — non-fatal */
  }
}

// Accepts one theme or a list, so a user can paste either an export from here or a
// whole themes.json. Validation mirrors api/themes.py: the same fields, the same
// tolerance — a bad entry is dropped, never thrown.
export function parseImport(text: string): { themes: Theme[]; error: string | null } {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (e) {
    return { themes: [], error: e instanceof Error ? e.message : "JSON 解析失敗" };
  }
  const list = Array.isArray(raw) ? raw : [raw];
  const out: Theme[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const t = item as Record<string, unknown>;
    if (typeof t.id !== "string" || !t.id.trim()) continue;
    if (typeof t.label !== "string" || !t.label.trim()) continue;
    out.push({
      id: t.id.trim(),
      label: t.label.trim(),
      base: t.base === "light" ? "light" : "dark",
      vars: (t.vars as Record<string, string>) || undefined,
      terminal: (t.terminal as Partial<TerminalTheme>) || undefined,
      tmux: (t.tmux as Record<string, string>) || undefined,
      source: "imported",
    });
  }
  return {
    themes: out,
    error: out.length ? null : "找不到可用的主題（需要 id 與 label）",
  };
}

export function exportTheme(theme: Theme): string {
  const { id, label, base, vars, tmux } = theme;
  return JSON.stringify(
    { id, label, base, vars, terminal: terminalPalette(theme), tmux },
    null,
    2,
  );
}

/** The full xterm palette for a theme — its own entries over its base's. */
export function terminalPalette(theme: Theme): TerminalTheme {
  return { ...BASE_TERM[theme.base], ...(theme.terminal || {}) };
}

// Terminals live in iframes that can't react to a CSS variable change, so switching
// theme fires this and every mounted TerminalView respawns its ttyd through the
// reopen path it already has for a dead viewer.
export const THEME_EVENT = "conductor:theme";

// Read by the api layer at call time so EVERY terminal-opening path picks up the
// current palette without threading a prop through four unrelated call sites.
function activeTheme(): Theme | null {
  try {
    const id = JSON.parse(localStorage.getItem("conductor.theme") || '""');
    return resolveTheme(id, _all);
  } catch {
    return null;
  }
}

function termFollows(): boolean {
  try {
    // opt-out: some people want a dark terminal on a light page, since a TUI picks its
    // colours for a dark backdrop
    return JSON.parse(localStorage.getItem("conductor.termFollowsTheme") || "true");
  } catch {
    return true;
  }
}

export function currentTerminalTheme(): TerminalTheme | undefined {
  const t = activeTheme();
  return t && termFollows() ? terminalPalette(t) : undefined;
}

/** The tmux styles to push with a terminal open. `{}` explicitly unstyles (restores the
 *  user's tmux.conf) so a theme without tmux colours doesn't leave the last one stuck;
 *  `undefined` means "don't touch tmux at all" (terminal-follows-theme is off). */
export function currentTmuxStyle(): Record<string, string> | undefined {
  const t = activeTheme();
  if (!t || !termFollows()) return undefined;
  return t.tmux ?? {};
}

export const DEFAULT_THEME_ID = "dark";

// Track what we set inline so switching themes REMOVES the previous theme's
// overrides — without this, custom vars would accumulate and a later theme would
// silently inherit an earlier one's tint.
let appliedVars: string[] = [];

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme.base);
  for (const name of appliedVars) root.style.removeProperty(name);
  appliedVars = [];
  for (const [name, value] of Object.entries(theme.vars ?? {})) {
    root.style.setProperty(name, value);
    appliedVars.push(name);
  }
  // keep the mobile browser chrome in step with the page background
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const bg = getComputedStyle(root).getPropertyValue("--z-950").trim();
    if (bg) meta.setAttribute("content", `rgb(${bg})`);
  }
}

export function resolveTheme(id: string, themes: Theme[] = BUILTIN_THEMES): Theme {
  return themes.find((t) => t.id === id) ?? themes[0];
}
