"""
Gold Ledger API
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.models import GoldTransaction, Team, Matchup

router = APIRouter()


@router.get("/team/{team_id}/ledger")
def get_gold_ledger(team_id: int, db: Session = Depends(get_db)):
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    transactions = (
        db.query(GoldTransaction)
        .filter(GoldTransaction.team_id == team_id)
        .order_by(GoldTransaction.timestamp)
        .all()
    )

    running_balance = 0.0
    entries = []
    for t in transactions:
        running_balance += t.amount
        entries.append({
            "id": t.id,
            "week": t.week_number,
            "type": t.transaction_type,
            "amount": t.amount,
            "description": t.description,
            "balance_after": running_balance,
            "is_provisional": t.is_provisional,
            "timestamp": t.timestamp.isoformat() if t.timestamp else None,
        })

    return {
        "team_id": team_id,
        "team_name": team.name,
        "current_gold": round(team.gold, 2),
        "computed_balance": round(running_balance, 2),
        "transactions": entries,
    }


@router.get("/matchup/{matchup_id}/summary")
def get_matchup_gold_summary(matchup_id: int, db: Session = Depends(get_db)):
    matchup = db.query(Matchup).filter(Matchup.id == matchup_id).first()
    if not matchup:
        raise HTTPException(status_code=404, detail="Matchup not found")

    def team_gold_summary(team_id: int) -> dict:
        transactions = (
            db.query(GoldTransaction)
            .filter(GoldTransaction.team_id == team_id, GoldTransaction.matchup_id == matchup_id)
            .all()
        )
        opening = next((t.amount for t in transactions if t.transaction_type == "opening"), 0)
        race_income = sum(t.amount for t in transactions if t.transaction_type == "race_income")
        purchases = sum(t.amount for t in transactions if t.transaction_type == "purchase")
        other = sum(t.amount for t in transactions if t.transaction_type not in ("opening", "race_income", "purchase"))
        return {
            "opening": opening,
            "race_income": race_income,
            "purchases": purchases,
            "other": other,
            "closing": opening + race_income + purchases + other,
        }

    return {
        "matchup_id": matchup_id,
        "home": {"team_id": matchup.home_team_id, **team_gold_summary(matchup.home_team_id)},
        "away": {"team_id": matchup.away_team_id, **team_gold_summary(matchup.away_team_id)},
    }
