import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router";
import { useAuth } from "../auth.tsx";
import { FOLDERS } from "../types.ts";

const THEME_KEY = "nimbus-theme";
type Theme = "light" | "dark" | "system";

/* Theme in eight lines instead of next-themes. "system" removes the attribute and
 * lets the media query in tokens.css decide, which is why the stylesheet declares
 * dark under both a media query and a [data-theme] scope. */
function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const t = localStorage.getItem(THEME_KEY);
      return t === "light" || t === "dark" ? t : "system";
    } catch {
      return "system";
    }
  });

  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    try {
      if (theme === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* private mode: the choice lasts for this page load only */
    }
  }, [theme]);

  return [theme, setTheme];
}

export function Shell({ children }: { children: ReactNode }) {
  const { session, signOut } = useAuth();
  const [theme, setTheme] = useTheme();
  const loc = useLocation();
  const snoozedView = new URLSearchParams(loc.search).get("snoozed") === "1";

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <nav className="rail" aria-label="Mailbox">
        <div className="rail__brand">Nimbus</div>
        {/* Not a tagline — the product's scope, stated where a Compose button would
            otherwise be. A greyed-out Compose is a promise; this is the truth. */}
        <div className="rail__scope">receive only · no sending</div>

        {/* Plain Link, not NavLink. NavLink decides "current" from the pathname alone
            and sets aria-current itself, which is wrong twice here: it cannot see that
            ?snoozed=1 is a different view of the same path, and with `end` it drops the
            highlight as soon as a message id is appended. Computing it here is three
            lines and is actually correct. */}
        <div className="rail__heading">Folders</div>
        {FOLDERS.map((f) => (
          <Link
            key={f}
            to={`/mail/${f}`}
            className="rail__link"
            style={{ textTransform: "capitalize" }}
            aria-current={
              loc.pathname.startsWith(`/mail/${f}`) && !snoozedView ? "page" : undefined
            }
          >
            {f}
          </Link>
        ))}

        {/* Snoozed is a query param on the inbox, not a folder — `folder` stays an
            equality on the server so its list index is still fully used. It needs a
            home in the rail because it is the only way to reach snoozed mail. */}
        <Link
          to="/mail/inbox?snoozed=1"
          className="rail__link"
          aria-current={snoozedView ? "page" : undefined}
        >
          Snoozed
        </Link>

        <div className="rail__heading">Find</div>
        <Link
          to="/search"
          className="rail__link"
          aria-current={loc.pathname === "/search" ? "page" : undefined}
        >
          Search
        </Link>
        <Link
          to="/storage"
          className="rail__link"
          aria-current={loc.pathname === "/storage" ? "page" : undefined}
        >
          Storage
        </Link>

        <div className="rail__foot">
          <div className="hint mono" title={session?.address}>
            {session?.address || "signed in"}
          </div>
          <button
            className="btn"
            onClick={() => setTheme(theme === "dark" ? "light" : theme === "light" ? "system" : "dark")}
          >
            Theme: {theme}
          </button>
          <button className="btn" onClick={signOut}>
            Sign out
          </button>
        </div>
      </nav>

      <main id="main" className="pane">
        {children}
      </main>
    </div>
  );
}
