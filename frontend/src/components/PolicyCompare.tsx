import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { StatusBadge } from "./StatusBadge";
import type { AppConfig, RunDetail, RunRecord } from "../types";
import { isTerminal } from "../types";

interface Props {
  goal: string;
  runs: RunRecord[];
  config: AppConfig;
  onSelectRun: (runId: string) => void;
  onClose: () => void;
}

const POLICY_ORDER = ["fast_only", "adaptive", "role_routed"] as const;

const POLICY_LABELS: Record<string, string> = {
  fast_only: "Fixed fast",
  adaptive: "Adaptive",
  role_routed: "Role-routed",
  config: "Configured",
};

interface Row {
  run: RunRecord;
  inputTokens: number;
  outputTokens: number;
  usageAvailable: boolean;
  retries: number;
  escalations: number;
  revisions: number;
  verified: boolean | null;
  diagnostic: boolean;
}

function asRecord(value: unknown): Record<string, any> {
  return (value ?? {}) as Record<string, any>;
}

function buildRow(detail: RunDetail): Row {
  const invocations = detail.events
    .filter((event) => event.kind === "agent_invocation")
    .map((event) => asRecord(event.payload));
  const run = detail.run;
  return {
    run,
    inputTokens: invocations.reduce(
      (total, item) => total + Number(item.input_tokens ?? 0),
      0,
    ),
    outputTokens: invocations.reduce(
      (total, item) => total + Number(item.output_tokens ?? 0),
      0,
    ),
    usageAvailable: invocations.some((item) => Boolean(item.usage_available)),
    retries: invocations.filter((item) => Number(item.attempt ?? 1) > 1).length,
    escalations: run.escalations,
    revisions: run.revisions,
    verified: run.verification ? run.verification.passed : null,
    diagnostic: Boolean(run.verification?.diagnostic),
  };
}

function cost(row: Row): string {
  if (!row.usageAvailable || !row.run.monetary_enforced) return "not measurable";
  return `$${row.run.cost_usd.toFixed(6)}`;
}

/**
 * Compares the same goal under each orchestration policy.
 *
 * Every policy runs the identical graph and differs only in model selection and
 * escalation, so the table is a routing comparison. It never declares a
 * winner, and it warns when the escalation knob cannot actually move.
 */
