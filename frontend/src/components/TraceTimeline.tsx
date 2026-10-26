import { useMemo, useState } from "react";
import type { TraceEvent } from "../types";

const KIND_LABELS: Record<string, string> = {
  run_started: "run started",
  agent_invocation: "agent invocation",
  routing_decision: "routing",
  verification: "verification",
  failure: "failure",
  retry: "retry",
  escalation: "escalation",
  revision: "revision",
  budget: "budget",
  run_finished: "run finished",
};

const FILTERS = [
  { id: "all", label: "All" },
  { id: "agent_invocation", label: "Invocations" },
  { id: "routing_decision", label: "Routing" },
  { id: "verification", label: "Verification" },
  { id: "issues", label: "Issues" },
];

const ISSUE_KINDS = new Set(["failure", "budget", "escalation", "retry"]);

function clock(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function asRecord(value: unknown): Record<string, any> {
  return (value ?? {}) as Record<string, any>;
}

function Summary({ event }: { event: TraceEvent }) {
  const payload = asRecord(event.payload);
  switch (event.kind) {
    case "agent_invocation":
      return (
        <div className="chips">
          <span className="chip-static">{payload.role}</span>
          <span className="chip-static mono">{payload.model}</span>
          <span className="chip-static">{payload.tier}</span>
          <span className="chip-static">attempt {payload.attempt}</span>
          <span className="chip-static">{payload.latency_ms} ms</span>
          <span className="chip-static">
            {(payload.input_tokens ?? 0) + (payload.output_tokens ?? 0)} tok
          </span>
          <span className="chip-static">${Number(payload.cost_usd ?? 0).toFixed(6)}</span>
          {payload.error ? (
            <span className="chip-static danger">{String(payload.error).slice(0, 90)}</span>
          ) : null}
        </div>
      );
    case "routing_decision":
      return (
        <div className="chips">
          <span className="chip-static mono">{payload.decision}</span>
          {payload.parallel ? <span className="chip-static accent">parallel</span> : null}
          {Array.isArray(payload.steps) && payload.steps.length > 0 ? (
            <span className="chip-static">{payload.steps.join(", ")}</span>
          ) : null}
          {payload.reason ? (
            <span className="chip-static muted">{String(payload.reason)}</span>
          ) : null}
        </div>
      );
    case "verification":
      return (
        <div className="chips">
          <span className={`chip-static ${payload.passed ? "ok" : "danger"}`}>
            exit {payload.exit_code}
          </span>
          <span className="chip-static">{payload.tests_passed} passed</span>
          <span className="chip-static">{payload.tests_failed} failed</span>
          <span className="chip-static">{payload.duration_ms} ms</span>
          <span className="chip-static mono">{payload.command}</span>
        </div>
      );
    case "failure":
      return (
        <div className="chips">
          {payload.step_id ? (
            <span className="chip-static">{String(payload.step_id)}</span>
          ) : null}
          {payload.error ? (
            <span className="chip-static danger">{String(payload.error).slice(0, 160)}</span>
          ) : null}
        </div>
      );
    case "escalation":
      return (
        <div className="chips">
          <span className="chip-static accent">
            {String(payload.from_tier)} → {String(payload.to_tier)}
          </span>
          <span className="chip-static">round {String(payload.round)}</span>
        </div>
      );
    default:
      return null;
  }
}

export function TraceTimeline({ events }: { events: TraceEvent[] }) {
  const [filter, setFilter] = useState("all");

  const counts = useMemo(() => {
    const map: Record<string, number> = { all: events.length };
    for (const event of events) {
      map[event.kind] = (map[event.kind] ?? 0) + 1;
      if (ISSUE_KINDS.has(event.kind)) map.issues = (map.issues ?? 0) + 1;
    }
    return map;
  }, [events]);

  const visible = useMemo(() => {
    if (filter === "all") return events;
    if (filter === "issues") return events.filter((event) => ISSUE_KINDS.has(event.kind));
    return events.filter((event) => event.kind === filter);
  }, [events, filter]);

  return (
    <div className="trace">
      <div className="filters">
        {FILTERS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`filter ${filter === item.id ? "active" : ""}`}
            onClick={() => setFilter(item.id)}
          >
            {item.label}
            <span className="count">{counts[item.id] ?? 0}</span>
          </button>
        ))}
      </div>

      <ol className="timeline">
        {visible.map((event) => (
          <li key={event.event_id} className={`event kind-${event.kind}`}>
            <span className="event-dot" aria-hidden="true" />
            <div className="event-body">
              <div className="event-head">
                <span className="event-kind">{KIND_LABELS[event.kind] ?? event.kind}</span>
                <span className="mono muted">
                  #{String(event.seq).padStart(2, "0")} · {clock(event.at)}
                </span>
              </div>
              <p className="event-message">{event.message}</p>
              <Summary event={event} />
              <details className="raw">
                <summary>payload</summary>
                <pre>{JSON.stringify(event.payload, null, 2)}</pre>
              </details>
            </div>
          </li>
        ))}
      </ol>
      {visible.length === 0 && <p className="empty-small">Nothing in this filter.</p>}
    </div>
  );
}