import type { TraceEvent } from "./api/client";

/** One row of the investigation timeline, derived from the raw agent transcript. */
export type TimelineItem =
  | { kind: "start"; seq: number; at: string; model?: string }
  | { kind: "thought"; seq: number; at: string; text: string }
  | {
      kind: "tool";
      seq: number;
      at: string;
      id: string;
      tool: string;
      input: Record<string, unknown>;
      status: "running" | "ok" | "error" | "skipped";
      output?: string;
      durationMs?: number;
    }
  | { kind: "report"; seq: number; at: string; accepted: boolean; problems: string[] }
  | { kind: "note"; seq: number; at: string; text: string }
  | { kind: "failed"; seq: number; at: string; error: string };

/** Merge events by seq (dedupes SSE replays) and keep them ordered. */
export function mergeEvents(existing: TraceEvent[], incoming: TraceEvent[]): TraceEvent[] {
  const bySeq = new Map(existing.map((e) => [e.seq, e]));
  for (const e of incoming) bySeq.set(e.seq, e);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

export function buildTimeline(events: TraceEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const tools = new Map<string, Extract<TimelineItem, { kind: "tool" }>>();

  for (const e of mergeEvents([], events)) {
    const d = e.data;
    switch (e.type) {
      case "investigation_started":
        items.push({ kind: "start", seq: e.seq, at: e.at, model: str(d.model) || undefined });
        break;
      case "model_response":
        for (const text of Array.isArray(d.text) ? d.text : []) {
          if (str(text).trim()) items.push({ kind: "thought", seq: e.seq, at: e.at, text: str(text) });
        }
        break;
      case "tool_call": {
        const item: Extract<TimelineItem, { kind: "tool" }> = {
          kind: "tool",
          seq: e.seq,
          at: e.at,
          id: str(d.tool_use_id),
          tool: str(d.tool),
          input: (d.input as Record<string, unknown>) ?? {},
          status: "running",
        };
        tools.set(item.id, item);
        items.push(item);
        break;
      }
      case "tool_result": {
        const item = tools.get(str(d.tool_use_id));
        if (item) {
          item.status = d.executed === false ? "skipped" : d.is_error ? "error" : "ok";
          item.output = str(d.content);
          item.durationMs = typeof d.duration_ms === "number" ? d.duration_ms : undefined;
        }
        break;
      }
      case "report_accepted":
        items.push({ kind: "report", seq: e.seq, at: e.at, accepted: true, problems: [] });
        break;
      case "report_rejected":
        items.push({
          kind: "report",
          seq: e.seq,
          at: e.at,
          accepted: false,
          problems: Array.isArray(d.problems) ? d.problems.map(String) : [],
        });
        break;
      case "nudge":
        items.push({ kind: "note", seq: e.seq, at: e.at, text: str(d.message) });
        break;
      case "investigation_failed":
        items.push({ kind: "failed", seq: e.seq, at: e.at, error: str(d.error) });
        break;
    }
  }
  return items;
}

/** Pretty-print a tool's JSON output when possible; fall back to the raw text. */
export function formatOutput(output: string): string {
  try {
    return JSON.stringify(JSON.parse(output), null, 2);
  } catch {
    return output;
  }
}

export function truncate(text: string, max: number): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false };
  return { text: `${text.slice(0, max)}…`, truncated: true };
}
