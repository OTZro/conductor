// Minimal Markdown → React renderer, deliberately dependency-free: its one producer
// is our own ADF→Markdown converter (Jira descriptions, see sources/jira.py), so it
// covers the blocks that emits and nothing more — callers must know their body is
// markdown before reaching for this. Everything comes out as React elements — never
// dangerouslySetInnerHTML — so ticket text can't inject markup, and hrefs are
// scheme-checked before they become links.
import { type ReactNode, useMemo } from "react";

// 3+ delimiters, any info string: the ADF converter lengthens the fence to outrun
// backticks in the body, and a closer must be at least as long as its opener.
const FENCE = /^\s*(`{3,}|~{3,})\s*(\S*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const HR = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^\s*>\s?(.*)$/;
const LI = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const TABLE_SEP = /^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$/;
const SAFE_HREF = /^(https?:|mailto:|\/)/i;
const ESCAPABLE = /[\\`*_~[\]()#>+\-.!|]/;

type Block =
  | { k: "h"; level: number; text: string }
  | { k: "p"; text: string }
  | { k: "code"; text: string }
  | { k: "hr" }
  | { k: "quote"; blocks: Block[] }
  | { k: "list"; ordered: boolean; start: number; items: Block[][] }
  | { k: "table"; rows: string[][] };

const indentOf = (line: string) => line.length - line.trimStart().length;

const isTable = (lines: string[], i: number) =>
  lines[i].includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]);

const startsBlock = (lines: string[], i: number) =>
  FENCE.test(lines[i]) ||
  HR.test(lines[i]) ||
  HEADING.test(lines[i]) ||
  QUOTE.test(lines[i]) ||
  LI.test(lines[i]) ||
  isTable(lines, i);

function splitRow(line: string): string[] {
  const s = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const out: string[] = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "\\" && s[i + 1] === "|") {
      cur += "|";
      i++;
    } else if (s[i] === "|") {
      out.push(cur.trim());
      cur = "";
    } else {
      cur += s[i];
    }
  }
  out.push(cur.trim());
  return out;
}

function parseList(lines: string[], start: number): [Block, number] {
  const first = LI.exec(lines[start])!;
  const base = first[1].length;
  const ordered = /^\d/.test(first[2]);
  const contIndent = base + first[2].length + 1; // where this item's own text begins
  const items: string[][] = [];
  let cur: string[] = [];
  let i = start;
  while (i < lines.length) {
    if (!lines[i].trim()) {
      // a blank line only keeps the list alive if indented/markered content follows
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      if (j >= lines.length || (indentOf(lines[j]) <= base && !LI.test(lines[j]))) break;
      cur.push("");
      i = j;
      continue;
    }
    const marker = LI.exec(lines[i]);
    const ind = indentOf(lines[i]);
    if (marker && ind <= base) {
      if (/^\d/.test(marker[2]) !== ordered) break; // a different marker type = a new list
      cur = [marker[3]];
      items.push(cur);
    } else if (ind > base) {
      cur.push(lines[i].slice(Math.min(ind, contIndent))); // dedent → nested lists nest
    } else {
      break; // dedented plain text: the list is over
    }
    i++;
  }
  const startNum = ordered ? parseInt(first[2], 10) || 1 : 1;
  return [{ k: "list", ordered, start: startNum, items: items.map(parse) }, i];
}

function parse(lines: string[]): Block[] {
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) {
      i++;
      continue;
    }
    const fence = FENCE.exec(lines[i]);
    if (fence) {
      const body: string[] = [];
      const close = new RegExp(`^\\s*${fence[1][0]}{${fence[1].length},}\\s*$`);
      i++;
      while (i < lines.length && !close.test(lines[i])) body.push(lines[i++]);
      i++; // the closing fence (or past EOF for an unterminated block)
      out.push({ k: "code", text: body.join("\n") });
      continue;
    }
    if (HR.test(lines[i])) {
      out.push({ k: "hr" });
      i++;
      continue;
    }
    const h = HEADING.exec(lines[i]);
    if (h) {
      out.push({ k: "h", level: h[1].length, text: h[2] });
      i++;
      continue;
    }
    if (QUOTE.test(lines[i])) {
      const body: string[] = [];
      while (i < lines.length) {
        const m = QUOTE.exec(lines[i]);
        if (!m) break;
        body.push(m[1]);
        i++;
      }
      out.push({ k: "quote", blocks: parse(body) });
      continue;
    }
    if (isTable(lines, i)) {
      const rows = [splitRow(lines[i])];
      i += 2; // header + the --- separator
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) rows.push(splitRow(lines[i++]));
      out.push({ k: "table", rows });
      continue;
    }
    if (LI.test(lines[i])) {
      const [block, next] = parseList(lines, i);
      out.push(block);
      i = next;
      continue;
    }
    const buf: string[] = [lines[i++]];
    while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) buf.push(lines[i++]);
    out.push({ k: "p", text: buf.join("\n") });
  }
  return out;
}

