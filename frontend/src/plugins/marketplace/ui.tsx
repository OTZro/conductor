// Small shared bits for the marketplace plugin's panel + dialog: a monochrome busy
// spinner (no colored icons — inherits the caller's text color via `currentColor`)
// and the repo → GitHub/host link resolver used by Browse rows, Installed rows, and
// the install dialog.

/** A repo string → its clickable https URL, or null when none is derivable (a local
 * path, or a scheme we don't recognize) — callers show the raw string as plain text
 * in that case. `owner/name` shorthand always resolves to GitHub (that's what it
 * means everywhere else in this plugin); a full URL or an ssh remote resolves to the
 * https form of whatever host it already names, GitHub or not. */
export function repoHref(repo: string): string | null {
  const r = repo.trim();
  if (/^[\w.-]+\/[\w.-]+$/.test(r)) return `https://github.com/${r}`;
  let m = /^https?:\/\/([^/]+)\/(.+?)(\.git)?\/?$/.exec(r);
  if (m) return `https://${m[1]}/${m[2]}`;
  m = /^git@([^:]+):(.+?)(\.git)?$/.exec(r); // git@host:owner/name.git
  if (m) return `https://${m[1]}/${m[2]}`;
  m = /^ssh:\/\/git@([^/]+)\/(.+?)(\.git)?$/.exec(r);
  if (m) return `https://${m[1]}/${m[2]}`;
  return null;
}

// Plain-text external-link glyph — currentColor via inheriting the link's own text
// color, not a separate colored icon.
export function RepoLink({ repo, className = "" }: { repo: string; className?: string }) {
  const href = repoHref(repo);
  if (!href) {
    return <span className={`font-mono text-caption text-zinc-500 ${className}`}>{repo}</span>;
  }
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={`font-mono text-caption text-sky-400 underline decoration-sky-400/30 hover:decoration-sky-400 ${className}`}
      title={href}
      onClick={(e) => e.stopPropagation()}
    >
      {repo} ↗
    </a>
  );
}

/** Inline busy spinner — `border-current` so it always matches whatever text color
 * it's dropped into (a primary button, a ghost button, dialog body text, …), never
 * a fixed color of its own. */
export function Spinner({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <span
      className={`inline-block shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent align-[-2px] ${className}`}
      aria-hidden="true"
    />
  );
}
