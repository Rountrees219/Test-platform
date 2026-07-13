"""
ManualEntryProvider — Administrator manually enters/edits player scores.
Every edit is logged as an audit event.
"""
from __future__ import annotations
from typing import Optional
from .base import ScoringProvider


class ManualEntryProvider(ScoringProvider):
    """Provider where all scores are entered manually by the administrator."""

    def __init__(self):
        self._scores: dict[str, float] = {}
        self._history: list[dict] = []

    @property
    def name(self) -> str:
        return "ManualEntryProvider (scores entered manually by administrator)"

    @property
    def is_simulated(self) -> bool:
        return False  # Could be real data

    def set_score(self, player_name: str, score: float, entered_by: str = "admin", note: str = ""):
        old = self._scores.get(player_name, 0.0)
        self._scores[player_name] = score
        self._history.append({
            "player_name": player_name,
            "old_score": old,
            "new_score": score,
            "entered_by": entered_by,
            "note": note,
            "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        })

    def get_player_score(self, player_name: str, matchup_id: int) -> Optional[float]:
        return self._scores.get(player_name)

    def get_all_scores(self, matchup_id: int) -> dict[str, float]:
        return dict(self._scores)

    def get_history(self) -> list[dict]:
        return list(self._history)
