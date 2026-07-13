"""
Admin API — Commissioner controls, rulings, manual entries.
"""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

from app.core.database import get_db
from app.models.models import (
    Matchup, PlayerRoster, CommissionerRuling, Ambiguity, Event,
    EventType, MatchupStatus, Attack, AttackStatus
)
from app.core.seed_demo import run_seed

router = APIRouter()


class ManualScoreEntry(BaseModel):
    player_roster_id: int
    new_raw_score: float
    note: str = ""
    entered_by: str = "admin"


class RulingRequest(BaseModel):
    matchup_id: int
    team_id: int
    ruling_type: str = "score_adjustment"
    description: str
    score_adjustment: float = 0.0
    gold_adjustment: float = 0.0
    issued_by: str = "Commissioner"


class AmbiguityResolution(BaseModel):
    ambiguity_id: int
    status: str  # "approved" or "rejected"
    commissioner_note: str = ""
    approved_by: str = "Commissioner"


class SimControl(BaseModel):
    action: str  # start, pause, resume, stop, restart
    speed: Optional[float] = None


@router.post("/seed")
def seed_database(db: Session = Depends(get_db)):
    """Seed the demo matchup data."""
    matchup = run_seed(db)
    return {"status": "seeded", "matchup_id": matchup.id}


@router.post("/score/manual")
def manual_score_entry(entry: ManualScoreEntry, db: Session = Depends(get_db)):
    """Manually edit a player's raw score. Creates audit event."""
    pr = db.query(PlayerRoster).filter(PlayerRoster.id == entry.player_roster_id).first()
    if not pr:
        raise HTTPException(status_code=404, detail="PlayerRoster not found")

    matchup = db.query(Matchup).filter(Matchup.id == pr.matchup_id).first()
    if matchup and matchup.status == MatchupStatus.FINALIZED:
        raise HTTPException(status_code=400, detail="Cannot edit scores after finalization")

    old_score = pr.raw_score
    pr.raw_score = entry.new_raw_score
    pr.adjusted_score = entry.new_raw_score
    pr.scoring_source = "manual_entry"

    # Audit event
    db.add(Event(
        sequence_number=9999,
        matchup_id=pr.matchup_id,
        event_type=EventType.MANUAL_SCORE_ENTRY,
        week_number=matchup.week.week_number if matchup and matchup.week else 0,
        team_id=pr.team_id,
        player_roster_id=pr.id,
        description=f"Manual score entry: {pr.nfl_player.name if pr.nfl_player else 'Unknown'} {old_score} → {entry.new_raw_score}",
        data={
            "old_score": old_score,
            "new_score": entry.new_raw_score,
            "entered_by": entry.entered_by,
            "note": entry.note,
        },
        is_simulated=False,
        is_confirmed=True,
    ))
    db.commit()
    return {"status": "updated", "player_roster_id": entry.player_roster_id, "new_score": entry.new_raw_score}


@router.post("/ruling")
def add_commissioner_ruling(ruling: RulingRequest, db: Session = Depends(get_db)):
    """Add a commissioner ruling."""
    r = CommissionerRuling(
        matchup_id=ruling.matchup_id,
        week_number=7,
        ruling_type=ruling.ruling_type,
        description=ruling.description,
        team_id=ruling.team_id,
        score_adjustment=ruling.score_adjustment,
        gold_adjustment=ruling.gold_adjustment,
        issued_by=ruling.issued_by,
        issued_at=datetime.now(timezone.utc),
        is_applied=False,
    )
    db.add(r)
    db.add(Event(
        sequence_number=9999,
        matchup_id=ruling.matchup_id,
        event_type=EventType.COMMISSIONER_RULING,
        week_number=7,
        description=f"Commissioner ruling: {ruling.description}",
        data={"ruling": ruling.dict()},
        is_simulated=False,
        is_confirmed=False,
    ))
    db.commit()
    return {"status": "ruling_added", "ruling_id": r.id}


@router.post("/ruling/{ruling_id}/approve")
def approve_ruling(ruling_id: int, db: Session = Depends(get_db)):
    """Apply a pending commissioner ruling."""
    r = db.query(CommissionerRuling).filter(CommissionerRuling.id == ruling_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Ruling not found")
    r.is_applied = True
    r.applied_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "applied", "ruling_id": ruling_id}


