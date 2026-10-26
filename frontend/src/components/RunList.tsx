import type { RunRecord } from "../types";
import { StatusBadge } from "./StatusBadge";

interface Props {
  runs: RunRecord[];
  selectedId: string | null;
  onSelect: (runId: string) => void;
}

function timestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function RunList({ runs, selectedId, onSelect }: Props) {
  if (runs.length === 0) {
    return <p className="empty-small">No runs yet. Dispatch one above.</p>;
  }
  return (
    <ul className="run-list">
      {runs.map((run) => (
        <li key={run.run_id}>
          <button
            type="button"
            className={`run-item ${run.run_id === selectedId ? "active" : ""}`}
            onClick={() => onSelect(run.run_id)}
          >
            <div className="run-item-top">
              <StatusBadge status={run.status} />
              <span className="muted">{timestamp(run.created_at)}</span>
            </div>
            <span className="run-goal">{run.goal}</span>
            <span className="run-meta">
              {run.agent_calls} calls · {run.changed_files.length} files ·{" "}
              {run.provider}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}