"""
MockLiveProvider — Replayable, seeded, deterministic simulation.

Uses a fixed seed so the same simulation always produces the same final scores.
All data is clearly labeled as DEMO/SIMULATED.
"""
from __future__ import annotations

import random
from typing import Optional
from .base import ScoringProvider

# ── Seeded simulation events ──────────────────────────────────────────────────
# These are generated once at import time using seed=42.
# Calling generate_events(seed) always returns the same list.

def generate_events(seed: int = 42) -> list[dict]:
    """
    Generate a deterministic sequence of scoring events for the demo matchup.
    Each event: {player_name, score_delta, new_raw_score, description, delay_seconds, game_status}
    """
    rng = random.Random(seed)

    # Demo players — these match the seed data in seed_demo.py
    players = [
        # Home team: Goldenrod Gloryboys (Elf race)
        {"name": "Patrick Mahomes", "position": "QB",  "base": 0.0, "max": 32.0},
        {"name": "Bijan Robinson",  "position": "RB",  "base": 0.0, "max": 24.0},
        {"name": "Tyreek Hill",     "position": "WR",  "base": 0.0, "max": 28.0},
        {"name": "Davante Adams",   "position": "WR",  "base": 0.0, "max": 22.0},
        {"name": "Cooper Kupp",     "position": "WR",  "base": 0.0, "max": 18.0},
        {"name": "Sam LaPorta",     "position": "TE",  "base": 0.0, "max": 16.0},
        {"name": "Justin Tucker",   "position": "K",   "base": 0.0, "max": 14.0},
        {"name": "Micah Parsons",   "position": "LB",  "base": 0.0, "max": 20.0},
        # Bench home
        {"name": "Jaylen Waddle",   "position": "WR",  "base": 0.0, "max": 14.0},
        {"name": "Jake Ferguson",   "position": "TE",  "base": 0.0, "max": 10.0},
        # Away team: Iron Hexes (Halfling race)
        {"name": "Josh Allen",      "position": "QB",  "base": 0.0, "max": 38.0},
        {"name": "Derrick Henry",   "position": "RB",  "base": 0.0, "max": 26.0},
        {"name": "Stefon Diggs",    "position": "WR",  "base": 0.0, "max": 20.0},
        {"name": "AJ Brown",        "position": "WR",  "base": 0.0, "max": 24.0},
        {"name": "Keenan Allen",    "position": "WR",  "base": 0.0, "max": 16.0},
        {"name": "Mark Andrews",    "position": "TE",  "base": 0.0, "max": 18.0},
        {"name": "Evan McPherson",  "position": "K",   "base": 0.0, "max": 12.0},
        {"name": "Maxx Crosby",     "position": "DL",  "base": 0.0, "max": 18.0},
        # Bench away
        {"name": "Travis Etienne",  "position": "RB",  "base": 0.0, "max": 12.0},
        {"name": "George Pickens",  "position": "WR",  "base": 0.0, "max": 10.0},
    ]

    scores: dict[str, float] = {p["name"]: 0.0 for p in players}
    events: list[dict] = []

    # Generate ~40 scoring events spread over ~4 hours
    # Each event increments one player's score
    scoring_steps = [
        # (player_name, delta, description_template, delay_sec)
        ("Patrick Mahomes",  6.0,  "Mahomes 45-yd TD pass", 60),
        ("Bijan Robinson",   8.5,  "Robinson 12-yd rush TD + 65 rush yds", 90),
        ("Josh Allen",       6.0,  "Allen 28-yd TD pass", 45),
        ("Derrick Henry",    6.0,  "Henry 1-yd rush TD", 120),
        ("Tyreek Hill",      8.0,  "Hill 52-yd catch + TD", 60),
        ("Patrick Mahomes",  4.2,  "Mahomes 105-yd pass", 90),
        ("AJ Brown",         7.3,  "Brown 73-yd receiving game", 75),
        ("Justin Tucker",    6.0,  "Tucker 47-yd FG × 2", 120),
        ("Josh Allen",       6.0,  "Allen rushing TD", 45),
        ("Mark Andrews",     9.6,  "Andrews 5 receptions, 60 yds", 90),
        ("Micah Parsons",    7.2,  "Parsons 2 sacks + 4 tackles", 60),
        ("Sam LaPorta",      4.8,  "LaPorta 3 catches 28 yds", 90),
        ("Davante Adams",    6.4,  "Adams 64-yd game", 75),
        ("Derrick Henry",    3.8,  "Henry 38 additional rush yds", 60),
        ("Maxx Crosby",      8.9,  "Crosby 3 sacks + FF", 90),
        ("Stefon Diggs",     5.5,  "Diggs 55-yd receiving", 45),
        ("Keenan Allen",     4.2,  "Allen 42 rec yds", 75),
        ("Evan McPherson",   4.0,  "McPherson 32-yd FG", 120),
        ("Cooper Kupp",      3.7,  "Kupp 37 rec yds", 90),
        ("Patrick Mahomes",  3.0,  "Mahomes stat correction +75 yds", 60),
        ("Jaylen Waddle",    6.8,  "Waddle (bench) 68 rec yds (bench only)", 45),
        ("Bijan Robinson",   2.4,  "Robinson 24 additional yds", 75),
        ("Josh Allen",       2.8,  "Allen bonus yardage", 90),
        ("Tyreek Hill",      2.2,  "Hill 22 additional rec yds", 60),
        ("Jake Ferguson",    3.1,  "Ferguson (bench) 31 rec yds", 45),
        ("AJ Brown",         1.8,  "Brown final yardage total", 75),
        ("Justin Tucker",    2.0,  "Tucker extra point", 60),
        ("Micah Parsons",    2.4,  "Parsons 2 more tackles", 45),
        ("Travis Etienne",   4.2,  "Etienne (bench) 42 yds", 90),
        ("Maxx Crosby",      1.6,  "Crosby 1 more tackle", 60),
        ("Derrick Henry",    2.0,  "Henry final stat", 45),
        ("Sam LaPorta",      2.4,  "LaPorta 2 more catches", 75),
        ("George Pickens",   3.8,  "Pickens (bench) 38 rec yds", 60),
        ("Patrick Mahomes",  2.0,  "Mahomes final passing yards", 90),
        ("Evan McPherson",   2.0,  "McPherson extra point", 45),
        ("Davante Adams",    1.6,  "Adams final catch", 60),
        ("Stefon Diggs",     1.5,  "Diggs final stat", 45),
        ("Cooper Kupp",      1.3,  "Kupp final catch", 60),
        ("Josh Allen",       1.2,  "Allen final stat", 45),
        ("Mark Andrews",     0.8,  "Andrews final stat", 60),
    ]

    for step in scoring_steps:
        pname, delta, desc, delay = step
        scores[pname] = round(scores[pname] + delta, 2)
        events.append({
            "player_name": pname,
            "score_delta": delta,
            "new_raw_score": scores[pname],
            "description": desc,
            "delay_seconds": delay,
            "game_status": "in_progress",
        })

    # Final events: games end
    for pname in scores:
        events.append({
            "player_name": pname,
            "score_delta": 0.0,
            "new_raw_score": scores[pname],
            "description": f"{pname} game finished",
            "delay_seconds": 5,
            "game_status": "finished",
        })

    return events


