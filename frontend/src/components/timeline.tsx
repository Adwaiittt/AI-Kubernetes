"use client";

import { useState } from "react";

import { formatDuration, formatParams, formatTime } from "@/lib/format";
import { formatOutput, truncate, type TimelineItem } from "@/lib/timeline";

const PREVIEW_CHARS = 600;

export function Timeline({ items, live }: { items: TimelineItem[]; live: boolean }) {
  if (items.length === 0) {
    return <p className="text-sm text-slate-500">{live ? "Waiting for the agent…" : "No trace."}</p>;
  }
  return (
    <ol className="relative space-y-3 border-l border-slate-200 pl-5 dark:border-slate-800">
      {items.map((item) => (
        <li key={`${item.kind}-${item.seq}-${"id" in item ? item.id : ""}`} className="relative">
          <Dot item={item} />
          <Row item={item} />
        </li>
      ))}
      {live && (
        <li className="relative text-sm text-slate-500">
          <span className="absolute -left-[25px] top-1.5 size-2.5 animate-ping rounded-full bg-blue-500" />
          Investigating…
        </li>
      )}
    </ol>
  );
}

function Dot({ item }: { item: TimelineItem }) {
  const color =
    item.kind === "tool"
      ? { running: "bg-blue-500", ok: "bg-emerald-500", error: "bg-red-500", skipped: "bg-amber-500" }[
          item.status
        ]
      : item.kind === "failed" || (item.kind === "report" && !item.accepted)
        ? "bg-red-500"
        : item.kind === "report"
          ? "bg-emerald-500"
          : "bg-slate-400";
  return (
    <span
      className={`absolute -left-[25px] top-1.5 size-2.5 rounded-full ring-4 ring-white dark:ring-slate-900 ${color}`}
    />
  );
}

function Row({ item }: { item: TimelineItem }) {
  const time = <span className="font-mono text-xs text-slate-400">{formatTime(item.at)}</span>;
  switch (item.kind) {
    case "start":
      return (
        <p className="text-sm">
          {time} Investigation started{item.model && <> with <span className="font-mono">{item.model}</span></>}
        </p>
      );
    case "thought":
      return (
        <p className="text-sm italic text-slate-600 dark:text-slate-300">
          {time} {item.text}
        </p>
      );
    case "note":
      return (
        <p className="text-sm text-amber-700 dark:text-amber-300">
          {time} {item.text}
        </p>
      );
    case "failed":
      return (
        <p className="text-sm font-medium text-red-700 dark:text-red-300">
          {time} Investigation failed: {item.error}
        </p>
      );
    case "report":
      return item.accepted ? (
        <p className="text-sm font-medium text-emerald-700 dark:text-emerald-300">
          {time} Root-cause report submitted
        </p>
      ) : (
        <div className="text-sm text-red-700 dark:text-red-300">
          {time} Report rejected by validation; the agent was asked to fix:
          <ul className="ml-4 mt-1 list-disc">
            {item.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      );
    case "tool":
      return <ToolRow item={item} time={time} />;
  }
}

function ToolRow({
  item,
  time,
}: {
  item: Extract<TimelineItem, { kind: "tool" }>;
  time: React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const output = item.output !== undefined ? formatOutput(item.output) : undefined;
  const shown = output !== undefined && !expanded ? truncate(output, PREVIEW_CHARS) : null;
  return (
    <div className="text-sm">
      <div className="flex flex-wrap items-baseline gap-x-2">
        {time}
        <span className="font-mono font-semibold">{item.tool}</span>
        <span className="font-mono text-xs text-slate-500">({formatParams(item.input)})</span>
        {item.status === "running" && <span className="text-xs text-blue-600">running…</span>}
        {item.status === "error" && <span className="text-xs font-medium text-red-600">error</span>}
        {item.status === "skipped" && (
          <span className="text-xs font-medium text-amber-600">not run: budget exhausted</span>
        )}
        {item.durationMs !== undefined && item.status !== "skipped" && (
          <span className="text-xs text-slate-400">{formatDuration(item.durationMs)}</span>
        )}
      </div>
      {output !== undefined && (
        <div className="mt-1">
          <pre
            className={`max-h-96 overflow-auto rounded-md p-2 font-mono text-xs leading-relaxed ${
              item.status === "error"
                ? "bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-100"
                : "bg-slate-50 text-slate-800 dark:bg-slate-950 dark:text-slate-200"
            }`}
          >
            {shown ? shown.text : output}
          </pre>
          {(shown?.truncated || expanded) && (
            <button
              type="button"
              onClick={() => setExpanded((e) => !e)}
              className="mt-1 text-xs font-medium text-blue-600 hover:underline dark:text-blue-400"
            >
              {expanded ? "Show less" : `Show full output (${output.length.toLocaleString()} chars)`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
