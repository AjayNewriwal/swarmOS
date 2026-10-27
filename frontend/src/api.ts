import type {
  AppConfig,
  CreateRunInput,
  ProviderModelsResult,
  RunDetail,
  RunRecord,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch {
      // keep the status line
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  config: () => request<AppConfig>("/api/config"),

  listRuns: (limit = 25) =>
    request<{ runs: RunRecord[] }>(`/api/runs?limit=${limit}`).then((r) => r.runs),

  getRun: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),

  createRun: (input: CreateRunInput) =>
    request<RunDetail>("/api/runs", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  providerModels: () => request<ProviderModelsResult>("/api/provider/models"),
};