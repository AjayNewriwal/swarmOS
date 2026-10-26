import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { RunDetail } from "./components/RunDetail";
import { RunForm } from "./components/RunForm";
import { RunList } from "./components/RunList";
import type { AppConfig, CreateRunInput, RunDetail as RunDetailData, RunRecord } from "./types";
import { isTerminal } from "./types";

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetailData | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .config()
      .then((value) => {
        if (!cancelled) setConfig(value);
      })
      .catch((cause: Error) => {
        if (!cancelled) setError(cause.message);
      });
    api
      .listRuns()
      .then((value) => {
        if (cancelled) return;
        setRuns(value);
        if (value.length > 0) setSelectedId((current) => current ?? value[0].run_id);
      })
      .catch((cause: Error) => {
        if (!cancelled) setError(cause.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const next = await api.getRun(selectedId);
        if (cancelled) return;
        setDetail(next);
        setRuns((current) =>
          current.map((run) => (run.run_id === next.run.run_id ? next.run : run)),
        );
        if (!isTerminal(next.run.status)) {
          timer = window.setTimeout(poll, 700);
        }
      } catch (cause) {
        if (!cancelled) setError((cause as Error).message);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [selectedId]);

  const submit = useCallback(async (input: CreateRunInput) => {
    setBusy(true);
    setError(null);
    try {
      const created = await api.createRun(input);
      setRuns((current) => [
        created.run,
        ...current.filter((run) => run.run_id !== created.run.run_id),
      ]);
      setSelectedId(created.run.run_id);
      setDetail(created);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      setRuns(await api.listRuns());
    } catch (cause) {
      setError((cause as Error).message);
    }
  }, []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div>
            <h1>SwarmOS</h1>
            <p className="tagline">adaptive multi-agent coding orchestration</p>
          </div>
        </div>

        <div className="provider-line">
          <span className={`provider ${config?.demo_fixture ? "demo" : "live"}`}>
            {config
              ? config.provider_kind === "demo"
                ? "demo fixture"
                : config.provider_kind === "local"
                  ? "local inference"
                  : "remote endpoint"
              : "…"}
          </span>
          {config && (
            <span className="muted mono small">{config.verification.command}</span>
          )}
        </div>

        {config ? (
          <RunForm
            caps={config.caps}
            openaiConfigured={config.openai_configured}
            defaultPolicy={config.model_policy}
            escalationAvailable={config.escalation_available}
            busy={busy}
            onSubmit={submit}
          />
        ) : (
          <p className="muted small">Connecting to the API…</p>
        )}

        {error && <div className="callout danger small">{error}</div>}

        <div className="sidebar-head">
          <h2>Runs</h2>
          <button type="button" className="link-button" onClick={() => void refresh()}>
            refresh
          </button>
        </div>
        <RunList runs={runs} selectedId={selectedId} onSelect={setSelectedId} />
      </aside>

      <main className="main">
        {detail ? (
          <RunDetail detail={detail} />
        ) : (
          <div className="empty">
            <div className="brand-mark large" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <h2>No run selected</h2>
            <p className="muted">
              Dispatch a task on the left. SwarmOS plans it, fans out independent
              coding steps, reviews the result, then runs the tests and records
              every decision.
            </p>
            <div className="empty-hints">
              <span className="chip-static">Planner</span>
              <span className="chip-static">Coding agent</span>
              <span className="chip-static">Review agent</span>
              <span className="chip-static">Verification agent</span>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}