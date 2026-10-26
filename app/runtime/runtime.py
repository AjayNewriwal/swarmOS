"""Per-run runtime: provider, ledger, trace, and isolated workspace."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.config import Settings
from app.models import Limits, ModelTier, TraceEvent, TraceKind
from app.providers.base import Provider
from app.runtime.limits import BudgetLedger
from app.runtime.trace import TraceRecorder
from app.runtime.workspace import Workspace


class RunRuntime:
    def __init__(
        self,
        *,
        run_id: str,
        settings: Settings,
        limits: Limits,
        provider: Provider,
        provider_name: str,
        provider_warning: str | None,
        store: object | None = None,
    ) -> None:
        self.run_id = run_id
        self.settings = settings
        self.limits = limits
        self.provider = provider
        self.provider_name = provider_name
        self.provider_warning = provider_warning
        self.ledger = BudgetLedger(
            max_usd=limits.max_budget_usd,
            max_calls=limits.max_agent_calls,
            max_wall_seconds=limits.max_wall_seconds,
            reservation_usd=settings.reservation_usd,
        )
        self.trace = TraceRecorder(run_id, store)
        self._temp_root = Path(tempfile.mkdtemp(prefix=f"swarmos-{run_id}-"))
        self.workspace = Workspace(self._temp_root / "workspace")
        self.step_root = self._temp_root / "steps"
        self.step_root.mkdir(parents=True, exist_ok=True)

    def seed_workspace(self, files: dict[str, str], fallback: Path) -> None:
        if files:
            self.workspace.seed(files)
        elif fallback and fallback.is_dir():
            self.workspace = Workspace.from_directory(fallback, self._temp_root / "workspace")
        else:
            self.workspace.write(
                "README.md",
                "# SwarmOS workspace\n\nThis run started from an empty workspace.\n",
            )

    def model_for(self, tier: ModelTier) -> str:
        return self.settings.model_for(tier)

    def rates_for(self, model: str) -> tuple[float, float]:
        return self.settings.rates_for(model)

    def cost_for(self, model: str, input_tokens: int, output_tokens: int) -> float:
        input_rate, output_rate = self.rates_for(model)
        if input_rate == 0.0 and output_rate == 0.0:
            return 0.0
        return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000

    def estimate_call_cost(self, model: str) -> float:
        """Conservative reservation for one call.

        With no configured rates the honest estimate is zero; USD enforcement
        then cannot apply and the call/time limits are the effective guard.
        """
        input_rate, output_rate = self.rates_for(model)
        if input_rate == 0.0 and output_rate == 0.0:
            return 0.0
        return max(0.0, self.settings.reservation_usd)

    def emit(self, kind: TraceKind, message: str = "", **payload: object) -> TraceEvent:
        return self.trace.emit(kind, message, **payload)

    def cleanup(self) -> None:
        shutil.rmtree(self._temp_root, ignore_errors=True)