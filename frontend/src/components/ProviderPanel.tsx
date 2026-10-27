import { useState } from "react";
import { api } from "../api";
import type { AppConfig, ProviderModelsResult } from "../types";

interface Props {
  config: AppConfig;
}

/**
 * Shows the provider the process is actually configured for, and lets the
 * operator check reachability without running inference. The check lists
 * models; it never sends a prompt and never costs anything.
 */
export function ProviderPanel({ config }: Props) {
  const [result, setResult] = useState<ProviderModelsResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const configured = Object.values(config.profiles).map((profile) => profile.model);
  const isDemo = config.provider_kind === "demo";

  const check = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.providerModels());
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const missing =
    result && result.models.length > 0
      ? configured.filter((model) => !result.models.includes(model))
      : [];

  return (
    <details className="provider-panel">
      <summary>Provider status</summary>

      <dl className="provider-facts">
        <dt>kind</dt>
        <dd>{config.provider_kind}</dd>
        <dt>endpoint</dt>
        <dd className="mono">{config.base_url || "(none — demo fixture)"}</dd>
        <dt>auth</dt>
        <dd>{config.auth_configured ? "bearer token set" : "none"}</dd>
        <dt>pricing</dt>
        <dd>
          {config.pricing_known
            ? "configured (USD budget enforceable)"
            : "unknown (USD budget not enforceable)"}
        </dd>
      </dl>

      {Object.keys(config.profiles).length > 0 ? (
        <div className="limit-chips">
          {Object.entries(config.profiles).map(([name, profile]) => (
            <span className="chip-static" key={name}>
              {name} → <span className="mono">{profile.model}</span>
            </span>
          ))}
        </div>
      ) : (
        <p className="hint">
          No model is configured. Runs use the deterministic demo fixture, so the
          orchestration, verification, and trace are real but the model output is
          scripted.
        </p>
      )}

      <div className="limit-chips">
        {Object.entries(config.role_routing).map(([role, profile]) => (
          <span className="chip-static" key={role}>
            {role} → {profile}
          </span>
        ))}
      </div>

      {isDemo ? (
        <p className="hint">Nothing to check: no endpoint is configured.</p>
      ) : (
        <button
          type="button"
          className="secondary small"
          onClick={() => void check()}
          disabled={busy}
        >
          {busy ? "Checking…" : "Check endpoint (lists models)"}
        </button>
      )}

      {error && <div className="callout danger small">{error}</div>}

      {result && (
        <div className="probe-result">
          <div className="chips">
            <span className={`chip-static ${result.ok ? "ok" : "danger"}`}>
              {result.ok ? "reachable" : "unreachable"}
            </span>
            <span className="chip-static">{result.detail}</span>
          </div>
          {result.models.length > 0 && (
            <div className="limit-chips">
              {result.models.slice(0, 12).map((model) => (
                <span
                  className={`chip-static ${configured.includes(model) ? "ok" : ""}`}
                  key={model}
                >
                  {model}
                </span>
              ))}
            </div>
          )}
          {missing.length > 0 && (
            <p className="hint danger">
              Configured but not listed by the endpoint: {missing.join(", ")}. A run
              will fail until the model id matches.
            </p>
          )}
          {result.warning && <p className="hint">{result.warning}</p>}
        </div>
      )}
    </details>
  );
}