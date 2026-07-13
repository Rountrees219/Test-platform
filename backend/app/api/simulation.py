"""
Simulation Control API — SSE live updates + simulation management.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
import asyncio
import json
from typing import Optional
from datetime import datetime, timezone

from app.core.database import get_db, SessionLocal
from app.models.models import (
    Matchup, PlayerRoster, SimulationEvent, Event, EventType, MatchupStatus
)
from app.providers.mock_live import MockLiveProvider

router = APIRouter()

# Singleton provider per matchup (demo only)
_providers: dict[int, MockLiveProvider] = {}


def get_provider(matchup_id: int) -> MockLiveProvider:
    if matchup_id not in _providers:
        _providers[matchup_id] = MockLiveProvider(seed=42)
    return _providers[matchup_id]


class SimAction(BaseModel):
    action: str  # start, pause, resume, stop, restart
    speed: Optional[float] = None


@router.post("/{matchup_id}/control")
def control_simulation(matchup_id: int, action: SimAction, db: Session = Depends(get_db)):
    """Control the simulation state."""
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")

    provider = get_provider(matchup_id)
    act = action.action.lower()

    if act == "start":
        matchup.simulation_state = "running"
        matchup.status = MatchupStatus.LIVE
        if action.speed:
            matchup.simulation_speed = action.speed
    elif act == "pause":
        matchup.simulation_state = "paused"
    elif act == "resume":
        matchup.simulation_state = "running"
    elif act == "stop":
        matchup.simulation_state = "stopped"
    elif act == "restart":
        matchup.simulation_state = "stopped"
        matchup.simulation_event_index = 0
        provider.reset(seed=42)
        # Reset all player scores to 0
        db.query(PlayerRoster).filter(PlayerRoster.matchup_id == matchup_id).update(
            {PlayerRoster.raw_score: 0.0, PlayerRoster.adjusted_score: 0.0}
        )
        db.query(SimulationEvent).filter(
            SimulationEvent.matchup_id == matchup_id
        ).update({SimulationEvent.was_executed: False})
    elif act == "jump_to_end":
        provider.jump_to_end()
        matchup.simulation_state = "finished"
        _apply_all_sim_events(matchup_id, db)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {act}")

    if action.speed:
        matchup.simulation_speed = action.speed

    db.commit()
    return {"status": "ok", "simulation_state": matchup.simulation_state}


def _apply_all_sim_events(matchup_id: int, db: Session):
    """Apply all simulation events at once."""
    sim_events = (
        db.query(SimulationEvent)
        .filter(SimulationEvent.matchup_id == matchup_id, SimulationEvent.was_executed == False)
        .order_by(SimulationEvent.event_index)
        .all()
    )
    for se in sim_events:
        pr = db.query(PlayerRoster).filter(PlayerRoster.id == se.player_roster_id).first()
        if pr:
            pr.raw_score = se.new_raw_score
            pr.adjusted_score = se.new_raw_score
            if se.game_status:
                pr.game_status = se.game_status
        se.was_executed = True
        se.executed_at = datetime.now(timezone.utc)
    db.commit()


@router.get("/{matchup_id}/next-event")
def get_next_simulation_event(matchup_id: int, db: Session = Depends(get_db)):
    """Get and apply the next simulation event."""
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")

    provider = get_provider(matchup_id)
    event = provider.get_next_event()

    if not event:
        return {"done": True, "message": "All simulation events exhausted"}

    # Apply score to DB
    pname = event["player_name"]
    # Find roster entry for this player in this matchup
    pr = (
        db.query(PlayerRoster)
        .join(PlayerRoster.nfl_player)
        .filter(
            PlayerRoster.matchup_id == matchup_id,
        )
        .filter(PlayerRoster.nfl_player.has(name=pname))
        .first()
    )
    if pr:
        pr.raw_score = event["new_raw_score"]
        pr.adjusted_score = event["new_raw_score"]
        pr.game_status = event.get("game_status", "in_progress")

    # Log event
    db.add(Event(
        sequence_number=matchup.simulation_event_index + 1,
        matchup_id=matchup_id,
        event_type=EventType.SCORE_UPDATE,
        week_number=matchup.week.week_number if matchup.week else 7,
        description=f"[DEMO] {event['description']}",
        data={**event, "is_simulated": True},
        is_simulated=True,
        is_confirmed=False,
    ))
    matchup.simulation_event_index += 1

    db.commit()
    return {"done": False, "event": event, "provider": provider.get_progress()}


@router.get("/{matchup_id}/status")
def get_simulation_status(matchup_id: int, db: Session = Depends(get_db)):
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")
    provider = get_provider(matchup_id)
    return {
        "matchup_id": matchup_id,
        "simulation_state": matchup.simulation_state,
        "simulation_speed": matchup.simulation_speed,
        "simulation_event_index": matchup.simulation_event_index,
        "provider": provider.get_progress(),
        "is_demo": True,
    }


@router.get("/{matchup_id}/sse")
async def sse_stream(matchup_id: int):
    """
    Server-Sent Events stream for live score updates.
    Frontend connects here to receive real-time updates.
    """
    async def event_generator():
        db = SessionLocal()
        try:
            provider = get_provider(matchup_id)
            matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()

            while True:
                try:
                    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
                    if not matchup:
                        break

                    if matchup.simulation_state == "running":
                        event = provider.get_next_event()
                        if event:
                            # Apply score update
                            pname = event["player_name"]
                            pr = (
                                db.query(PlayerRoster)
                                .join(PlayerRoster.nfl_player)
                                .filter(PlayerRoster.matchup_id == matchup_id)
                                .filter(PlayerRoster.nfl_player.has(name=pname))
                                .first()
                            )
                            if pr:
                                pr.raw_score = event["new_raw_score"]
                                pr.adjusted_score = event["new_raw_score"]
                                pr.game_status = event.get("game_status", "in_progress")
                                db.commit()

                            payload = {
                                "type": "score_update",
                                "player": pname,
                                "score": event["new_raw_score"],
                                "delta": event["score_delta"],
                                "description": event["description"],
                                "is_simulated": True,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                            yield f"data: {json.dumps(payload)}\n\n"

                            # Check if done
                            progress = provider.get_progress()
                            if progress["is_finished"]:
                                matchup.simulation_state = "finished"
                                db.commit()
                                yield f"data: {json.dumps({'type': 'simulation_complete'})}\n\n"
                        else:
                            yield f"data: {json.dumps({'type': 'heartbeat', 'ts': datetime.now(timezone.utc).isoformat()})}\n\n"

                    elif matchup.simulation_state in ("paused", "stopped"):
                        yield f"data: {json.dumps({'type': 'paused', 'state': matchup.simulation_state})}\n\n"

                    else:
                        yield f"data: {json.dumps({'type': 'heartbeat', 'ts': datetime.now(timezone.utc).isoformat()})}\n\n"

                    speed = matchup.simulation_speed or 1.0
                    delay = max(0.5, 3.0 / speed)
                    await asyncio.sleep(delay)

                except Exception as e:
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
                    await asyncio.sleep(2)
        finally:
            db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
