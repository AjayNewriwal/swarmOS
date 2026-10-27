import { useState } from "react";
import type { Caps, CreateRunInput } from "../types";

interface Props {
  caps: Caps;
  openaiConfigured: boolean;
  defaultPolicy: string;
  escalationAvailable: boolean;
  busy: boolean;
  onSubmit: (input: CreateRunInput) => Promise<void>;
  onCompare: (input: CreateRunInput) => Promise<void>;
}

const EXAMPLES = [
  "Implement tag normalization so the failing tests pass",
  "Fix the failing test in target.py and document the change",
];

export function RunForm({
  caps,
  openaiConfigured,
  defaultPolicy,
  escalationAvailable,
  busy,
  onSubmit,
  onCompare,
}: Props) {
  const [goal, setGoal] = useState(EXAMPLES[0]);
  const [provider, setProvider] = useState<"auto" | "demo" | "openai">("auto");
  const [modelPolicy, setModelPolicy] = useState(defaultPolicy || "config");
  const [budget, setBudget] = useState(Math.min(0.25, caps.max_budget_usd));
  const [calls, setCalls] = useState(Math.min(12, caps.max_agent_calls));
  const [retries, setRetries] = useState(Math.min(2, caps.max_retries));
  const [escalations, setEscalations] = useState(Math.min(1, caps.max_escalations));
  const [revisions, setRevisions] = useState(Math.min(1, caps.max_revisions));
  const [advanced, setAdvanced] = useState(false);

  const tooShort = goal.trim().length < 8;

  const buildInput = (): CreateRunInput => ({
    goal: goal.trim(),
    provider,
    model_policy: modelPolicy as CreateRunInput["model_policy"],
    limits: {
      max_budget_usd: budget,
      max_agent_calls: calls,
      max_retries: retries,
      max_escalations: escalations,
      max_revisions: revisions,
    },
  });

  return (
    <form
      className="run-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (tooShort || busy) return;
        void onSubmit(buildInput());
      }}
    >
      <label className="field">
        <span>Goal</span>
        <textarea
          value={goal}
          onChange={(event) => setGoal(event.target.value)}
          rows={4}
          placeholder="Describe the coding task…"
        />
      </label>
      <div className="examples">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            className="chip"
            onClick={() => setGoal(example)}
          >
            {example.length > 34 ? `${example.slice(0, 34)}…` : example}
          </button>
        ))}
      </div>

      <div className="row">
        <label className="field compact">
          <span>Provider</span>
          <select
            value={provider}
            onChange={(event) =>
              setProvider(event.target.value as "auto" | "demo" | "openai")
            }
          >
            <option value="auto">auto</option>
            <option value="demo">demo fixture</option>
            <option value="openai" disabled={!openaiConfigured}>
              openai-compatible
            </option>
          </select>
        </label>
        <label className="field compact">
          <span>Budget (USD)</span>
          <input
            type="number"
            min={0}
            max={caps.max_budget_usd}
            step={0.05}
            value={budget}
            onChange={(event) => setBudget(Number(event.target.value))}
          />
        </label>
      </div>

      <label className="field compact">
        <span>Model policy</span>
        <select
          value={modelPolicy}
          onChange={(event) => setModelPolicy(event.target.value)}
        >
          <option value="config">configured role routing</option>
          <option value="fast_only">fast only (no escalation)</option>
          <option value="adaptive">adaptive (fast, then escalate)</option>
          <option value="role_routed" disabled={!escalationAvailable}>
            role-routed (needs a strong model)
          </option>
        </select>
      </label>

      <button
        type="button"
        className="link-button"
        onClick={() => setAdvanced((value) => !value)}
      >
        {advanced ? "Hide limits" : "Autonomy limits"}
      </button>

      {advanced && (
        <div className="limits-grid">
          <label className="field compact">
            <span>Max agent calls</span>
            <input
              type="number"
              min={1}
              max={caps.max_agent_calls}
              value={calls}
              onChange={(event) => setCalls(Number(event.target.value))}
            />
          </label>
          <label className="field compact">
            <span>Max retries / step</span>
            <input
              type="number"
              min={0}
              max={caps.max_retries}
              value={retries}
              onChange={(event) => setRetries(Number(event.target.value))}
            />
          </label>
          <label className="field compact">
            <span>Max escalations</span>
            <input
              type="number"
              min={0}
              max={caps.max_escalations}
              value={escalations}
              onChange={(event) => setEscalations(Number(event.target.value))}
            />
          </label>
          <label className="field compact">
            <span>Max revision rounds</span>
            <input
              type="number"
              min={0}
              max={caps.max_revisions}
              value={revisions}
              onChange={(event) => setRevisions(Number(event.target.value))}
            />
          </label>
        </div>
      )}

      <div className="form-actions">
        <button className="primary" type="submit" disabled={busy || tooShort}>
          {busy ? "Dispatching…" : "Run orchestration"}
        </button>
        <button
          className="secondary"
          type="button"
          disabled={busy || tooShort}
          onClick={() => void onCompare(buildInput())}
          title="Run the same goal under fixed-fast, adaptive, and role-routed policies"
        >
          Compare policies
        </button>
      </div>
      {!openaiConfigured && (
        <p className="hint">
          No model endpoint is configured, so runs use the deterministic demo
          fixture. Orchestration, verification, and traces are real.
        </p>
      )}
    </form>
  );
}