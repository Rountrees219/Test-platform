"""
Footbolzano Live Scoring — FastAPI Application
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.database import engine, Base
from app.models import models  # noqa: F401 — ensures all ORM models are registered with Base
from app.api import matchup, admin, gold, events, simulation


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Footbolzano Live Scoring",
    description="Real-time fantasy football scoring with Footbolzano mechanics",
    version="1.0.0-demo",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Demo mode: allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(matchup.router, prefix="/api/matchup", tags=["matchup"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(gold.router, prefix="/api/gold", tags=["gold"])
app.include_router(events.router, prefix="/api/events", tags=["events"])
app.include_router(simulation.router, prefix="/api/simulation", tags=["simulation"])


@app.get("/")
def root():
    return {
        "app": "Footbolzano Live Scoring",
        "version": "1.0.0-demo",
        "status": "running",
        "disclaimer": "DEMO — All scoring data is simulated. This application is independent of footbolzano.com.",
        "source": "Rules from https://footbolzano.com/rules/ (2025 Rulebook v1)",
    }


@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok", "is_demo": True}