@router.post("/ambiguity/resolve")
def resolve_ambiguity(resolution: AmbiguityResolution, db: Session = Depends(get_db)):
    """Commissioner approves or rejects a provisional ambiguity."""
    amb = db.query(Ambiguity).filter(Ambiguity.id == resolution.ambiguity_id).first()
    if not amb:
        raise HTTPException(status_code=404, detail="Ambiguity not found")
    amb.status = resolution.status
    amb.commissioner_note = resolution.commissioner_note
    amb.approved_by = resolution.approved_by
    amb.approved_at = datetime.now(timezone.utc)

    if amb.matchup_id:
        db.add(Event(
            sequence_number=9999,
            matchup_id=amb.matchup_id,
            event_type=EventType.AMBIGUITY_RESOLVED,
            week_number=7,
            description=f"Ambiguity {resolution.status}: {amb.title}",
            data={"ambiguity_id": resolution.ambiguity_id, "note": resolution.commissioner_note},
            is_simulated=False,
            is_confirmed=True,
        ))
    db.commit()
    return {"status": "resolved", "ambiguity_id": resolution.ambiguity_id, "new_status": resolution.status}


@router.post("/matchup/{matchup_id}/finalize")
def finalize_matchup(matchup_id: int, db: Session = Depends(get_db)):
    """Finalize a matchup after correction window."""
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")
    matchup.status = MatchupStatus.FINALIZED
    matchup.finalized_at = datetime.now(timezone.utc)
    matchup.simulation_state = "finished"
    db.commit()
    return {"status": "finalized", "matchup_id": matchup_id}


@router.post("/matchup/{matchup_id}/reopen")
def reopen_matchup(matchup_id: int, db: Session = Depends(get_db)):
    """Reopen a finalized matchup for corrections."""
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")
    matchup.status = MatchupStatus.REOPENED
    matchup.finalized_at = None
    db.commit()
    return {"status": "reopened", "matchup_id": matchup_id}


@router.get("/matchup/{matchup_id}/audit-report")
def export_audit_report(matchup_id: int, db: Session = Depends(get_db)):
    """Export full audit report for a matchup."""
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")

    events = db.query(Event).filter(Event.matchup_id == matchup_id).order_by(Event.sequence_number).all()
    attacks = db.query(Attack).filter(Attack.matchup_id == matchup_id).all()
    rulings = db.query(CommissionerRuling).filter(CommissionerRuling.matchup_id == matchup_id).all()

    return {
        "matchup_id": matchup_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": matchup.status.value,
        "home_team": matchup.home_team.name if matchup.home_team else "",
        "away_team": matchup.away_team.name if matchup.away_team else "",
        "home_raw_score": matchup.home_raw_score,
        "away_raw_score": matchup.away_raw_score,
        "home_adjusted_score": matchup.home_adjusted_score,
        "away_adjusted_score": matchup.away_adjusted_score,
        "events": [
            {"seq": e.sequence_number, "type": e.event_type.value, "desc": e.description, "data": e.data}
            for e in events
        ],
        "attacks": [
            {
                "item": a.item_id,
                "status": a.status.value,
                "blocked_by": a.blocked_by,
                "invalidation_reason": a.invalidation_reason,
                "score_effect": a.score_effect,
            }
            for a in attacks
        ],
        "rulings": [
            {"desc": r.description, "adj": r.score_adjustment, "applied": r.is_applied}
            for r in rulings
        ],
        "disclaimer": "DEMO — All data is simulated.",
    }


@router.get("/rules")
def list_rules():
    """List all implemented rules."""
    from app.rules.catalog import RULES_CATALOG
    return {"rules": RULES_CATALOG, "count": len(RULES_CATALOG)}


@router.get("/ambiguities")
def list_ambiguities():
    """List all ambiguities from the register."""
    from app.rules.ambiguity_register import AMBIGUITIES
    return {"ambiguities": AMBIGUITIES, "count": len(AMBIGUITIES)}
