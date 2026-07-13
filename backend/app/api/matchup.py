"""
Matchup API — main scoring dashboard data.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from typing import Optional

from app.core.database import get_db
from app.models.models import (
    Matchup, Team, PlayerRoster, Attack, AuditEntry,
    GoldTransaction, Ambiguity, CommissionerRuling, Event,
    MatchupStatus
)
from app.rules.engine import RulesEngine, MatchupState, TeamState, PlayerState
from app.core.seed_demo import run_seed

router = APIRouter()

engine = RulesEngine()


def _build_player_state(pr: PlayerRoster) -> PlayerState:
    nfl = pr.nfl_player
    return PlayerState(
        roster_id=pr.id,
        name=nfl.name if nfl else "Unknown",
        nfl_team=nfl.nfl_team if nfl else "",
        position=nfl.position if nfl else "",
        slot=pr.slot.value if pr.slot else "BN1",
        is_starter=pr.is_starter,
        raw_score=pr.raw_score or 0.0,
        adjusted_score=pr.adjusted_score or 0.0,
        status=pr.status.value if pr.status else "active",
        is_protected=pr.is_protected or False,
        protection_source=pr.protection_source or "",
        is_dead=pr.is_dead or False,
        death_source=pr.death_source or "",
        applied_effects=pr.applied_effects or [],
        game_status=pr.game_status or "scheduled",
        opponent_nfl_team=pr.opponent_nfl_team or "",
        is_home_game=pr.is_home_game if pr.is_home_game is not None else True,
        is_night_game=pr.is_night_game or False,
        is_rookie=nfl.is_rookie if nfl else False,
        previous_week_score=pr.previous_week_score or 0.0,
        previous_week_ones_digit=pr.previous_week_ones_digit or 0,
        scoring_source=pr.scoring_source or "mock",
    )


def _build_team_state(team: Team, rosters: list, db: Session) -> TeamState:
    perks_active = [tp.perk_id for tp in team.perks if tp.is_active_this_week]
    inv = [i.item_id for i in team.inventory if not i.is_used]
    return TeamState(
        team_id=team.id,
        name=team.name,
        owner_name=team.owner_name,
        race=team.race.value if team.race else "Human",
        gold=team.gold or 500.0,
        players=[_build_player_state(pr) for pr in rosters],
        perks=perks_active,
        inventory=inv,
    )


def _serialize_player(p: PlayerState, roster: Optional[PlayerRoster] = None) -> dict:
    return {
        "roster_id": p.roster_id,
        "name": p.name,
        "nfl_team": p.nfl_team,
        "position": p.position,
        "slot": p.slot,
        "is_starter": p.is_starter,
        "status": p.status,
        "raw_score": round(p.raw_score, 2),
        "adjusted_score": round(p.adjusted_score, 2),
        "projected_score": round(roster.projected_score, 2) if roster else 0.0,
        "applied_effects": p.applied_effects,
        "is_protected": p.is_protected,
        "protection_source": p.protection_source,
        "is_dead": p.is_dead,
        "death_source": p.death_source,
        "game_status": p.game_status,
        "opponent_nfl_team": p.opponent_nfl_team,
        "is_home_game": p.is_home_game,
        "is_night_game": p.is_night_game,
        "is_rookie": p.is_rookie,
        "scoring_source": p.scoring_source,
        "previous_week_score": p.previous_week_score,
        "previous_week_ones_digit": p.previous_week_ones_digit,
    }


@router.get("/demo")
def get_demo_matchup(db: Session = Depends(get_db)):
    """Get or create and return the demo matchup with full scoring state."""
    matchup = db.query(Matchup).filter(Matchup.is_demo == True).first()
    if not matchup:
        run_seed(db)
        matchup = db.query(Matchup).filter(Matchup.is_demo == True).first()

    return _get_matchup_data(matchup.id, db)


@router.get("/{matchup_id}")
def get_matchup(matchup_id: int, db: Session = Depends(get_db)):
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")
    return _get_matchup_data(matchup_id, db)


def _get_matchup_data(matchup_id: int, db: Session) -> dict:
    matchup = (
        db.query(Matchup)
        .options(
            joinedload(Matchup.home_team).joinedload(Team.perks),
            joinedload(Matchup.home_team).joinedload(Team.inventory),
            joinedload(Matchup.away_team).joinedload(Team.perks),
            joinedload(Matchup.away_team).joinedload(Team.inventory),
        )
        .filter(Matchup.id == matchup_id)
        .first()
    )

    home_rosters = (
        db.query(PlayerRoster)
        .options(joinedload(PlayerRoster.nfl_player))
        .filter(PlayerRoster.matchup_id == matchup_id, PlayerRoster.team_id == matchup.home_team_id)
        .all()
    )
    away_rosters = (
        db.query(PlayerRoster)
        .options(joinedload(PlayerRoster.nfl_player))
        .filter(PlayerRoster.matchup_id == matchup_id, PlayerRoster.team_id == matchup.away_team_id)
        .all()
    )

    # Build attacks list for engine
    attacks = []
    db_attacks = db.query(Attack).filter(Attack.matchup_id == matchup_id).all()
    for atk in db_attacks:
        ed = atk.extra_data or {}
        attacks.append({
            "id": atk.id,
            "attacker_team_id": atk.attacker_team_id,
            "item_id": atk.item_id,
            "perk_id": atk.perk_id,
            "target_player_names": ed.get("target_player_names", []),
            "is_wooden": ed.get("is_wooden", False),
            "cost_gold": atk.cost_gold or 0,
            "status": atk.status.value if atk.status else "pending",
            "causes_death": ed.get("causes_death"),
            "invalidation_reason": atk.invalidation_reason,
            "blocked_by": atk.blocked_by,
            "score_effect": atk.score_effect or {},
        })

    # Build rulings
    rulings = []
    for r in db.query(CommissionerRuling).filter(CommissionerRuling.matchup_id == matchup_id).all():
        rulings.append({
            "id": r.id,
            "team_id": r.team_id,
            "description": r.description,
            "score_adjustment": r.score_adjustment or 0.0,
            "gold_adjustment": r.gold_adjustment or 0.0,
            "is_applied": r.is_applied,
            "issued_by": r.issued_by,
        })

    home_state = _build_team_state(matchup.home_team, home_rosters, db)
    away_state = _build_team_state(matchup.away_team, away_rosters, db)

    state = MatchupState(
        matchup_id=matchup_id,
        week=matchup.week.week_number if matchup.week else 7,
        home=home_state,
        away=away_state,
        attacks=attacks,
        rulings=rulings,
    )

    # Run the scoring engine
    engine.calculate(state)

    # Persist updated scores back to DB
    _persist_scores(matchup, state, db)

    # Build response
    roster_map = {pr.id: pr for pr in home_rosters + away_rosters}

    return {
        "matchup_id": matchup_id,
        "week": state.week,
        "status": matchup.status.value,
        "simulation_state": matchup.simulation_state,
        "simulation_speed": matchup.simulation_speed,
        "is_demo": matchup.is_demo,
        "last_updated": matchup.last_updated.isoformat() if matchup.last_updated else None,
        "is_provisional": state.is_provisional,
        "provisional_winner": state.provisional_winner(),
        "home": _serialize_team(state.home, roster_map),
        "away": _serialize_team(state.away, roster_map),
        "attacks": _serialize_attacks(db_attacks),
        "audit_trail": [t.to_dict() for t in state.traces[-50:]],  # last 50
        "gold_transactions": state.gold_transactions,
        "deaths": state.deaths,
        "ambiguities": [
            {
                "id": a.id,
                "rule_id": a.rule_id,
                "title": a.title,
                "description": a.description,
                "provisional_interpretation": a.provisional_interpretation,
                "status": a.status,
            }
            for a in db.query(Ambiguity).filter(Ambiguity.matchup_id == matchup_id).all()
        ],
        "rulings": rulings,
        "disclaimer": "DEMO DATA — All player scores are simulated. This application is independent of footbolzano.com.",
    }


def _persist_scores(matchup: Matchup, state: MatchupState, db: Session):
    matchup.home_raw_score = state.home.raw_team_score
    matchup.away_raw_score = state.away.raw_team_score
    matchup.home_adjusted_score = state.home.adjusted_team_score
    matchup.away_adjusted_score = state.away.adjusted_team_score

    winner_name = state.provisional_winner()
    if winner_name:
        if winner_name == state.home.name:
            matchup.provisional_winner_id = matchup.home_team_id
        else:
            matchup.provisional_winner_id = matchup.away_team_id

    db.commit()


def _serialize_team(t: TeamState, roster_map: dict) -> dict:
    return {
        "team_id": t.team_id,
        "name": t.name,
        "owner_name": t.owner_name,
        "race": t.race,
        "gold": round(t.gold, 2),
        "race_gold_earned": round(t.race_gold_earned, 2),
        "raw_score": round(t.raw_team_score, 2),
        "adjusted_score": round(t.adjusted_team_score, 2),
        "bench_score": round(t.bench_score, 2),
        "applied_effects": t.applied_team_effects,
        "perks": t.perks,
        "players": [_serialize_player(p, roster_map.get(p.roster_id)) for p in t.players],
    }


def _serialize_attacks(attacks: list) -> list:
    result = []
    for a in attacks:
        ed = a.extra_data or {}
        result.append({
            "id": a.id,
            "attacker_team_id": a.attacker_team_id,
            "item_id": a.item_id,
            "perk_id": a.perk_id,
            "target_players": ed.get("target_player_names", []),
            "cost_gold": a.cost_gold,
            "status": a.status.value if a.status else "pending",
            "validation_result": a.validation_result,
            "score_effect": a.score_effect,
            "blocked_by": a.blocked_by,
            "invalidation_reason": a.invalidation_reason,
        })
    return result