export function PolicyCompare({ goal, runs, config, onSelectRun, onClose }: Props) {
  const [details, setDetails] = useState<Record<string, RunDetail>>({});
  const [error, setError] = useState<string | null>(null);

  const runIds = useMemo(() => runs.map((run) => run.run_id).join(","), [runs]);

  useEffect(() => {
    if (!runIds) return;
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      const ids = runIds.split(",");
      const fetched = await Promise.all(
        ids.map((id) => api.getRun(id).catch(() => null)),
      );
      if (cancelled) return;
      if (fetched.some((detail) => detail === null)) {
        setError("Some comparison runs could not be refreshed; retrying…");
      } else {
        setError(null);
      }
      const next: Record<string, RunDetail> = {};
      for (const detail of fetched) {
        if (detail) next[detail.run.run_id] = detail;
      }
      setDetails((current) => ({ ...current, ...next }));
      const allDone = fetched.every(
        (detail) => detail && isTerminal(detail.run.status),
      );
      if (!allDone) {
        timer = window.setTimeout(tick, 800);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [runIds]);

  const rows = useMemo(() => {
    return [...runs]
      .sort((a, b) => {
        const indexA = POLICY_ORDER.indexOf(
          a.model_policy as (typeof POLICY_ORDER)[number],
        );
        const indexB = POLICY_ORDER.indexOf(
          b.model_policy as (typeof POLICY_ORDER)[number],
        );
        return (indexA < 0 ? 99 : indexA) - (indexB < 0 ? 99 : indexB);
      })
      .map((run) => ({ run }));
  }, [runs]);

  const measured = rows
    .map((row) => details[row.run.run_id])
    .filter((detail): detail is RunDetail => Boolean(detail))
    .map((detail) => buildRow(detail));
  const byId = new Map(measured.map((row) => [row.run.run_id, row]));

  const allDemo = rows.every(
    (row) =>
      (details[row.run.run_id]?.run ?? row.run).provider_kind === "demo",
  );
  const pending = rows.some(
    (row) => !isTerminal((details[row.run.run_id]?.run ?? row.run).status),
  );

  return (
    <div className="detail">
      <header className="detail-head">
        <div className="detail-title">
          <div className="detail-topline">
            <span className="muted mono">policy comparison</span>
            {pending && <span className="live">running</span>}
          </div>
          <h1>{goal}</h1>
        </div>
        <div className="detail-actions">
          <button type="button" className="secondary small" onClick={onClose}>
            Back to run
          </button>
        </div>
      </header>

      {!config.escalation_available && (
        <div className="callout warning">
          No distinct strong model is configured, so the adaptive and role-routed
          policies cannot select a stronger model. Any equal results below are
          identical by construction, not evidence that the policies perform
          equivalently.
        </div>
      )}
      {allDemo && (
        <div className="callout warning">
          All three runs used the demo fixture. Orchestration, verification, and
          traces are real; the model reasoning is scripted.
        </div>
      )}
      {error && <div className="callout danger">{error}</div>}

      <p className="hint">
        This comparison started three independent runs with the same goal and
        limits. With a priced provider each run has its own soft cap, so total
        spend can be up to three times a single run.
      </p>

      <section className="panel">
        <div className="panel-head">
          <h2>Results</h2>
          <span className="muted">
            same goal, same limits, three policies
          </span>
        </div>

        <div className="compare-wrap">
          <table className="compare-table">
            <thead>
              <tr>
                <th>Policy</th>
                <th>Status</th>
                <th>Verified</th>
                <th>Calls</th>
                <th>Retries</th>
                <th>Escal.</th>
                <th>Rev.</th>
                <th>Time</th>
                <th>Tokens</th>
                <th>Cost</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const measuredRow = byId.get(row.run.run_id);
                const run = measuredRow?.run ?? row.run;
                return (
                  <tr key={run.run_id}>
                    <td>
                      <div className="policy-name">
                        {POLICY_LABELS[run.model_policy] ?? run.model_policy}
                      </div>
                      <div className="muted mono small policy-models">
                        {Object.entries(run.models_by_role)
                          .filter(([role]) => role !== "verifier")
                          .map(([role, model]) => `${role}: ${model}`)
                          .join(" · ")}
                      </div>
                    </td>
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td>
                      {measuredRow?.verified === null || !measuredRow ? (
                        <span className="muted">not run</span>
                      ) : measuredRow.verified ? (
                        <span className="chip-static ok">passed</span>
                      ) : (
                        <span className="chip-static danger">
                          failed
                          {measuredRow.diagnostic ? " (diag)" : ""}
                        </span>
                      )}
                    </td>
                    <td className="mono">{run.agent_calls}</td>
                    <td className="mono">{measuredRow?.retries ?? 0}</td>
                    <td className="mono">{run.escalations}</td>
                    <td className="mono">{run.revisions}</td>
                    <td className="mono">{(run.duration_ms / 1000).toFixed(1)}s</td>
                    <td className="mono">
                      {measuredRow && measuredRow.usageAvailable
                        ? `${measuredRow.inputTokens}/${measuredRow.outputTokens}`
                        : "n/a"}
                    </td>
                    <td className="mono">
                      {measuredRow ? cost(measuredRow) : "—"}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => onSelectRun(run.run_id)}
                      >
                        open
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <p className="hint">
          All policies run the same orchestration graph and differ only in model
          selection and escalation. Token and cost figures appear only when the
          provider reported usage and pricing is configured. No policy is
          declared best.
        </p>
      </section>
    </div>
  );
}
