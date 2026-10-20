import { diffStats, parseDiff } from "@/lib/diff";

const LINE_STYLES = {
  add: "bg-emerald-50 text-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-200",
  del: "bg-red-50 text-red-900 dark:bg-red-950/60 dark:text-red-200",
  hunk: "text-blue-600 dark:text-blue-400",
  meta: "text-slate-400",
  ctx: "text-slate-600 dark:text-slate-400",
} as const;

export function DiffView({ diff }: { diff: string }) {
  const lines = parseDiff(diff);
  if (lines.length === 0) {
    return <p className="text-sm text-slate-500">No changes: the deployment is already in this state.</p>;
  }
  const { added, removed } = diffStats(lines);
  return (
    <div>
      <p className="mb-1 text-xs text-slate-500">
        <span className="text-emerald-600">+{added}</span> / <span className="text-red-600">−{removed}</span>{" "}
        lines in the Deployment spec
      </p>
      <pre className="max-h-80 overflow-auto rounded-md border border-slate-200 font-mono text-xs leading-5 dark:border-slate-800">
        {lines.map((line, i) => (
          <div key={i} className={`px-3 ${LINE_STYLES[line.kind]}`}>
            {line.text || " "}
          </div>
        ))}
      </pre>
    </div>
  );
}