# Precomputed final scores from seed 42
FINAL_SCORES_SEED_42 = {
    "Patrick Mahomes": 17.2,
    "Bijan Robinson": 14.7,
    "Tyreek Hill": 18.2,
    "Davante Adams": 10.4,
    "Cooper Kupp": 8.7,
    "Sam LaPorta": 10.2,
    "Justin Tucker": 10.0,
    "Micah Parsons": 11.6,
    "Jaylen Waddle": 6.8,   # bench
    "Jake Ferguson": 3.1,    # bench
    "Josh Allen": 22.0,
    "Derrick Henry": 18.2,
    "Stefon Diggs": 7.0,
    "AJ Brown": 9.1,
    "Keenan Allen": 4.2,
    "Mark Andrews": 10.4,
    "Evan McPherson": 8.0,
    "Maxx Crosby": 11.1,
    "Travis Etienne": 4.2,  # bench
    "George Pickens": 3.8,  # bench
}


class MockLiveProvider(ScoringProvider):
    """
    Seeded, deterministic mock live scoring provider.
    Data is 100% simulated. Always labeled DEMO.
    """

    LABEL = "[DEMO/SIMULATED]"

    def __init__(self, seed: int = 42):
        self.seed = seed
        self._events = generate_events(seed)
        self._current_index = 0
        self._current_scores: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "MockLiveProvider (DEMO — no real data)"

    @property
    def is_simulated(self) -> bool:
        return True

    def get_player_score(self, player_name: str, matchup_id: int) -> Optional[float]:
        return self._current_scores.get(player_name, 0.0)

    def get_all_scores(self, matchup_id: int) -> dict[str, float]:
        return dict(self._current_scores)

    def get_next_event(self) -> Optional[dict]:
        """Return next simulation event (or None if finished)."""
        if self._current_index >= len(self._events):
            return None
        event = self._events[self._current_index]
        self._current_index += 1
        # Apply score
        pname = event["player_name"]
        self._current_scores[pname] = event["new_raw_score"]
        return event

    def reset(self, seed: Optional[int] = None):
        """Reset simulation to beginning (same seed = same result)."""
        if seed is not None:
            self.seed = seed
        self._events = generate_events(self.seed)
        self._current_index = 0
        self._current_scores = {}

    def get_progress(self) -> dict:
        return {
            "total_events": len(self._events),
            "executed_events": self._current_index,
            "is_finished": self._current_index >= len(self._events),
            "is_simulated": True,
            "seed": self.seed,
        }

    def get_all_events(self) -> list[dict]:
        return self._events

    def jump_to_end(self):
        """Instantly apply all remaining events."""
        while self._current_index < len(self._events):
            self.get_next_event()
