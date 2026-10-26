"""Process configuration.

Every tunable that affects money, time, or autonomy is read here once so that
limits cannot be silently widened deep inside the orchestrator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)


@dataclass(frozen=True)
class Settings:
    # Provider selection: "auto" | "demo" | "openai"
    provider: str = os.getenv("SWARMOS_PROVIDER", "auto").strip().lower()

    # OpenAI-compatible endpoint. SwarmOS makes no assumption that any hosted
    # assistant exposes an API; this is deliberately generic and optional.
    base_url: str = os.getenv("OPENAI_COMPAT_BASE_URL", "").strip().rstrip("/")
    api_key: str = os.getenv("OPENAI_COMPAT_API_KEY", "").strip()
    fast_model: str = os.getenv("OPENAI_COMPAT_MODEL_FAST", "").strip()
    strong_model: str = os.getenv("OPENAI_COMPAT_MODEL_STRONG", "").strip()

    fast_input_rate: float = _env_float("FAST_INPUT_USD_PER_MILLION", 0.0)
    fast_output_rate: float = _env_float("FAST_OUTPUT_USD_PER_MILLION", 0.0)
    strong_input_rate: float = _env_float("STRONG_INPUT_USD_PER_MILLION", 0.0)
    strong_output_rate: float = _env_float("STRONG_OUTPUT_USD_PER_MILLION", 0.0)

    request_timeout_seconds: int = _env_int("SWARMOS_PROVIDER_TIMEOUT_SECONDS", 90)

    # Hard defaults. A request may lower these but never raise the process cap.
    max_agent_calls: int = _env_int("SWARMOS_MAX_AGENT_CALLS", 24)
    max_budget_usd: float = _env_float("SWARMOS_MAX_BUDGET_USD", 0.50)
    max_wall_seconds: int = _env_int("SWARMOS_MAX_WALL_SECONDS", 300)
    max_retries: int = _env_int("SWARMOS_MAX_RETRIES", 2)
    max_revisions: int = _env_int("SWARMOS_MAX_REVISIONS", 1)
    max_escalations: int = _env_int("SWARMOS_MAX_ESCALATIONS", 1)

    # Conservative per-call reservation used to enforce the budget before spend.
    reservation_usd: float = _env_float("SWARMOS_RESERVATION_USD", 0.02)

    verification_command: str = os.getenv("SWARMOS_VERIFY_COMMAND", "python -m pytest -q")
    verification_timeout_seconds: int = _env_int("SWARMOS_VERIFY_TIMEOUT_SECONDS", 120)

    db_path: Path = field(
        default_factory=lambda: _env_path("SWARMOS_DB_PATH", PROJECT_ROOT / "swarmos.db")
    )
    sample_project: Path = field(
        default_factory=lambda: _env_path(
            "SWARMOS_SAMPLE_PROJECT", PROJECT_ROOT / "samples" / "sample_project"
        )
    )
    frontend_dist: Path = field(
        default_factory=lambda: _env_path(
            "SWARMOS_FRONTEND_DIST", PROJECT_ROOT / "frontend" / "dist"
        )
    )

    @property
    def openai_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.fast_model)

    def model_for(self, tier: str) -> str:
        if tier == "strong":
            return self.strong_model or self.fast_model or "demo-strong"
        return self.fast_model or "demo-fast"

    def rates_for(self, model: str) -> tuple[float, float]:
        if model and model == self.strong_model:
            return self.strong_input_rate, self.strong_output_rate
        return self.fast_input_rate, self.fast_output_rate


settings = Settings()