"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { Empty, ErrorBanner } from "@/components/ui";
import { api, unwrap, type AuditEntry } from "@/lib/api/client";
import { formatDateTime } from "@/lib/format";

export default function AuditPage() {
  return (
    <Suspense fallback={<p className="text-sm text-slate-500">Loading…</p>}>
      <AuditLog />
    </Suspense>
  );
}

function AuditLog() {
  const incidentId = useSearchParams().get("incident");
  const [items, setItems] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = unwrap(
          await api.GET("/audit", {
            params: { query: { limit: 500, ...(incidentId ? { incident_id: incidentId } : {}) } },
          }),
        );
        if (!cancelled) setItems(data.items);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [incidentId]);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Audit log</h1>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          Append-only record of every state transition: who, what, when, before and after.
          {incidentId && (
            <>
              {" "}
              Showing incident <span className="font-mono">{incidentId.slice(0, 8)}</span> ·{" "}
              <Link href="/audit" className="text-blue-600 hover:underline">
                show all
              </Link>
            </>
          )}
        </p>
      </div>
      {error && <ErrorBanner message={error} />}
      {items && items.length === 0 && <Empty>No audit entries.</Empty>}
      {items && items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800">
              <tr>
                <th className="px-4 py-2 font-medium">When</th>
                <th className="px-4 py-2 font-medium">Actor</th>
                <th className="px-4 py-2 font-medium">Action</th>
                <th className="px-4 py-2 font-medium">Transition</th>
                <th className="px-4 py-2 font-medium">Incident</th>
                <th className="px-4 py-2 font-medium">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 align-top dark:divide-slate-800">
              {items.map((a) => (
                <AuditRow key={a.id} entry={a} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AuditRow({ entry: a }: { entry: AuditEntry }) {
  const [open, setOpen] = useState(false);
  const hasPayload = a.before || a.after || a.detail;
  return (
    <>
      <tr>
        <td className="px-4 py-2 whitespace-nowrap text-slate-500">{formatDateTime(a.at)}</td>
        <td className="px-4 py-2 font-medium whitespace-nowrap">{a.actor}</td>
        <td className="px-4 py-2 font-mono text-xs whitespace-nowrap">{a.action}</td>
        <td className="px-4 py-2 text-xs whitespace-nowrap text-slate-600 dark:text-slate-300">
          {a.from_status || a.to_status ? `${a.from_status ?? "–"} → ${a.to_status ?? "–"}` : "–"}
        </td>
        <td className="px-4 py-2 font-mono text-xs">
          {a.incident_id ? (
            <Link href={`/incidents/${a.incident_id}`} className="text-blue-600 hover:underline">
              {a.incident_id.slice(0, 8)}
            </Link>
          ) : (
            "–"
          )}
        </td>
        <td className="px-4 py-2">
          {hasPayload && (
            <button
              type="button"
              onClick={() => setOpen((o) => !o)}
              className="text-xs font-medium text-blue-600 hover:underline dark:text-blue-400"
            >
              {open ? "Hide" : "Show"}
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={6} className="bg-slate-50 px-4 py-3 dark:bg-slate-950">
            <div className="grid gap-3 md:grid-cols-2">
              {(["before", "after"] as const).map((k) => (
                <div key={k}>
                  <div className="text-xs font-medium uppercase text-slate-500">{k}</div>
                  <pre className="mt-1 max-h-64 overflow-auto font-mono text-xs">
                    {a[k] ? JSON.stringify(a[k], null, 2) : "–"}
                  </pre>
                </div>
              ))}
              {a.detail && (
                <div className="md:col-span-2">
                  <div className="text-xs font-medium uppercase text-slate-500">detail</div>
                  <pre className="mt-1 max-h-64 overflow-auto font-mono text-xs whitespace-pre-wrap">
                    {a.detail}
                  </pre>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
