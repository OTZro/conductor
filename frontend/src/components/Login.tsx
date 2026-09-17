import { useCallback, useEffect, useRef, useState } from "react";
import { startAuthentication, startRegistration } from "@simplewebauthn/browser";
import {
  googleLogin,
  waLoginOptions,
  waLoginVerify,
  waRegisterOptions,
  waRegisterVerify,
} from "../api";
import type { AuthMe } from "../api";

type GoogleAccountsId = {
  initialize: (config: {
    client_id: string;
    callback: (resp: { credential: string }) => void;
  }) => void;
  renderButton: (parent: HTMLElement, options: Record<string, unknown>) => void;
};

declare global {
  interface Window {
    google?: { accounts: { id: GoogleAccountsId } };
  }
}

function friendly(message: string): string {
  if (message.includes("email not allowed")) return "That account isn't on the allowlist.";
  if (message.includes("setup token invalid")) return "Setup token is wrong — copy it from .env.";
  if (message.includes("origin not allowed"))
    return "This origin isn't allowed for login (check CONDUCTOR_ALLOWED_ORIGINS).";
  return message;
}

export function Login({ me, onLoggedIn }: { me: AuthMe; onLoggedIn: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [setupToken, setSetupToken] = useState("");
  const [email, setEmail] = useState("");
  const googleDiv = useRef<HTMLDivElement>(null);

  const passkeySupported = typeof window !== "undefined" && !!window.PublicKeyCredential;

  const doPasskeyLogin = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const { state_id, options } = await waLoginOptions();
      const credential = await startAuthentication({ optionsJSON: options });
      await waLoginVerify({ state_id, credential });
      onLoggedIn();
    } catch (e) {
      setError(friendly(e instanceof Error ? e.message : "passkey login failed"));
    } finally {
      setBusy(false);
    }
  }, [onLoggedIn]);

  const doSetupRegister = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const { state_id, options } = await waRegisterOptions({
        setup_token: setupToken,
        email: email.trim(),
      });
      const credential = await startRegistration({ optionsJSON: options });
      await waRegisterVerify({ state_id, credential, setup_token: setupToken });
      onLoggedIn();
    } catch (e) {
      setError(friendly(e instanceof Error ? e.message : "passkey registration failed"));
    } finally {
      setBusy(false);
    }
  }, [setupToken, email, onLoggedIn]);

  // Google Identity Services button — only when a client id is configured.
  useEffect(() => {
    const clientId = me.google_client_id;
    if (!clientId) return;
    let cancelled = false;
    const init = () => {
      const gsi = window.google?.accounts.id;
      if (cancelled || !gsi || !googleDiv.current) return;
      gsi.initialize({
        client_id: clientId,
        callback: async (resp) => {
          setError(null);
          try {
            await googleLogin(resp.credential);
            onLoggedIn();
          } catch (e) {
            setError(friendly(e instanceof Error ? e.message : "google login failed"));
          }
        },
      });
      gsi.renderButton(googleDiv.current, { theme: "filled_black", size: "large", width: 280 });
    };
    if (window.google?.accounts) {
      init();
      return;
    }
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.onload = init;
    document.head.appendChild(script);
    return () => {
      cancelled = true;
      script.remove();
    };
  }, [me.google_client_id, onLoggedIn]);

  return (
    <div
      className="h-full flex items-center justify-center bg-zinc-950 text-zinc-100"
      data-testid="login-root"
    >
      <div className="w-full max-w-sm mx-4 p-6 rounded-xl border border-zinc-800 bg-zinc-900/60 flex flex-col gap-4">
        <div className="flex items-center gap-2">
          <svg width="22" height="22" viewBox="0 0 32 32">
            <rect width="32" height="32" rx="7" fill="#0f0f12" />
            <rect x="6.5" y="10" width="4.5" height="16" rx="2" fill="#64748b" />
            <rect x="13.75" y="6" width="4.5" height="20" rx="2" fill="#fbbf24" />
            <rect x="21" y="13" width="4.5" height="13" rx="2" fill="#38bdf8" />
          </svg>
          <h1 className="text-lg font-bold tracking-tight">Conductor</h1>
        </div>
        <p className="text-sm text-zinc-400">Sign in to your cockpit.</p>
        {passkeySupported && (
          <button
            onClick={doPasskeyLogin}
            disabled={busy}
            className="w-full px-3 py-2 rounded bg-sky-600 hover:bg-sky-500 text-sm font-medium disabled:opacity-50"
          >
            Sign in with a passkey
          </button>
        )}
        {me.google_client_id && <div ref={googleDiv} className="flex justify-center min-h-[40px]" />}
        {error && (
          <div className="text-sm text-red-400" data-testid="login-error">
            {error}
          </div>
        )}
        {me.setup_available && (
          <details className="border-t border-zinc-800 pt-3">
            <summary className="cursor-pointer select-none text-xs text-zinc-400 hover:text-zinc-200">
              First-time setup (passkey)
            </summary>
            <div className="mt-3 flex flex-col gap-2">
              <input
                type="password"
                value={setupToken}
                onChange={(e) => setSetupToken(e.target.value)}
                placeholder="setup token (CONDUCTOR_AUTH_SETUP_TOKEN)"
                data-testid="setup-token"
                className="text-sm px-2 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
              />
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                data-testid="setup-email"
                className="text-sm px-2 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-zinc-100"
              />
              <button
                onClick={doSetupRegister}
                disabled={busy || !passkeySupported || !setupToken || !email.trim()}
                data-testid="setup-register"
                className="px-3 py-1.5 rounded bg-emerald-700 hover:bg-emerald-600 text-sm disabled:opacity-50"
              >
                Register passkey
              </button>
              <p className="text-xs text-zinc-500">
                Paste the setup token from .env, enter your allowlisted email, then register this
                device's passkey.
              </p>
            </div>
          </details>
        )}
      </div>
    </div>
  );
}