// ── inline ───────────────────────────────────────────────────────────────────
const DELIMS: { d: string; tag: "strong" | "em" | "del" }[] = [
  { d: "**", tag: "strong" },
  { d: "__", tag: "strong" },
  { d: "~~", tag: "del" },
  { d: "*", tag: "em" },
  { d: "_", tag: "em" },
];

// Every delimiter scan below has to skip an ESCAPED delimiter. The producer escapes
// literal markdown characters, so `\*` inside an emphasis span (or a code span) is
// content, not the closer — searching blindly closes the span early and leaks the
// backslash into the page.
function findUnescaped(text: string, needle: string, from: number): number {
  for (let at = from; (at = text.indexOf(needle, at)) !== -1; at += needle.length) {
    let slashes = 0;
    while (at - slashes - 1 >= 0 && text[at - slashes - 1] === "\\") slashes++;
    if (slashes % 2 === 0) return at;
  }
  return -1;
}

function matchEmphasis(text: string, i: number) {
  for (const { d, tag } of DELIMS) {
    if (!text.startsWith(d, i)) continue;
    // `_` never opens emphasis mid-word — snake_case identifiers are not italics,
    // and Jira descriptions are full of them (default_make_payment_method).
    if (d[0] === "_" && /\w/.test(text[i - 1] ?? " ")) continue;
    const from = i + d.length;
    if (!text[from] || /\s/.test(text[from])) continue; // "* " opens nothing
    let end = findUnescaped(text, d, from);
    while (end > 0 && /\s/.test(text[end - 1])) end = findUnescaped(text, d, end + d.length);
    if (end < 0) continue;
    if (d[0] === "_" && /\w/.test(text[end + d.length] ?? " ")) continue;
    return { tag, body: text.slice(from, end), len: end + d.length - i };
  }
  return null;
}

const CODE = "rounded bg-zinc-800/80 px-1 py-px font-mono text-[0.92em] text-amber-200/90";
const LINK = "text-sky-400 underline decoration-sky-400/30 hover:decoration-sky-400";

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let buf = "";
  let n = 0;
  const add = (node: ReactNode) => {
    if (buf) {
      out.push(buf);
      buf = "";
    }
    out.push(node);
  };
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === "\\" && ESCAPABLE.test(text[i + 1] ?? "")) {
      buf += text[i + 1];
      i += 2;
      continue;
    }
    if (c === "`") {
      const fence = /^`+/.exec(text.slice(i))![0];
      const end = findUnescaped(text, fence, i + fence.length);
      if (end > 0) {
        add(
          <code key={`${key}c${n++}`} className={CODE}>
            {text.slice(i + fence.length, end).replace(/^ (.*) $/, "$1")}
          </code>,
        );
        i = end + fence.length;
        continue;
      }
    }
    if (c === "[") {
      // the href allows one level of balanced parens — Confluence/Jira links carry
      // them unescaped (…/Page_(draft)) and a plain [^)]+ would cut the URL short
      const m = /^\[((?:\\.|[^\\\]])*)\]\(<?((?:[^()\s]|\([^()\s]*\))+)>?(?:\s+"[^"]*")?\)/.exec(
        text.slice(i),
      );
      if (m) {
        const kids = inline(m[1], `${key}l${n}`);
        add(
          SAFE_HREF.test(m[2]) ? (
            <a key={`${key}a${n++}`} href={m[2]} target="_blank" rel="noreferrer noopener" className={LINK}>
              {kids}
            </a>
          ) : (
            <span key={`${key}a${n++}`}>{kids}</span>
          ),
        );
        i += m[0].length;
        continue;
      }
    }
    if (c === "h" || c === "w") {
      const m = /^(?:https?:\/\/|www\.)(?:[^\s<>()[\]"']|\([^\s<>()[\]"']*\))+/.exec(text.slice(i));
      if (m) {
        const raw = m[0].replace(/[.,;:!?]+$/, ""); // trailing sentence punctuation isn't part of it
        // A bare URL arrives carrying the producer's escapes (…type=INCOMING\_EMAIL),
        // and those would land in the href verbatim. A real backslash never appears in
        // a URL — it would be percent-encoded — so undoing them here is safe.
        const url = raw.replace(/\\([\\`*_~[\]])/g, "$1");
        add(
          <a
            key={`${key}u${n++}`}
            href={url.startsWith("www.") ? `https://${url}` : url}
            target="_blank"
            rel="noreferrer noopener"
            className={LINK}
          >
            {url}
          </a>,
        );
        i += raw.length;
        continue;
      }
    }
    const emph = matchEmphasis(text, i);
    if (emph) {
      const kids = inline(emph.body, `${key}e${n}`);
      const k = `${key}e${n++}`;
      add(
        emph.tag === "strong" ? (
          <strong key={k} className="font-semibold text-zinc-100">
            {kids}
          </strong>
        ) : emph.tag === "del" ? (
          <del key={k} className="text-zinc-500">
            {kids}
          </del>
        ) : (
          <em key={k}>{kids}</em>
        ),
      );
      i += emph.len;
      continue;
    }
    buf += c;
    i++;
  }
  if (buf) out.push(buf);
  return out;
}

