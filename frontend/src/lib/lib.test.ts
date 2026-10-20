import { describe, expect, it } from "vitest";

import { errorMessage, unwrap } from "./api/client";
import { diffStats, parseDiff } from "./diff";
import { formatDuration, formatParams, timeAgo } from "./format";
import { isActive, STATUS } from "./status";
import { buildTimeline, formatOutput, mergeEvents, truncate } from "./timeline";
import type { TraceEvent } from "./api/client";

const at = "2026-10-20T12:00:00Z";
const ev = (seq: number, type: string, data: Record<string, unknown> = {}): TraceEvent => ({
  seq,
  type,
  at,
  data,
});

describe("timeline", () => {
  it("pairs tool calls with their results and keeps order", () => {
    const items = buildTimeline([
      ev(0, "investigation_started", { model: "claude-sonnet-5-5" }),
      ev(1, "model_response", { text: ["Checking pods first."], tool_uses: ["list_pods"] }),
      ev(2, "tool_call", { tool_use_id: "t1", tool: "list_pods", input: { namespace: "demo" } }),
      ev(3, "tool_result", { tool_use_id: "t1", is_error: false, executed: true, duration_ms: 42, content: '{"pods":[]}' }),
      ev(4, "tool_call", { tool_use_id: "t2", tool: "describe_pod", input: { name: "x" } }),
      ev(5, "tool_result", { tool_use_id: "t2", is_error: true, executed: true, content: "404" }),
      ev(6, "tool_call", { tool_use_id: "t3", tool: "get_events", input: {} }),
      ev(7, "tool_result", { tool_use_id: "t3", is_error: true, executed: false, content: "budget" }),
      ev(8, "report_rejected", { problems: ["confidence: bad"] }),
      ev(9, "report_accepted", {}),
    ]);
    expect(items.map((i) => i.kind)).toEqual([
      "start", "thought", "tool", "tool", "tool", "report", "report",
    ]);
    const tools = items.filter((i) => i.kind === "tool");
    expect(tools.map((t) => (t.kind === "tool" ? t.status : null))).toEqual(["ok", "error", "skipped"]);
    expect(tools[0]).toMatchObject({ tool: "list_pods", durationMs: 42, output: '{"pods":[]}' });
  });

  it("shows a running tool until its result arrives", () => {
    const [item] = buildTimeline([ev(0, "tool_call", { tool_use_id: "t", tool: "get_metrics", input: {} })]);
    expect(item).toMatchObject({ kind: "tool", status: "running" });
  });

  it("merges replayed SSE events by seq without duplicates", () => {
    const merged = mergeEvents([ev(0, "a"), ev(1, "b")], [ev(1, "b"), ev(3, "d"), ev(2, "c")]);
    expect(merged.map((e) => e.seq)).toEqual([0, 1, 2, 3]);
  });

  it("formats and truncates tool output", () => {
    expect(formatOutput('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(formatOutput("plain log line")).toBe("plain log line");
    expect(truncate("abcdef", 3)).toEqual({ text: "abc…", truncated: true });
    expect(truncate("ab", 3)).toEqual({ text: "ab", truncated: false });
  });
});

describe("diff", () => {
  it("classifies unified diff lines", () => {
    const lines = parseDiff("--- before\n+++ after\n@@ -1 +1 @@\n ctx\n-old\n+new\n");
    expect(lines.map((l) => l.kind)).toEqual(["meta", "meta", "hunk", "ctx", "del", "add"]);
    expect(diffStats(lines)).toEqual({ added: 1, removed: 1 });
    expect(parseDiff("")).toEqual([]);
  });
});

describe("status", () => {
  it("knows which statuses are still in flight", () => {
    expect(isActive("investigating")).toBe(true);
    expect(isActive("applying")).toBe(true);
    expect(isActive("applied")).toBe(false);
    expect(Object.values(STATUS).filter((s) => s.terminal)).toHaveLength(4);
  });
});

describe("format", () => {
  it("formats durations, params and relative time", () => {
    expect(formatDuration(null)).toBe("–");
    expect(formatDuration(250)).toBe("250 ms");
    expect(formatDuration(1500)).toBe("1.5 s");
    expect(formatParams({ container: "app", replicas: 3 })).toBe("container=app, replicas=3");
    expect(timeAgo(at, new Date(at).getTime() + 5 * 60_000)).toBe("5 minutes ago");
  });
});

describe("api client helpers", () => {
  it("surfaces FastAPI error details", () => {
    expect(errorMessage({ detail: "run a dry run before approving" })).toBe(
      "run a dry run before approving",
    );
    expect(errorMessage({ detail: [{ msg: "field required" }, { msg: "too short" }] })).toBe(
      "field required; too short",
    );
    const response = new Response(null, { status: 409 });
    expect(() => unwrap({ error: { detail: "conflict" }, response })).toThrow("conflict");
    expect(unwrap({ data: 1, response: new Response(null, { status: 200 }) })).toBe(1);
  });
});
