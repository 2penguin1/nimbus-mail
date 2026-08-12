import { useEffect, useId, useRef, type ReactNode } from "react";

/* Native <dialog>. showModal() gives a focus trap, Esc-to-close, an inert background
 * and spec-mandated focus return to whatever was focused before — all the things a
 * dialog library exists to provide. Radix would be ~10 KB to re-implement them.
 *
 * ponytail: no exit animation. Ceiling — the dialog vanishes instantly on close.
 * Upgrade path: @starting-style plus transition-behavior: allow-discrete, in CSS only.
 */

export function ConfirmDialog({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  // Toolbar mounts two of these at once (delete, snooze). A fixed id would be duplicated
  // in the document, and `aria-labelledby` resolves to the first match — so opening
  // Snooze announced "Delete this copy?" to a screen reader.
  const titleId = useId();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Guarded both ways: calling showModal on an already-open dialog throws
    // InvalidStateError, and close() on a shut one fires a stray cancel event.
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      // Esc dispatches `cancel` then `close`; listening to close covers both that and
      // a backdrop-driven dismissal, so React state cannot drift from the DOM's.
      onClose={onClose}
    >
      <h2 className="dialog__title" id={titleId}>
        {title}
      </h2>
      {children}
    </dialog>
  );
}
