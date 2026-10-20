import type { RootCauseReport } from "@/lib/api/client";

import { Card, ConfidenceBadge, Mono } from "./ui";

export function ReportCard({ report }: { report: RootCauseReport }) {
  return (
    <Card title="Root-cause report" actions={<ConfidenceBadge confidence={report.confidence} />}>
      <div className="space-y-4">
        <p className="text-base font-medium">{report.summary}</p>
        <div>
          <h3 className="text-xs font-medium uppercase tracking-wide text-slate-500">Root cause</h3>
          <p className="mt-1 text-sm leading-relaxed">{report.root_cause}</p>
        </div>

        <div>
          <h3 className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Evidence ({report.evidence.length})
          </h3>
          <ul className="mt-2 space-y-3">
            {report.evidence.map((ev, i) => (
              <li key={i} className="rounded-md border border-slate-200 p-3 dark:border-slate-800">
                <div className="text-xs text-slate-500">
                  from <Mono>{ev.tool_name}</Mono>
                </div>
                <blockquote className="mt-1 border-l-2 border-slate-300 pl-3 font-mono text-xs whitespace-pre-wrap [overflow-wrap:anywhere] dark:border-slate-700">
                  {ev.excerpt}
                </blockquote>
                <p className="mt-2 text-sm">{ev.why_it_matters}</p>
              </li>
            ))}
          </ul>
        </div>

        {(report.alternative_hypotheses ?? []).length > 0 && (
          <div>
            <h3 className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Alternative hypotheses
            </h3>
            <ul className="mt-1 ml-4 list-disc text-sm text-slate-600 dark:text-slate-300">
              {(report.alternative_hypotheses ?? []).map((h) => (
                <li key={h}>{h}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Card>
  );
}
