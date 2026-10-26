export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "completed_with_findings"
  | "failed"
  | "budget_exceeded"
  | "limit_exceeded";

export type ProviderKind = "demo" | "local" | "remote" | "unknown";

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
  diagnostic: boolean;
}

export interface RunRecord {
  run_id: string;
  created_at: string;
  updated_at: string;
  goal: string;
  provider: string;
  provider_kind: ProviderKind;
  provider_warning: string | null;
  status: RunStatus;
  limits: Limits;
  model_policy: string;
  models_by_role: Record<string, string>;
  plan: Plan | null;
  review: Review | null;
  verification: Verification | null;
  summary: string;
  error: string | null;
  cost_usd: number;
  monetary_enforced: boolean;
  usage_available: boolean;
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

export interface ModelProfile {
  name: string;
  model: string;
  priced: boolean;
  input_usd_per_million: number;
  output_usd_per_million: number;
}

export interface AppConfig {
  provider_mode: string;
  provider_kind: ProviderKind;
  openai_configured: boolean;
  base_url: string;
  auth_configured: boolean;
  json_mode: boolean;
  profiles: Record<string, ModelProfile>;
  role_routing: Record<string, string>;
  model_policy: string;
  escalation_available: boolean;
  pricing_known: boolean;
  monetary_enforced: boolean;
  caps: Caps;
  verification: {
    command: string;
    timeout_seconds: number;
    execution_enabled: boolean;
    allowed_commands: string[];
    repo_checkout_allowed: boolean;
  };
  api: { auth_required: boolean; remote_bind_allowed: boolean };
  sample_files: string[];
  demo_fixture: boolean;
}

export interface CreateRunInput {
  goal: string;
  provider: "auto" | "demo" | "openai";
  model_policy?: "config" | "fast_only" | "adaptive" | "role_routed";
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

export function providerLabel(kind: ProviderKind): string {
  switch (kind) {
    case "demo":
      return "demo fixture";
    case "local":
      return "local inference";
    case "remote":
      return "remote endpoint";
    default:
      return "unknown provider";
  }
}