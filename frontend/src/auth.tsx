import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router";
import * as api from "./api.ts";

interface Session {
  /** The address the user typed at login. The API never returns it. */
  address: string;
  /** This mailbox's own id, read out of the token — see `readSub`. */
  mailboxId: string | null;
}

interface AuthValue {
  session: Session | null;
  signIn: (address: string, password: string) => Promise<void>;
  signOut: () => void;
}

const Ctx = createContext<AuthValue | null>(null);
const ADDRESS_KEY = "nimbus-address";

/** Read the `sub` claim out of the JWT we are already holding.
 *
 * This is NOT verification — the signature is the server's business and we never
 * check it. It answers one question the API cannot: which mailbox id is *mine*, so a
 * list row belonging to a different mailbox can be labelled as a shared one.
 *
 * ponytail: reads a claim name out of the token payload. Ceiling — couples the UI to
 * the token's internal shape; if `sub` is ever renamed, shared-mailbox rows are
 * silently mislabelled as your own. Upgrade path: `GET /v1/mailboxes/me` returning
 * `{id, address, readable[]}` from the existing `visibility.readable_mailboxes()`.
 * Requested and not built, so this is the honest degradation rather than a guess.
 */
function readSub(jwt: string): string | null {
  try {
    const payload = jwt.split(".")[1];
    if (!payload) return null;
    // base64url -> base64. atob rejects - and _ ; padding is optional for atob but
    // not for every engine, so normalise both.
    const b64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const sub = JSON.parse(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4))).sub;
    return typeof sub === "string" ? sub : null;
  } catch {
    return null;
  }
}

function restore(): Session | null {
  const t = api.token.get();
  if (!t) return null;
  try {
    return { address: sessionStorage.getItem(ADDRESS_KEY) ?? "", mailboxId: readSub(t) };
  } catch {
    return { address: "", mailboxId: readSub(t) };
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(restore);
  const navigate = useNavigate();

  const signOut = useCallback(() => {
    api.token.clear();
    try {
      sessionStorage.removeItem(ADDRESS_KEY);
    } catch {
      /* nothing to clear */
    }
    setSession(null);
  }, []);

  // One place turns a 401 into a redirect, and it keeps where the user was so they
  // land back on the same message after signing in again.
  useEffect(() => {
    api.setUnauthorizedHandler(() => {
      setSession(null);
      const here = window.location.pathname + window.location.search;
      // `expired=1` only on this path. `next` alone cannot mean "your session ended" —
      // a first-time visitor hitting / is redirected with a `next` too, and telling
      // them their session expired when they never had one is a small lie.
      navigate(`/login?expired=1&next=${encodeURIComponent(here)}`, { replace: true });
    });
  }, [navigate]);

  const signIn = useCallback(async (address: string, password: string) => {
    const { token } = await api.login(address, password);
    api.token.set(token);
    try {
      sessionStorage.setItem(ADDRESS_KEY, address);
    } catch {
      /* the in-memory session still carries it for this tab */
    }
    setSession({ address, mailboxId: readSub(token) });
  }, []);

  const value = useMemo(() => ({ session, signIn, signOut }), [session, signIn, signOut]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
