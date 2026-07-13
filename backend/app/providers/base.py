"""
Live Data Provider Interface.
All providers must implement ScoringProvider.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional


class ScoringProvider(ABC):
    """Interface for all scoring data providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""
        ...

    @property
    @abstractmethod
    def is_simulated(self) -> bool:
        """True if data is simulated/demo, not real."""
        ...

    @abstractmethod
    def get_player_score(self, player_name: str, matchup_id: int) -> Optional[float]:
        """Return current raw score for a player."""
        ...

    @abstractmethod
    def get_all_scores(self, matchup_id: int) -> dict[str, float]:
        """Return {player_name: raw_score} for all players in matchup."""
        ...
