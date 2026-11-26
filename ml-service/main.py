from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from anomaly_model import IsolationForestDetector
from chronos_adapter import ChronosAdapter

MODEL_DIR = os.environ.get("CHRONOS_MODEL_DIR", "/models/chronos-2")
IFOREST_PATH = os.environ.get("IFOREST_ARTIFACT_PATH", "/app/artifacts/isolation_forest.joblib")
MANIFEST_PATH = os.environ.get("MODEL_MANIFEST_PATH", "/app/model-manifest.json")

chronos_adapter = ChronosAdapter(MODEL_DIR, MANIFEST_PATH)
iforest_detector = IsolationForestDetector(IFOREST_PATH, MANIFEST_PATH)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    chronos_adapter.load()
    iforest_detector.load()
    yield


app = FastAPI(title="BUP Internal ML Service", version="1.0.0", lifespan=lifespan)


class ForecastRequest(BaseModel):
    generation_id: str
    as_of_tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    horizon_ticks: int = Field(gt=0, le=288)
    series: list[dict[str, Any]]


class AnomalyRequest(BaseModel):
    items: list[dict[str, Any]]


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    chronos_ready = chronos_adapter.status == "loaded"
    iforest_ready = iforest_detector.status == "loaded"
    both_ready = chronos_ready and iforest_ready
    status_str = "ready" if both_ready else ("partial" if (chronos_ready or iforest_ready) else "degraded")
    payload = {
        "status": status_str,
        "chronos2_ready": chronos_ready,
        "isolation_forest_ready": iforest_ready,
        "chronos2": {
            "status": chronos_adapter.status,
            "model_id": chronos_adapter.model_id,
            "revision": chronos_adapter.model_revision,
            "load_time_ms": chronos_adapter.load_time_ms,
            "last_inference_ms": chronos_adapter.last_inference_ms,
            "last_inference_at": chronos_adapter.last_inference_at,
            "error": chronos_adapter.error,
        },
        "isolation_forest": {
            "status": iforest_detector.status,
            "version": iforest_detector.model_version,
            "error": iforest_detector.error,
        },
    }
    return JSONResponse(status_code=200 if both_ready else 503, content=payload)


@app.post("/internal/forecast")
def internal_forecast(req: ForecastRequest) -> dict[str, Any]:
    try:
        return chronos_adapter.predict(
            generation_id=req.generation_id,
            as_of_tick=req.as_of_tick,
            tick_minutes=req.tick_minutes,
            horizon_ticks=req.horizon_ticks,
            series_inputs=req.series,
        )
    except Exception as exc:
        raise HTTPException(503, {"error": str(exc), "status": chronos_adapter.status}) from exc


@app.post("/internal/anomaly")
def internal_anomaly(req: AnomalyRequest) -> dict[str, Any]:
    try:
        scores = iforest_detector.score_batch(req.items)
        return {"model_version": iforest_detector.model_version, "scores": scores}
    except Exception as exc:
        raise HTTPException(503, {"error": str(exc), "status": iforest_detector.status}) from exc
