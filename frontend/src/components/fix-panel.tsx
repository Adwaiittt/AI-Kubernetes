"use client";

import { useState } from "react";

import {
  api,
  approverHeaders,
  ApiError,
  unwrap,
  type IncidentDetail,
  type PlanResult,
} from "@/lib/api/client";
import { useApproverKey } from "@/lib/approver-key";
import { formatDateTime, formatParams } from "@/lib/format";

import { ConfirmDialog } from "./confirm-dialog";
import { DiffView } from "./diff-view";
import { Button, Card, ErrorBanner, Field, Mono, StatusBadge } from "./ui";

type Pending = "dry-run" | "approve" | "reject" | null;

/** Proposed fix: dry-run diff, then Approve / Reject behind a confirm dialog. */
export function FixPanel({
  incident,
  onChanged,
}: {
  incident: IncidentDetail;
  onChanged: () => void;
}) {
  const fix = incident.report?.proposed_fix;
  const key = useApproverKey();
  const [plan, setPlan] = useState<PlanResult | null>(null);
  const [dialog, setDialog] = useState<"approve" | "reject" | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [error, setError] = useState<string | null>(null);

  if (!fix) return null;
  const dryRun = plan ?? incident.dry_run ?? null;
  const awaiting = incident.status === "pending_approval";
  const path = { params: { path: { incident_id: incident.id } } };

  async function run<T>(what: Exclude<Pending, null>, fn: () => Promise<T>): Promise<T | undefined> {
    setPending(what);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 401
          ? "The approver API key was rejected. Set a valid key in the header."
          : e instanceof Error
            ? e.message
            : String(e),
      );
      return undefined;
    } finally {
      setPending(null);
    }
  }

  const runDryRun = () =>
    run("dry-run", async () => {
      const result = unwrap(
        await api.POST("/incidents/{incident_id}/dry-run", { ...path, headers: approverHeaders(key) }),
      );
      setPlan(result);
      onChanged();
    });

  const decide = (decision: "approve" | "reject", comment: string) =>
    run(decision, async () => {
      const route =
        decision === "approve"
          ? ("/incidents/{incident_id}/approve" as const)
          : ("/incidents/{incident_id}/reject" as const);
      unwrap(
        await api.POST(route, {
          ...path,
          headers: approverHeaders(key),
          body: comment ? { comment } : {},
        }),
      );
      setDialog(null);
      onChanged();
    });

  return (
    <Card title="Proposed fix" actions={<StatusBadge status={incident.status} />}>
      <div className="space-y-4">
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Field label="Action">
            <Mono>{fix.action_type}</Mono>
          </Field>
          <Field label="Target">
            {fix.namespace}/{fix.target}
          </Field>
          <Field label="Parameters">
            <span className="font-mono text-xs">{formatParams(fix.parameters ?? {}) || "none"}</span>
          </Field>
        </dl>
        <Field label="Rationale">{fix.rationale}</Field>
        <Field label="Risk">
          <span className="text-amber-800 dark:text-amber-300">{fix.risk}</span>
        </Field>

        {error && <ErrorBanner message={error} />}

        {dryRun ? (
          <div>
            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">
              Server-side dry run: exact change to be applied
            </h3>
            <DiffView diff={dryRun.diff} />
          </div>
        ) : (
          awaiting && (
            <p className="text-sm text-slate-600 dark:text-slate-300">
              Run a server-side dry run to see the exact patch before deciding.
            </p>
          )
        )}

        {awaiting && (
          <div className="flex flex-wrap items-center gap-2 border-t border-slate-200 pt-4 dark:border-slate-800">
            {!key && (
              <p className="w-full text-sm text-amber-700 dark:text-amber-300">
                Set your approver API key (top right) to dry-run, approve or reject.
              </p>
            )}
            <Button onClick={runDryRun} disabled={!key || pending !== null}>
              {pending === "dry-run" ? "Running dry run…" : dryRun ? "Re-run dry run" : "Run dry run"}
            </Button>
            <div className="flex-1" />
            <Button
              variant="ghost"
              onClick={() => setDialog("reject")}
              disabled={!key || pending !== null}
            >
              Reject
            </Button>
            <Button
              variant="primary"
              onClick={() => setDialog("approve")}
              disabled={!key || !dryRun || pending !== null}
              title={dryRun ? undefined : "Run a dry run first"}
            >
              Approve…
            </Button>
          </div>
        )}

        {incident.approval && (
          <p className="text-sm text-slate-600 dark:text-slate-300">
            {incident.approval.decision === "approved" ? "Approved" : "Rejected"} by{" "}
            <strong>{incident.approval.actor}</strong> on {formatDateTime(incident.approval.created_at)}
            {incident.approval.comment && <>: “{incident.approval.comment}”</>}
          </p>
        )}

        {incident.execution && <ExecutionResult incident={incident} />}
      </div>

      <ConfirmDialog
        open={dialog === "approve"}
        title="Apply this change to the cluster?"
        confirmLabel="Approve and apply"
        busy={pending === "approve"}
        onCancel={() => setDialog(null)}
        onConfirm={(c) => decide("approve", c)}
      >
        <p>
          The executor will apply <Mono>{fix.action_type}</Mono> to{" "}
          <strong>
            {fix.namespace}/{fix.target}
          </strong>{" "}
          exactly as shown in the dry run. The approval is single-use and is recorded in the audit
          log under your name.
        </p>
        <p className="mt-2 text-amber-800 dark:text-amber-300">Risk: {fix.risk}</p>
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "reject"}
        title="Reject the proposed fix?"
        confirmLabel="Reject"
        variant="danger"
        busy={pending === "reject"}
        onCancel={() => setDialog(null)}
        onConfirm={(c) => decide("reject", c)}
      >
        Nothing will be changed in the cluster. The incident is closed as rejected.
      </ConfirmDialog>
    </Card>
  );
}

function ExecutionResult({ incident }: { incident: IncidentDetail }) {
  const rollout = incident.rollout_status ?? {};
  const complete = rollout["rollout_complete"];
  return (
    <div className="space-y-2 border-t border-slate-200 pt-4 dark:border-slate-800">
      <h3 className="text-xs font-medium uppercase tracking-wide text-slate-500">Applied change</h3>
      <DiffView diff={incident.execution?.diff ?? ""} />
      <p className="text-sm">
        Rollout after apply:{" "}
        {complete === true ? (
          <span className="font-medium text-emerald-700 dark:text-emerald-300">complete</span>
        ) : complete === false ? (
          <span className="font-medium text-amber-700 dark:text-amber-300">still progressing</span>
        ) : (
          <span className="text-slate-500">unknown</span>
        )}
        {typeof rollout["available_replicas"] === "number" &&
          ` (${String(rollout["available_replicas"])}/${String(rollout["desired_replicas"] ?? "?")} available)`}
      </p>
    </div>
  );
}
