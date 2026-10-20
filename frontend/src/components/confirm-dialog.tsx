"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "./ui";

/** Native <dialog> (focus trap + Esc handling for free) with an optional comment field. */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  variant = "primary",
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  variant?: "primary" | "danger";
  busy: boolean;
  onConfirm: (comment: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [comment, setComment] = useState("");

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setComment("");
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onCancel();
      }}
      className="m-auto w-full max-w-lg rounded-lg border border-slate-200 bg-white p-0 text-slate-900 shadow-xl backdrop:bg-slate-900/40 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
    >
      <form
        method="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          onConfirm(comment.trim());
        }}
      >
        <div className="space-y-4 p-5">
          <h2 className="text-base font-semibold">{title}</h2>
          <div className="text-sm text-slate-600 dark:text-slate-300">{children}</div>
          <label className="block text-sm">
            <span className="font-medium">Comment</span>{" "}
            <span className="text-slate-500">(optional, recorded in the audit log)</span>
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              maxLength={2000}
              rows={3}
              className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-3 dark:border-slate-800">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant={variant} disabled={busy} autoFocus>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </div>
      </form>
    </dialog>
  );
}
