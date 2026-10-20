"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useState } from "react";

import { FixPanel } from "@/components/fix-panel";
import { ReportCard } from "@/components/report-card";
import { Timeline } from "@/components/timeline";
import { Card, ErrorBanner, Field, StatusBadge } from "@/components/ui";
import { api, ApiError, unwrap, type IncidentDetail } from "@/lib/api/client";
import { compactNumber, formatDateTime, formatDuration } from "@/lib/format";
import { isActive } from "@/lib/status";
import { buildTimeline } from "@/lib/timeline";
import { useIncidentStream } from "@/lib/use-incident-stream";
import { messageOf, useResource } from "@/lib/use-resource";

export default function IncidentPage() {
  return (
    <Suspense fallback={<p className="text-sm text-slate-500">Loading…</p>}>
      <Incident />
    </Suspense>
  );
}

function Incident() {
  const { id } = useParams<{ id: string }>();
  const [applying, setApplying] = useState(false);

  const fetcher = useCallback(async (): Promise<IncidentDetail> => {
    const detail = unwrap(
      await api.GET("/incidents/{incident_id}", { params: { path: { incident_id: id } } }),
    );
    setApplying(detail.status === "approved" || detail.status === "applying");
    return detail;
  }, [id]);
  // While the executor is applying, poll until a terminal state.
  const { data: incident, error: loadError, reload } = useResource(fetcher, applying ? 2000 : null);
  const notFound = loadError instanceof ApiError && loadError.status === 404;
  const error = notFound ? null : messageOf(loadError);

  // Live trace while investigating; when it ends, fetch the final report.
  const investigating = incident?.status === "investigating";
  const stream = useIncidentStream(id, investigating, reload);

  // Live stream while investigating (it replays history first); persisted trace otherwise.
  const persisted = incident?.trace;
  const timeline = useMemo(() => {
    const trace = persisted ?? [];
    return buildTimeline(stream.events.length > trace.length ? stream.events : trace);
  }, [stream.events, persisted]);

  if (notFound) {
    return (
      <p className="text-sm">
        Incident not found.{" "}
        <Link href="/" className="text-blue-600 hover:underline">
          Back to incidents
        </Link>
      </p>
    );
  }
  if (!incident) {
    return error ? <ErrorBanner message={error} /> : <p className="text-sm text-slate-500">Loading…</p>;
  }

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Link href="/" className="text-sm text-slate-500 hover:underline">
          ← Incidents
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold">
            {incident.namespace}
            {incident.target && <span className="text-slate-500">/{incident.target}</span>}
          </h1>
          <StatusBadge status={incident.status} />
        </div>
        <p className="text-sm text-slate-600 dark:text-slate-300">{incident.symptom}</p>
      </div>

      {error && <ErrorBanner message={error} />}
      {incident.status === "failed" && incident.error && (
        <ErrorBanner message={`Failed: ${incident.error}`} />
      )}

      <div className="grid gap-6 lg:grid-cols-5">
        <div className="min-w-0 space-y-6 lg:col-span-3">
          {incident.report && <ReportCard report={incident.report} />}
          {incident.report?.proposed_fix && <FixPanel incident={incident} onChanged={reload} />}
          {incident.report && !incident.report.proposed_fix && (
            <Card>
              <p className="text-sm text-slate-600 dark:text-slate-300">
                The agent did not propose an automated fix: none of the allowed actions would
                safely resolve this. See the root cause for the recommended manual change.
              </p>
            </Card>
          )}
          <Card
            title="Agent timeline"
            actions={
              investigating && (
                <span className="text-xs text-slate-500">
                  {stream.connected ? "● live" : "connecting…"}
                </span>
              )
            }
          >
            <Timeline items={timeline} live={investigating} />
          </Card>
        </div>

        <div className="min-w-0 lg:col-span-2">
          <Card title="Details">
            <dl className="grid grid-cols-2 gap-4">
              <Field label="Opened">{formatDateTime(incident.created_at)}</Field>
              <Field label="Updated">{formatDateTime(incident.updated_at)}</Field>
              <Field label="Model">
                <span className="font-mono text-xs">{incident.model ?? "–"}</span>
              </Field>
              <Field label="Latency">{formatDuration(incident.latency_ms)}</Field>
              <Field label="Tool calls">{incident.tool_calls}</Field>
              <Field label="Model turns">{incident.model_turns}</Field>
              <Field label="Input tokens">{compactNumber(incident.usage.input_tokens)}</Field>
              <Field label="Output tokens">{compactNumber(incident.usage.output_tokens)}</Field>
              <Field label="Cache reads">
                {compactNumber(incident.usage.cache_read_input_tokens)}
              </Field>
              <Field label="Cache writes">
                {compactNumber(incident.usage.cache_creation_input_tokens)}
              </Field>
            </dl>
            <p className="mt-4 text-xs text-slate-500">
              ID <span className="font-mono">{incident.id}</span> ·{" "}
              <Link href={`/audit?incident=${incident.id}`} className="text-blue-600 hover:underline">
                audit trail
              </Link>
              {isActive(incident.status) && " · refreshing"}
            </p>
          </Card>
        </div>
      </div>
    </div>
  );
}
