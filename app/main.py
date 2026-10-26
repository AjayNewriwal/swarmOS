from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app.models import RunDetail, RunRequest
from app.providers.base import ProviderConfigError
from app.service import RunService
from app.store import RunStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = RunStore(settings.db_path)
    app.state.store = store
    app.state.service = RunService(settings, store)
    try:
        yield
    finally:
        await app.state.service.drain()
        store.close()


app = FastAPI(
    title="SwarmOS",
    version=__version__,
    description="Adaptive multi-agent coding orchestration with inspectable traces",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _service() -> RunService:
    return app.state.service


def _store() -> RunStore:
    return app.state.store


def _detail(run_id: str) -> RunDetail:
    record = _store().get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return RunDetail(run=record, events=_store().get_events(run_id))


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "swarmos",
        "version": __version__,
        "provider_mode": settings.provider,
        "openai_configured": settings.openai_configured,
    }


@app.get("/api/config")
async def config() -> dict:
    sample_files: list[str] = []
    if settings.sample_project.is_dir():
        sample_files = sorted(
            str(path.relative_to(settings.sample_project)).replace("\\", "/")
            for path in settings.sample_project.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return {
        "provider_mode": settings.provider,
        "openai_configured": settings.openai_configured,
        "caps": {
            "max_agent_calls": settings.max_agent_calls,
            "max_budget_usd": settings.max_budget_usd,
            "max_wall_seconds": settings.max_wall_seconds,
            "max_retries": settings.max_retries,
            "max_revisions": settings.max_revisions,
            "max_escalations": settings.max_escalations,
        },
        "verification_command": settings.verification_command,
        "sample_files": sample_files,
        "demo_fixture": not settings.openai_configured,
    }


@app.post("/api/runs", status_code=202)
async def create_run(
    request: RunRequest,
    response: Response,
    wait: bool = Query(default=False, description="Execute inline instead of in the background"),
) -> RunDetail:
    service = _service()
    try:
        if wait:
            record = await service.run_request(request)
            response.status_code = 200
        else:
            record = service.submit(request)
    except ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _detail(record.run_id)


@app.get("/api/runs")
async def list_runs(limit: int = Query(default=25, ge=1, le=200)) -> dict:
    records = _store().list_runs(limit)
    return {"runs": [record.model_dump(mode="json") for record in records]}


@app.get("/api/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: str) -> RunDetail:
    return _detail(run_id)


@app.get("/api/runs/{run_id}/events")
async def get_run_events(run_id: str) -> dict:
    if _store().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return {
        "run_id": run_id,
        "events": [
            event.model_dump(mode="json") for event in _store().get_events(run_id)
        ],
    }


# Serve the built React dashboard when it exists, so one process can run the MVP.
_dist = Path(settings.frontend_dist)
if (_dist / "index.html").is_file():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="ui")
else:

    @app.get("/")
    async def root() -> dict:
        return {
            "service": "swarmos",
            "docs": "/docs",
            "ui": "Run `npm install && npm run dev` in frontend/ for the dashboard.",
        }