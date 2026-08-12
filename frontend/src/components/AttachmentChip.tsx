import { useEffect, useState } from "react";
import { fetchAttachment } from "../api.ts";
import { bytes } from "../format.ts";
import type { Attachment } from "../types.ts";

/** Only auto-preview small images. This bound is what keeps the buffering cost of the
 *  blob download invisible — a 25 MB image would otherwise be fetched whole just to
 *  show a thumbnail nobody asked for. */
const PREVIEW_MAX = 2_000_000;

/* There was a 16-cell binary strip here, derived from the blob hash, meant to make
 * dedup visible: two chips with the same strip ARE the same bytes on disk. It worked —
 * a shared 6 MB attachment rendered an identical strip across two unrelated emails.
 * It was cut anyway, for three reasons that outweigh that:
 *
 *   1. The content address itself is printed immediately beside it. The hex is honest,
 *      verifiable and does the same job.
 *   2. At 3px cells with no label it reads as a barcode to anyone not told what it is.
 *   3. The comparison only pays off when two attachments sit in one view, which almost
 *      never happens — you read one message at a time.
 *
 * A mechanic the user has to be told to notice is decoration, whatever it was meant as.
 * If it comes back, it belongs on the Storage screen where dedup is the subject, and it
 * needs the relationship named ("same bytes as …") rather than left to be spotted.
 */

export function AttachmentChip({ att }: { att: Attachment }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const previewable =
    !!att.content_type?.startsWith("image/") && att.size_bytes > 0 && att.size_bytes <= PREVIEW_MAX;

  // An object URL pins its blob for the lifetime of the document. Revoking on unmount
  // is not tidiness — without it, opening ten messages leaks ten images' worth of memory.
  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  async function download() {
    setBusy(true);
    setError(null);
    try {
      const blob = await fetchAttachment(att.url);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      // The filename we already hold, rather than parsing Content-Disposition back
      // out of a header the server percent-encoded on the way out.
      a.download = att.filename || "attachment";
      a.click();
      // Revoked on the next task, not immediately: Safari cancels an in-flight
      // navigation to an object URL that has already been revoked.
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Download failed");
    } finally {
      setBusy(false);
    }
  }

  async function preview() {
    setBusy(true);
    setError(null);
    try {
      const blob = await fetchAttachment(att.url);
      setPreviewUrl(URL.createObjectURL(blob));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the image");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="att">
        <div className="att__grow">
          <div className="att__name">{att.filename || "(unnamed file)"}</div>
          <div className="att__meta">
            <span>{bytes(att.size_bytes)}</span>
            <span>{att.content_type || "application/octet-stream"}</span>
            <span>{att.blob_hash.slice(0, 10)}</span>
          </div>
        </div>

        {previewable && !previewUrl && (
          <button className="btn btn--bordered" onClick={preview} disabled={busy}>
            Preview
          </button>
        )}
        <button className="btn btn--bordered" onClick={download} disabled={busy}>
          {busy ? "Working…" : "Download"}
        </button>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {previewUrl && (
        <img className="att__preview" src={previewUrl} alt={att.filename || "Attachment"} />
      )}
    </div>
  );
}
