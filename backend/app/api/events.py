"""
Events / Timeline API
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.core.database import get_db
from app.models.models import Event, Attack, CommissionerRuling, Matchup, EventType

router = APIRouter()


@router.get("/matchup/{matchup_id}/timeline")
def get_timeline(
    matchup_id: int,
    event_type: Optional[str] = None,
    team_id: Optional[int] = None,
    confirmed_only: bool = False,
    db: Session = Depends(get_db)
):
    query = db.query(Event).filter(Event.matchup_id == matchup_id)

    if event_type:
        try:
            et = EventType(event_type)
            query = query.filter(Event.event_type == et)
        except ValueError:
            pass

    if team_id:
        query = query.filter(Event.team_id == team_id)

    if confirmed_only:
        query = query.filter(Event.is_confirmed == True)

    events = query.order_by(Event.sequence_number).all()

    return {
        "matchup_id": matchup_id,
        "events": [
            {
                "id": e.id,
                "sequence": e.sequence_number,
                "type": e.event_type.value if e.event_type else "unknown",
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "week": e.week_number,
                "team_id": e.team_id,
                "player_roster_id": e.player_roster_id,
                "description": e.description,
                "data": e.data,
                "is_simulated": e.is_simulated,
                "is_confirmed": e.is_confirmed,
            }
            for e in events
        ],
    }
