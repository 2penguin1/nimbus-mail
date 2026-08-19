import { Component, type ErrorInfo, type ReactNode } from "react";

/* The only class component in the app, because React offers no hook for this — there
 * is no `useErrorBoundary`, and `componentDidCatch` exists only on a class.
 *
 * Without one, a render error anywhere unmounts the WHOLE tree and the user is left
 * looking at a white page with no explanation and no way back. React says so in the
 * console on every such error; it was saying so here.
 *
 * A mail client makes this concrete: every message body, sender name and attachment
 * filename is a string an untrusted stranger chose. The iframe in BodyView.tsx contains
 * the HTML, but a value that surprises a formatter is a different kind of failure, and
 * the honest answer to "we did not expect this" is to say so and keep the rest of the
 * app usable — not to disappear.
 */

type Props = { children: ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // There is no error-reporting service and adding one would be a dependency with no
    // stated need. The console is what an operator has, so make the entry findable.
    console.error("Nimbus: unhandled render error", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div className="crash" role="alert">
        <h1 className="crash__title">This screen stopped working</h1>
        <p className="crash__body">
          Nothing was lost. Your mail is on the server, and reloading usually fixes it.
        </p>
        <div className="crash__actions">
          {/* A full reload, not a state reset. The tree that threw is the tree we would
            * be re-rendering, so clearing the error alone tends to throw again
            * immediately and trap the user in a loop. */}
          <button type="button" className="btn btn--primary" onClick={() => location.reload()}>
            Reload
          </button>
          <a className="btn" href="/mail/inbox">
            Back to inbox
          </a>
        </div>
        {/* Shown, not hidden. The person who hits this is far more likely to be
          * developing Nimbus than using it, and a message they can paste is worth more
          * than a tidier box. */}
        <pre className="crash__detail mono">{this.state.error.message}</pre>
      </div>
    );
  }
}
