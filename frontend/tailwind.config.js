/** @type {import('tailwindcss').Config} */
// Design tokens. Components use SEMANTIC names — never raw palette classes — so
// "a new feature invents its own color" is structurally impossible. See
// docs/IMPROVEMENT_PLAN.md §3A.
//
// THEMING: every colour below resolves through a CSS custom property defined in
// src/index.css (:root = dark, :root[data-theme="light"] = light), so switching
// themes is one attribute on <html> — no rebuild, no re-render, no per-component
// dark: variants. The `rgb(var(--x) / <alpha-value>)` form is REQUIRED, not
// stylistic: the codebase leans on opacity modifiers (bg-state-ai/20,
// bg-zinc-900/40) and a bare var() would break every one of them.
//
// `zinc` is overridden WHOLESALE on purpose. ~600 raw zinc-* usages predate the
// token layer; pointing the ramp itself at the vars themes all of them at once
// (light mode reverses the ramp — see index.css), which is why light mode needed
// no per-file colour migration.
const z = (step) => `rgb(var(--z-${step}) / <alpha-value>)`;
const accent = (name) => `rgb(var(--a-${name}) / <alpha-value>)`;

// The RAW hues get the zinc treatment on their LIGHT END only. Semantic names are still
// the rule for new code, but ~130 pre-token `text-emerald-300` style usages exist — six
// files' worth inside gitignored local plugins, which no migration here could reach — and
// on a light page a 200/300 shade is invisible. Redirecting shades 50-400 through vars
// (mirrored in light mode, see index.css) fixes every one of them at once.
//
// 500 and up keep tailwind's own values deliberately: those carry the SOLID fills
// (bg-sky-600 with white text), and flipping them would erase the button's own label.
const HUES = ["emerald", "amber", "sky", "red", "violet", "cyan", "blue", "purple", "teal", "indigo"];
const hueScale = (name) =>
  Object.fromEntries(
    [50, 100, 200, 300, 400].map((s) => [s, `rgb(var(--h-${name}-${s}) / <alpha-value>)`]),
  );
const themedHues = Object.fromEntries(HUES.map((h) => [h, hueScale(h)]));

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ...themedHues,
        zinc: {
          50: z(50), 100: z(100), 200: z(200), 300: z(300), 400: z(400),
          500: z(500), 600: z(600), 700: z(700), 800: z(800), 900: z(900),
          950: z(950),
        },
        // surfaces (three steps: page / raised card / interactive control)
        surface: {
          base: z(950),
          raised: z(900),
          hover: z(800),
        },
        // card origins
        src: {
          jira: accent("blue"),
          pr: accent("purple"),
          slack: accent("emerald"),
          manual: z(400),
        },
        // who holds the ball
        state: {
          human: accent("amber"),
          ai: accent("sky"),
          done: z(600),
        },
        // severity / freshness
        sev: {
          urgent: accent("red"),
          warn: accent("amber"),
          ok: accent("emerald"),
        },
        // machine a session runs on
        host: {
          local: accent("cyan"),
          remote: accent("violet"),
        },
      },
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      // type scale: caption 11 / body-s 12 / body 14 / title 16 — nothing below 11px
      fontSize: {
        caption: ["11px", "14px"],
        "body-s": ["12px", "16px"],
        body: ["14px", "20px"],
        title: ["16px", "22px"],
      },
      borderRadius: {
        chip: "6px",
        card: "8px",
        modal: "12px",
      },
      transitionDuration: {
        fast: "150ms",
        panel: "200ms",
      },
    },
  },
  plugins: [],
};
