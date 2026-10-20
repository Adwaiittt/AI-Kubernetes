"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Card, ErrorBanner } from "@/components/ui";
import { api, unwrap } from "@/lib/api/client";

// HTML `pattern` is compiled with the regex `v` flag, where a literal "-" in a class must be
// escaped; an invalid pattern is silently ignored by the browser.
const DNS_LABEL = "[a-z0-9]([a-z0-9\\-]*[a-z0-9])?";

export default function NewInvestigationPage() {
  const router = useRouter();
  const [namespace, setNamespace] = useState("demo");
  const [target, setTarget] = useState("");
  const [symptom, setSymptom] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const incident = unwrap(
        await api.POST("/incidents", {
          body: { namespace: namespace.trim(), symptom: symptom.trim(), target: target.trim() || null },
        }),
      );
      // Next keeps this page alive for back-navigation, so leave it ready for the next one.
      setSymptom("");
      router.push(`/incidents/${incident.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const input =
    "mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950";

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <h1 className="text-xl font-semibold">New investigation</h1>
      <Card>
        <form onSubmit={submit} className="space-y-4">
          <p className="text-sm text-slate-600 dark:text-slate-300">
            The agent investigates with read-only tools and proposes at most one fix. Nothing changes
            in the cluster until an approver reviews the dry run and approves it.
          </p>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm font-medium">
              Namespace
              <input
                required
                pattern={DNS_LABEL}
                maxLength={63}
                value={namespace}
                onChange={(e) => setNamespace(e.target.value)}
                className={input}
              />
            </label>
            <label className="block text-sm font-medium">
              Target workload <span className="font-normal text-slate-500">(optional)</span>
              <input
                pattern={`${DNS_LABEL}(\\.${DNS_LABEL})*`}
                maxLength={253}
                placeholder="e.g. checkout"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                className={input}
              />
            </label>
          </div>
          <label className="block text-sm font-medium">
            Symptom
            <textarea
              required
              minLength={3}
              maxLength={4000}
              rows={4}
              placeholder="What's going wrong? e.g. checkout pods restart every few minutes and users see 502s"
              value={symptom}
              onChange={(e) => setSymptom(e.target.value)}
              className={input}
            />
          </label>
          {error && <ErrorBanner message={error} />}
          <div className="flex justify-end">
            <Button type="submit" variant="primary" disabled={submitting}>
              {submitting ? "Starting…" : "Start investigation"}
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