// ── blocks → elements ────────────────────────────────────────────────────────
const TH = "border-b border-zinc-700 bg-zinc-900/70 px-2 py-1 text-left align-top font-semibold text-zinc-200";
const TD = "border-t border-zinc-800 px-2 py-1 align-top";

function render(blocks: Block[], key: string): ReactNode[] {
  return blocks.map((b, n) => {
    const k = `${key}-${n}`;
    switch (b.k) {
      case "h":
        return (
          <div
            key={k}
            className={`mt-3 mb-1 font-semibold text-zinc-100 first:mt-0 ${
              b.level <= 2 ? "text-[15px]" : b.level === 3 ? "text-sm" : "text-[13px]"
            }`}
          >
            {inline(b.text, k)}
          </div>
        );
      case "p":
        // pre-wrap: our ADF converter emits one line per paragraph and a real
        // hardBreak as "  \n", so every remaining newline IS a break to keep.
        return (
          <p key={k} className="my-1.5 whitespace-pre-wrap first:mt-0">
            {inline(b.text.replace(/ {2,}\n/g, "\n"), k)}
          </p>
        );
      case "code":
        return (
          <pre key={k} className="my-2 overflow-x-auto rounded border border-zinc-800 bg-black/50 p-2">
            <code className="font-mono text-[12px] leading-relaxed text-zinc-200">{b.text}</code>
          </pre>
        );
      case "hr":
        return <hr key={k} className="my-3 border-zinc-800" />;
      case "quote":
        return (
          <blockquote key={k} className="my-2 border-l-2 border-zinc-700 pl-2.5 text-zinc-400">
            {render(b.blocks, k)}
          </blockquote>
        );
      case "list": {
        const items = b.items.map((it, j) => (
          <li key={j} className="pl-0.5 [&>p]:my-0.5">
            {render(it, `${k}-${j}`)}
          </li>
        ));
        return b.ordered ? (
          <ol key={k} start={b.start} className="my-1.5 ml-5 list-decimal space-y-1">
            {items}
          </ol>
        ) : (
          <ul key={k} className="my-1.5 ml-5 list-disc space-y-1">
            {items}
          </ul>
        );
      }
      case "table": {
        // an all-empty header row means the source table had no header cells — the
        // separator is only there to make it a table, so don't render a blank strip
        const headed = b.rows[0].some((c) => c.trim());
        const [head, rows] = headed ? [b.rows[0], b.rows.slice(1)] : [null, b.rows];
        return (
          <div key={k} className="my-2 overflow-x-auto rounded border border-zinc-800">
            <table className="w-full border-collapse text-[12px]">
              {head && (
                <thead>
                  <tr>
                    {head.map((c, j) => (
                      <th key={j} className={TH}>
                        {inline(c, `${k}h${j}`)}
                      </th>
                    ))}
                  </tr>
                </thead>
              )}
              <tbody>
                {rows.map((r, ri) => (
                  <tr key={ri}>
                    {r.map((c, j) => (
                      <td key={j} className={TD}>
                        {inline(c, `${k}r${ri}c${j}`)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }
    }
  });
}

export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => parse(text.replace(/\r\n?/g, "\n").split("\n")), [text]);
  return <div className="leading-relaxed">{render(blocks, "b")}</div>;
}
