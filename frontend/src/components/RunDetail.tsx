import type { PlanStep, RunDetail as RunDetailData, TraceEvent } from "../types";
import { isTerminal, providerLabel } from "../types";
import { StatusBadge } from "./StatusBadge";
import { TraceTimeline } from "./TraceTimeline";

function waves(steps: PlanStep[]): PlanStep[][] {
  const done = new Set<string>();
  const pending = [...steps];
  const levels: PlanStep[][] = [];
  while (pending.length > 0) {
    const ready = pending.filter((step) =>
      step.depends_on.every((dependency) => done.has(dependency)),
    );
    if (ready.length === 0) {
      levels.push(pending);
      break;
    }
    levels.push(ready);
    for (const step of ready) done.add(step.id);
    for (const step of ready) {
      const index = pending.indexOf(step);
      if (index >= 0) pending.splice(index, 1);
    }
  }
  return levels;
}

function stepStates(events: TraceEvent[]): Map<string, "ok" | "failed"> {
  const states = new Map<string, "ok" | "failed">();
  for (const event of events) {
    const payload = event.payload as Record<string, any>;
    if (event.kind === "agent_invocation" && payload.role === "coder") {
      const stepId = payload.step_id as string | undefined;
      if (stepId && payload.status === "succeeded") states.set(stepId, "ok");
    }
    if (event.kind === "failure") {
      const stepId = payload.step_id as string | undefined;
      if (stepId && !states.has(stepId)) states.set(stepId, "failed");
    }
  }
  return states;
}

function percent(value: number, max: number): number {
  if (!max || max <= 0) return 0;
  return Math.max(0, Math.min(100, (value / max) * 100));
}

interface UsageTotals {
  inputTokens: number;
  outputTokens: number;
  available: boolean;
  retries: number;
}

function usageTotals(events: TraceEvent[]): UsageTotals {
  const invocations = events
    .filter((event) => event.kind === "agent_invocation")
    .map((event) => event.payload as Record<string, any>);
  return {
    inputTokens: invocations.reduce(
      (total, item) => total + Number(item.input_tokens ?? 0),
      0,
    ),
    outputTokens: invocations.reduce(
      (total, item) => total + Number(item.output_tokens ?? 0),
      0,
    ),
    available: invocations.some((item) => Boolean(item.usage_available)),
    retries: invocations.filter((item) => Number(item.attempt ?? 1) > 1).length,
  };
}

