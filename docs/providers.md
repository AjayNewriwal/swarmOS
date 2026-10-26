# Providers and model routing

SwarmOS talks to any **OpenAI-compatible** `/chat/completions` endpoint. That
covers local runtimes first and hosted compatible APIs second. No hosted
provider is assumed to be free, and no assistant product is assumed to expose
its internal models over HTTP.

## Supported configurations

| Provider | `SWARMOS_PROVIDER` | Base URL shape | Key |
| --- | --- | --- | --- |
| Demo fixture | `demo` | none | none |
| Ollama | `openai` or `auto` | `http://127.0.0.1:11434/v1` | empty |
| vLLM | `openai` or `auto` | `http://127.0.0.1:8000/v1` | empty |
| llama.cpp server | `openai` or `auto` | `http://127.0.0.1:8080/v1` | empty |
| LM Studio | `openai` or `auto` | `http://127.0.0.1:1234/v1` | empty |
| Hosted compatible | `openai` or `auto` | provider's documented URL | required |

The base URL must include the version prefix (`/v1`) if the server expects it.
SwarmOS appends `/chat/completions` and does not rewrite the path.

Precedence: an explicit per-request `provider` wins; `auto` uses
`SWARMOS_PROVIDER` when that is `demo` or `openai`; otherwise `auto` uses the
endpoint if configured, else the demo fixture.

## Minimal local setup (Ollama)

```bash
ollama pull qwen2.5-coder:7b
ollama serve
```

`.env`:

```dotenv
SWARMOS_PROVIDER=openai
OPENAI_COMPAT_BASE_URL=http://127.0.0.1:11434/v1
OPENAI_COMPAT_API_KEY=
OPENAI_COMPAT_MODEL_FAST=qwen2.5-coder:7b
OPENAI_COMPAT_MODEL_STRONG=
```

Verify without running inference:

```bash
swarmos doctor
```

`doctor` lists models from `<base>/models`; it never sends a prompt and costs
nothing. A configured model that is absent from the list is reported.

Then run:

```bash
swarmos run "Implement tag normalization so the failing tests pass" --provider openai
```

## Models and role routing

A **profile** is a model id plus optional pricing:

- `fast` — required, from `OPENAI_COMPAT_MODEL_FAST`.
- `strong` — optional, from `OPENAI_COMPAT_MODEL_STRONG`. It only exists when it
  is a **different** model id from `fast`.

Roles map to profiles with `SWARMOS_ROLE_MODELS`, e.g.
`planner=strong,coder=fast,reviewer=strong`. The verifier is not a model; it runs
the test command.

Policies (per request with `--policy`, defaulting to `SWARMOS_MODEL_POLICY`):

| Policy | Selection | Escalation |
| --- | --- | --- |
| `config` | role routing | allowed if strong exists |
| `fast_only` | every role fast | forced off |
| `adaptive` | every role starts fast | allowed if strong exists |
| `role_routed` | role routing | allowed if strong exists |

All policies run the same orchestration graph. They differ only in model
selection, so a comparison measures routing, not a different product.

## Fallback, stated exactly

- No endpoint configured + `auto` → demo fixture, with a warning in the run.
- `openai` requested but base URL or fast model missing → `ProviderConfigError`
  (HTTP 400 from the API, exit code 2 from the CLI).
- Escalation requested but no distinct strong model → **escalation is reported as
  unavailable**. The run does not silently call the fast model again.
- There is **no cross-provider fallback**. One endpoint is used per run. With a
  single endpoint configured, SwarmOS is a single-provider orchestrator, and the
  run record says so.

## Error handling

HTTP and transport failures are classified. Only transient classes are retried,
with exponential backoff (base `SWARMOS_RETRY_BACKOFF_SECONDS`, capped at 8s):

| Category | Typical cause | Retried |
| --- | --- | --- |
| `auth` | 401/403, wrong or missing key | no |
| `rate_limit` | 429, honours `Retry-After` | yes |
| `timeout` | request exceeded `SWARMOS_PROVIDER_TIMEOUT_SECONDS` | yes |
| `connection` | nothing listening, DNS failure | yes |
| `server` | 5xx | yes |
| `bad_request` | 400/422, unsupported parameter | no |
| `not_found` | 404, wrong URL or model id | no |
| `invalid_response` | non-JSON body, missing/empty content | no |
| `config` | provider or model not configured | no |

Unparseable **structured output** is retried with an explicit repair note that
includes the parser error. That is a different prompt, not an identical resend.

Every failure is recorded with its category in the trace (`provider` or
`failure` event) and on the invocation (`error_class`).

## Structured output

Requests ask for `response_format: {"type": "json_object"}` when
`SWARMOS_JSON_MODE=1`. If a server rejects that field, SwarmOS retries once
without it, records `structured_mode: "prompt_only"`, and keeps that downgrade
for the rest of the run. The agents' JSON parsing is unchanged: schema
validation still applies.

## Usage and cost honesty

- Standard `usage.prompt_tokens` / `usage.completion_tokens` and Ollama's
  `prompt_eval_count` / `eval_count` are both read.
- If no usage is present, tokens and cost are zero and `usage_available` is
  false. Nothing is estimated.
- **USD is only treated as an enforced budget when both input and output rates
  are configured for every usable profile.** Otherwise the run says
  "Monetary budget: NOT enforced" and call, time, retry, and revision limits are
  the effective controls.
- Even when enforced, the cap is **soft, not a strict hard limit**. Reserves are
  settled after each call, so a call already in flight can exceed the cap by its
  own cost, and several parallel in-flight calls can collectively exceed the
  configured cap before any of them settles. The cap bounds the spend of calls
  that have not started yet; it does not bound the total spend of the run. The
  call count and the wall clock are always enforced. The summary says this
  explicitly.

## Testing status

The adapter is covered by mock-HTTP tests: success, usage variants, missing
usage, auth, rate limit with `Retry-After`, timeout, connection failure, bad
request, JSON-mode downgrade, invalid shape, and health. It has **not** been
validated against a live model in this environment, because no local runtime is
installed. Do not read the mock tests as evidence of a real inference call.
