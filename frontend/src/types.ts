export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_findings"
  | "failed"
  | "budget_exceeded"
  | "limit_exceeded";

export interface Limits {
  max_agent_calls: number;
  max_budget_usd: number;
  max_wall_seconds: number;
  max_retries: number;
  max_revisions: number;
  max_escalations: number;
  parallel_coding: boolean;
}

export interface PlanStep {
  id: string;
  title: string;
  detail: string;
  acceptance_criteria: string[];
  depends_on: string[];
  target_files: string[];
}

export interface Plan {
  steps: PlanStep[];
  assumptions: string[];
}

export interface ReviewFinding {
  severity: "blocker" | "major" | "minor" | "nit";
  step_id: string | null;
  file: string | null;
  message: string;
}

export interface Review {
  verdict: "approve" | "request_changes";
  rationale: string;
  findings: ReviewFinding[];
}

export interface Verification {
  passed: boolean;
  command: string;
  exit_code: number;
  duration_ms: number;
  tests_passed: number;
  tests_failed: number;
  stdout_tail: string;
  stderr_tail: string;
  workspace_files: string[];
}

export interface RunRecord {
  run_id: string;
  created_at: string;
  updated_at: string;
  goal: string;
  provider: string;
  provider_warning: string | null;
  status: RunStatus;
  limits: Limits;
  plan: Plan | null;
  review: Review | null;
  verification: Verification | null;
  summary: string;
  error: string | null;
  cost_usd: number;
  agent_calls: number;
  escalations: number;
  revisions: number;
  duration_ms: number;
  changed_files: string[];
  artifacts: Record<string, string>;
}

export interface TraceEvent {
  event_id: string;
  run_id: string;
  seq: number;
  at: string;
  kind: string;
  message: string;
  payload: Record<string, unknown>;
}

export interface RunDetail {
  run: RunRecord;
  events: TraceEvent[];
}

export interface Caps {
  max_agent_calls: number;
  max_budget_usd: number;
  max_wall_seconds: number;
  max_retries: number;
  max_revisions: number;
  max_escalations: number;
}

export interface AppConfig {
  provider_mode: string;
  openai_configured: boolean;
  caps: Caps;
  verification_command: string;
  sample_files: string[];
  demo_fixture: boolean;
}

export interface CreateRunInput {
  goal: string;
  provider: "auto" | "demo" | "openai";
  limits: Partial<Limits>;
}

export const TERMINAL: RunStatus[] = [
  "completed",
  "completed_with_findings",
  "failed",
  "budget_exceeded",
  "limit_exceeded",
];

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL.includes(status);
}