import type { IncidentStatus } from "./api/client";

type Tone = "blue" | "amber" | "green" | "red" | "slate";

export const STATUS: Record<IncidentStatus, { label: string; tone: Tone; terminal: boolean }> = {
  investigating: { label: "Investigating", tone: "blue", terminal: false },
  pending_approval: { label: "Pending approval", tone: "amber", terminal: false },
  approved: { label: "Approved", tone: "blue", terminal: false },
  applying: { label: "Applying", tone: "blue", terminal: false },
  applied: { label: "Applied", tone: "green", terminal: true },
  rejected: { label: "Rejected", tone: "slate", terminal: true },
  no_action: { label: "No action", tone: "slate", terminal: true },
  failed: { label: "Failed", tone: "red", terminal: true },
};

export const TONE_CLASSES: Record<Tone, string> = {
  blue: "bg-blue-50 text-blue-700 ring-blue-600/20 dark:bg-blue-950 dark:text-blue-300 dark:ring-blue-400/30",
  amber:
    "bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-400/30",
  green:
    "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-400/30",
  red: "bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-950 dark:text-red-300 dark:ring-red-400/30",
  slate:
    "bg-slate-100 text-slate-700 ring-slate-500/20 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-400/30",
};

/** Statuses where the backend is still working and the UI should keep refreshing. */
export function isActive(status: IncidentStatus): boolean {
  return status === "investigating" || status === "approved" || status === "applying";
}

export const CONFIDENCE_TONE: Record<"high" | "medium" | "low", Tone> = {
  high: "green",
  medium: "amber",
  low: "red",
};
