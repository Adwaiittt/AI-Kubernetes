"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import { Button, Empty, ErrorBanner, StatusBadge } from "@/components/ui";
import { api, unwrap, type IncidentStatus, type IncidentSummary } from "@/lib/api/client";
import { timeAgo } from "@/lib/format";
import { isActive, STATUS } from "@/lib/status";
import { messageOf, useResource } from "@/lib/use-resource";

const FILTERS: (IncidentStatus | "all")[] = [
  "all",
  "investigating",
  "pending_approval",
  "applied",
  "rejected",
  "failed",
];

export default function IncidentsPage() {
  const [filter, setFilter] = useState<IncidentStatus | "all">("all");
  const [anyActive, setAnyActive] = useState(false);

  const fetcher = useCallback(async (): Promise<IncidentSummary[]> => {
    const data = unwrap(
      await api.GET("/incidents", {
        params: { query: { limit: 100, ...(filter === "all" ? {} : { status: filter }) } },
      }),
    );
    setAnyActive(data.items.some((i) => isActive(i.status)));
    return data.items;
  }, [filter]);
  // Refresh quickly while anything is still in flight, slowly otherwise.
  const { data: items, error: loadError } = useResource(fetcher, anyActive ? 3000 : 15000);
  const error = messageOf(loadError);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Incidents</h1>
        <Link href="/incidents/new">
          <Button variant="primary">New investigation</Button>
        </Link>
      </div>

      <div className="flex flex-wrap gap-1" role="tablist" aria-label="Filter by status">
        {FILTERS.map((f) => (
          <button
            key={f}
            role="tab"
            aria-selected={filter === f}
            onClick={() => setFilter(f)}
            className={`rounded-full px-3 py-1 text-sm ${
              filter === f
                ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                : "text-slate-600 hover:bg-slate-200 dark:text-slate-400 dark:hover:bg-slate-800"
            }`}
          >
            {f === "all" ? "All" : STATUS[f].label}
          </button>
        ))}
      </div>

      {error && <ErrorBanner message={`Could not load incidents: ${error}`} />}

      {items === undefined && !error ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : items && items.length === 0 ? (
        <Empty>
          No incidents yet.{" "}
          <Link href="/incidents/new" className="font-medium text-blue-600 hover:underline">
            Start an investigation
          </Link>
          .
        </Empty>
      ) : (
        items && (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800">
                <tr>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Workload</th>
                  <th className="px-4 py-2 font-medium">Finding</th>
                  <th className="px-4 py-2 font-medium">Proposed fix</th>
                  <th className="px-4 py-2 font-medium">Opened</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {items.map((i) => (
                  <tr key={i.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/50">
                    <td className="px-4 py-3 whitespace-nowrap">
                      <StatusBadge status={i.status} />
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <Link href={`/incidents/${i.id}`} className="font-medium hover:underline">
                        {i.namespace}
                        {i.target && <span className="text-slate-500">/{i.target}</span>}
                      </Link>
                    </td>
                    <td className="max-w-md px-4 py-3">
                      <Link href={`/incidents/${i.id}`} className="line-clamp-2">
                        {i.summary ?? <span className="text-slate-500">{i.symptom}</span>}
                      </Link>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs whitespace-nowrap">
                      {i.proposed_action ?? <span className="text-slate-400">–</span>}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-slate-500" title={i.created_at}>
                      {timeAgo(i.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
}