function downloadTrace(detail: RunDetailData): void {
  const blob = new Blob([JSON.stringify(detail, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `swarmos-${detail.run.run_id}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function RunDetail({ detail }: { detail: RunDetailData }) {
  const run = detail.run;
  const usage = usageTotals(detail.events);
  const planWaves = run.plan ? waves(run.plan.steps) : [];
  const states = stepStates(detail.events);
  const budgetPct = percent(run.cost_usd, run.limits.max_budget_usd);
  const callPct = percent(run.agent_calls, run.limits.max_agent_calls);
  const live = !isTerminal(run.status);

  return (
    <div className="detail">
      <header className="detail-head">
        <div className="detail-title">
          <div className="detail-topline">
            <StatusBadge status={run.status} />
            <span className="mono muted">{run.run_id}</span>
            <span className={`mode-tag ${run.provider_kind}`}>
              {providerLabel(run.provider_kind)}
            </span>
            <span className="muted mono small">{run.provider}</span>
            {live && <span className="live">live</span>}
          </div>
          <h1>{run.goal}</h1>
        </div>
        <div className="detail-metrics">
          <div className="metric">
            <span>Duration</span>
            <strong>{(run.duration_ms / 1000).toFixed(1)}s</strong>
          </div>
          <div className="metric">
            <span>Agent calls</span>
            <strong>
              {run.agent_calls}
              <small>/{run.limits.max_agent_calls}</small>
            </strong>
          </div>
          <div className="metric">
            <span>Cost</span>
            <strong>
              ${run.cost_usd.toFixed(6)}
              <small>/{run.limits.max_budget_usd.toFixed(2)}</small>
            </strong>
          </div>
          <div className="metric">
            <span>Changed files</span>
            <strong>{run.changed_files.length}</strong>
          </div>
        </div>
      </header>

      {run.provider_warning && (
        <div className="callout warning">{run.provider_warning}</div>
      )}
      {run.provider_kind === "demo" ? (
        <div className="callout warning">
          Demo fixture: model outputs are scripted, no inference happened. The
          plan, coding, and review text are simulated; verification and the trace
          are real.
        </div>
      ) : (
        <div className="callout success">
          {providerLabel(run.provider_kind)}: every model invocation below was
          sent to the configured endpoint. Verification ran real tests.
        </div>
      )}
      {run.error && <div className="callout danger">{run.error}</div>}

      <section className="panel">
        <div className="panel-head">
          <h2>Model routing</h2>
          <span className="muted mono">{run.model_policy}</span>
        </div>
        <div className="limit-chips">
          {Object.entries(run.models_by_role).map(([role, model]) => (
            <span className="chip-static" key={role}>
              {role} → <span className="mono">{model}</span>
            </span>
          ))}
        </div>
        <div className="limit-chips">
          <span className={`chip-static ${run.monetary_enforced ? "ok" : "danger"}`}>
            {run.monetary_enforced
              ? "USD budget enforced"
              : "USD budget not enforced (pricing unknown)"}
          </span>
          <span className={`chip-static ${run.usage_available ? "ok" : ""}`}>
            {run.usage_available
              ? "token usage reported"
              : "token usage unavailable"}
          </span>
          <span className="chip-static">retries: {usage.retries}</span>
          <span className={`chip-static ${usage.available ? "ok" : ""}`}>
            tokens:{" "}
            {usage.available
              ? `${usage.inputTokens} in / ${usage.outputTokens} out`
              : "unavailable"}
          </span>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Autonomy envelope</h2>
          <span className="muted">
            {run.escalations} escalation(s) · {run.revisions} revision round(s)
          </span>
        </div>
        <div className="meters">
          <div className="meter-row">
            <span>Calls</span>
            <div className="meter">
              <div style={{ width: `${callPct}%` }} />
            </div>
            <span className="mono">
              {run.agent_calls}/{run.limits.max_agent_calls}
            </span>
          </div>
          <div className="meter-row">
            <span>{run.monetary_enforced ? "Budget" : "Cost*"}</span>
            <div className="meter">
              <div style={{ width: `${budgetPct}%` }} />
            </div>
            <span className="mono">
              ${run.cost_usd.toFixed(6)}/${run.limits.max_budget_usd.toFixed(2)}
            </span>
          </div>
        </div>
        <div className="limit-chips">
          <span className="chip-static">
            max {run.limits.max_retries} retries/step
          </span>
          <span className="chip-static">
            max {run.limits.max_revisions} revision rounds
          </span>
          <span className="chip-static">
            max {run.limits.max_escalations} escalation(s)
          </span>
          <span className="chip-static">{run.limits.max_wall_seconds}s wall clock</span>
        </div>
      </section>

      {run.plan && (
        <section className="panel">
          <div className="panel-head">
            <h2>Plan</h2>
            <span className="muted">
              {run.plan.steps.length} step(s) · {planWaves.length} wave(s)
            </span>
          </div>
          <div className="waves">
            {planWaves.map((wave, index) => (
              <div className="wave" key={index}>
                <div className="wave-label">
                  Wave {index + 1}
                  {wave.length > 1 ? <span className="accent"> · parallel</span> : null}
                </div>
                {wave.map((step) => {
                  const state = states.get(step.id);
                  return (
                    <article className={`step ${state ?? "pending"}`} key={step.id}>
                      <div className="step-head">
                        <span className="mono step-id">{step.id}</span>
                        <span className={`step-state ${state ?? "pending"}`}>
                          {state === "ok"
                            ? "done"
                            : state === "failed"
                              ? "failed"
                              : "not run"}
                        </span>
                      </div>
                      <h3>{step.title}</h3>
                      <p>{step.detail}</p>
                      {step.target_files.length > 0 && (
                        <p className="mono small">
                          targets: {step.target_files.join(", ")}
                        </p>
                      )}
                      <ul className="criteria">
                        {step.acceptance_criteria.map((criterion) => (
                          <li key={criterion}>{criterion}</li>
                        ))}
                      </ul>
                    </article>
                  );
                })}
              </div>
            ))}
          </div>
          {run.plan.assumptions.length > 0 && (
            <details className="raw">
              <summary>assumptions</summary>
              <ul>
                {run.plan.assumptions.map((assumption) => (
                  <li key={assumption}>{assumption}</li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>Review</h2>
          {run.review ? (
            <span className={`verdict ${run.review.verdict}`}>
              {run.review.verdict === "approve" ? "approved" : "changes requested"}
            </span>
          ) : (
            <span className="muted">not reached</span>
          )}
        </div>
        {run.review ? (
          <>
            <p className="muted">{run.review.rationale}</p>
            {run.review.findings.length > 0 ? (
              <ul className="findings">
                {run.review.findings.map((finding, index) => (
                  <li key={`${finding.message}-${index}`} className={finding.severity}>
                    <span className="severity">{finding.severity}</span>
                    <div>
                      <p>{finding.message}</p>
                      <span className="mono muted">
                        {[finding.step_id, finding.file].filter(Boolean).join(" · ")}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted small">No findings.</p>
            )}
          </>
        ) : (
          <p className="muted">The review agent has not run yet.</p>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>Verification</h2>
          {run.verification ? (
            <span className={`verdict ${run.verification.passed ? "approve" : "reject"}`}>
              {run.verification.passed ? "passed" : "failed"}
            </span>
          ) : (
            <span className="muted">not run</span>
          )}
        </div>
        {run.verification ? (
          <>
            {run.verification.diagnostic && (
              <p className="hint">
                Diagnostic only: the reviewer did not approve these changes, so
                this result is evidence, not an approval.
              </p>
            )}
            <div className="verify-grid">
              <div>
                <span>Command</span>
                <code>{run.verification.command}</code>
              </div>
              <div>
                <span>Exit code</span>
                <strong className={run.verification.exit_code === 0 ? "ok" : "danger"}>
                  {run.verification.exit_code}
                </strong>
              </div>
              <div>
                <span>Tests passed</span>
                <strong>{run.verification.tests_passed}</strong>
              </div>
              <div>
                <span>Tests failed</span>
                <strong>{run.verification.tests_failed}</strong>
              </div>
            </div>
            {run.verification.stdout_tail && (
              <details className="raw" open={!run.verification.passed}>
                <summary>stdout</summary>
                <pre>{run.verification.stdout_tail}</pre>
              </details>
            )}
            {run.verification.stderr_tail && (
              <details className="raw" open>
                <summary>stderr</summary>
                <pre>{run.verification.stderr_tail}</pre>
              </details>
            )}
          </>
        ) : (
          <p className="muted">
            Verification runs only after the review agent approves the changes.
          </p>
        )}
      </section>

      {Object.keys(run.artifacts).length > 0 && (
        <section className="panel">
          <div className="panel-head">
            <h2>Artifacts</h2>
            <span className="muted">{Object.keys(run.artifacts).length} file(s)</span>
          </div>
          {Object.entries(run.artifacts).map(([path, content]) => (
            <details className="artifact" key={path}>
              <summary className="mono">{path}</summary>
              <pre>{content}</pre>
            </details>
          ))}
        </section>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>Trace</h2>
          <div className="panel-actions">
            <span className="muted">{detail.events.length} events</span>
            <button
              type="button"
              className="link-button"
              onClick={() => downloadTrace(detail)}
            >
              download JSON
            </button>
          </div>
        </div>
        <div className="trace-scroll">
          <TraceTimeline events={detail.events} />
        </div>
      </section>

      {run.summary && (
        <section className="panel">
          <div className="panel-head">
            <h2>Summary</h2>
          </div>
          <pre className="summary">{run.summary}</pre>
        </section>
      )}
    </div>
  );
}
