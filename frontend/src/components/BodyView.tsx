import { useState } from "react";

/* Rendering a message body. This is the largest XSS surface in the product.
 *
 * `body_html` is written by whoever sent the email. There is no
 * `dangerouslySetInnerHTML` here and there must never be one: injecting sender HTML
 * into our own document hands an attacker the session token in sessionStorage and 24
 * hours of full mailbox access.
 *
 * It goes in an iframe with a bare `sandbox` attribute — no allow-scripts, no
 * allow-same-origin — so the content runs in an opaque origin with scripting disabled.
 * A sanitiser is a denylist that has to be right every time; the sandbox is a boundary
 * the browser enforces. That is why there is no DOMPurify dependency here.
 *
 * The CSP meta on top of the sandbox blocks remote loads, which stops the tracking
 * pixel that tells a sender exactly when you opened their mail. "Load remote images"
 * swaps one directive and is per message, never sticky.
 */

const CSP = (remoteImages: boolean) =>
  `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; ` +
  `img-src data:${remoteImages ? " https:" : ""}; style-src 'unsafe-inline'; ` +
  `font-src data:">` +
  // Without a base target, a link inside the mail would try to navigate the frame,
  // which the sandbox blocks silently — the user would just see nothing happen.
  `<base target="_blank">` +
  `<style>html{color-scheme:light}body{margin:0;padding:12px;font:16px/1.5 system-ui}` +
  `img{max-width:100%;height:auto}</style>`;

export function BodyView({ text, html }: { text: string | null; html: string | null }) {
  const [showHtml, setShowHtml] = useState(false);
  const [remoteImages, setRemoteImages] = useState(false);

  // Plain text is preferred when it exists: it is the same message with none of the
  // risk and none of the layout surprises.
  const hasText = !!text?.trim();
  const hasHtml = !!html?.trim();

  if (!hasText && !hasHtml) {
    return <p className="msg__body muted">This message has no body.</p>;
  }

  const renderHtml = hasHtml && (showHtml || !hasText);

  return (
    <div className="msg__body">
      {hasHtml && (
        <div className="msg__framebar">
          <span>
            {renderHtml
              ? "Showing the sender's HTML in a sandbox. Scripts cannot run."
              : "This message also has an HTML version."}
          </span>
          <span className="row-flex">
            {hasText && (
              <button className="btn btn--bordered" onClick={() => setShowHtml(!showHtml)}>
                {renderHtml ? "Show plain text" : "Show HTML"}
              </button>
            )}
            {renderHtml && !remoteImages && (
              <button className="btn btn--bordered" onClick={() => setRemoteImages(true)}>
                Load remote images
              </button>
            )}
          </span>
        </div>
      )}

      {renderHtml ? (
        <iframe
          className="msg__frame"
          // Two allow-* tokens, and ONLY these two. Everything that matters is still
          // absent: no `allow-scripts`, so sender JavaScript cannot run, and no
          // `allow-same-origin`, so the frame stays an opaque origin that cannot read
          // our cookies, our sessionStorage or our DOM. The XSS argument is untouched.
          //
          // They are here because a bare `sandbox` also blocks opening a link, which
          // made `<base target="_blank">` above a lie: every link in every HTML email
          // silently did nothing, which is the exact failure that line exists to
          // prevent. Verified in the console —
          //   "Blocked opening '…' in a new window because the request was made in a
          //    sandboxed frame whose 'allow-popups' permission is not set."
          // `-to-escape-sandbox` means the page that opens is a normal page rather than
          // an inheriting sandboxed one, which is what makes the destination site work.
          // `_blank` implies `noopener` in current browsers, so there is no tabnabbing
          // path back to us.
          //
          // Never add `allow-scripts` or `allow-same-origin`. Together they let the
          // frame remove its own sandbox attribute, which is the whole boundary.
          sandbox="allow-popups allow-popups-to-escape-sandbox"
          referrerPolicy="no-referrer"
          title="Message content"
          // Keyed on the toggle so flipping remote images rebuilds the document —
          // an already-parsed CSP cannot be relaxed in place.
          key={String(remoteImages)}
          srcDoc={CSP(remoteImages) + (html ?? "")}
        />
      ) : (
        <pre>{text}</pre>
      )}
    </div>
  );
}
