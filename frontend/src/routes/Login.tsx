import { useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { useAuth } from "../auth.tsx";

export default function Login() {
  const { session, signIn } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [address, setAddress] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // `next` comes from the query string, so it is treated as untrusted input rather than
  // as a route. It must be one of our own paths: a single leading slash. "//evil.test"
  // is protocol-relative — the router hands it to history.pushState, which rejects a
  // cross-origin URL with a SecurityError, so a signed-in user would be stranded on this
  // form with a browser error where the redirect should have been.
  const raw = params.get("next");
  const next = raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/mail/inbox";
  // Set only by the 401 handler, never by the signed-out redirect — see auth.tsx.
  const expired = params.has("expired");

  if (session) return <Navigate to={next} replace />;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(address.trim(), password);
      navigate(next, { replace: true });
    } catch (err) {
      // The server answers one message for "no such mailbox" and for "wrong password"
      // on purpose — telling them apart lets anyone enumerate addresses. Show its
      // wording rather than inventing our own.
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="login__box" onSubmit={submit}>
        <div>
          <h1 className="page__title">Nimbus</h1>
          <p className="rail__scope" style={{ margin: 0 }}>
            receive only · no sending
          </p>
        </div>

        {expired && (
          <p className="hint" role="status">
            Your session ended. Sign in to continue.
          </p>
        )}

        <div className="field">
          <label htmlFor="address">Email address</label>
          <input
            id="address"
            className="input"
            type="email"
            autoComplete="username"
            autoFocus
            required
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            aria-invalid={!!error}
            aria-describedby={error ? "login-error" : undefined}
          />
        </div>

        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={!!error}
            aria-describedby={error ? "login-error" : undefined}
          />
        </div>

        {error && (
          <p className="error" id="login-error" role="alert">
            {error}
          </p>
        )}

        <button className="btn btn--primary" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
