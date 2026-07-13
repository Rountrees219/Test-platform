"""
Footbolzano Rules Engine — Deterministic Scoring Calculator.

All scoring adjustments are deterministic: same inputs always produce same outputs.
No AI or random judgment during runtime (mock data uses a seeded RNG only for
simulation event generation, not for rule application).

Execution Order (provisional — see ambiguity register):
  1.  Validate league state and submissions
  2.  Establish active rosters
  3.  Establish protections and immunities
  4.  Import raw NFL/platform scoring
  5.  Validate attacks and item targets
  6.  Apply score replacements
  7.  Apply score transfers
  8.  Apply player-specific: zeroing, division, multiplication, additions
  9.  Apply bench effects
  10. Calculate raw team totals
  11. Apply teamwide percentage effects
  12. Apply fixed team bonuses/penalties
  13. Calculate race gold
  14. Process purchases and gold transactions
  15. Process automatic win/loss conditions
  16. Process post-matchup deaths and persistent consequences
  17. Apply approved manual rulings
  18. Round according to selected policy
  19. Determine provisional winner
  20. Finalize after correction window
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime, timezone


# ── Trace / Audit ─────────────────────────────────────────────────────────────

@dataclass
class RuleTrace:
    """Every rule application produces one of these."""
    sequence: int
    timestamp: str
    week: int
    matchup_id: int
    rule_id: str
    rule_version: str = "v1"
    source_value: float = 0.0
    target: str = ""
    prior_value: float = 0.0
    resulting_value: float = 0.0
    did_fire: bool = True
    was_blocked: bool = False
    was_invalid: bool = False
    reason: str = ""
    related_attack_id: Optional[int] = None
    related_item_id: Optional[str] = None
    related_perk_id: Optional[str] = None
    related_ruling_id: Optional[int] = None
    is_provisional: bool = True
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "week": self.week,
            "matchup_id": self.matchup_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "source_value": self.source_value,
            "target": self.target,
            "prior_value": self.prior_value,
            "resulting_value": self.resulting_value,
            "did_fire": self.did_fire,
            "was_blocked": self.was_blocked,
            "was_invalid": self.was_invalid,
            "reason": self.reason,
            "related_attack_id": self.related_attack_id,
            "related_item_id": self.related_item_id,
            "related_perk_id": self.related_perk_id,
            "related_ruling_id": self.related_ruling_id,
            "is_provisional": self.is_provisional,
            "explanation": self.explanation,
        }


# ── Player State (in-memory for calculation) ──────────────────────────────────

@dataclass
class PlayerState:
    roster_id: int
    name: str
    nfl_team: str
    position: str  # QB/RB/WR/TE/K/DL/LB/DB
    slot: str       # QB, RB1, RB2, WR1 ... BN1 ...
    is_starter: bool
    raw_score: float = 0.0
    adjusted_score: float = 0.0
    status: str = "active"      # active/inactive/injured/bye/finished/dead
    is_protected: bool = False
    protection_source: str = ""
    is_dead: bool = False
    death_source: str = ""
    applied_effects: list = field(default_factory=list)
    is_zeroed: bool = False
    score_replaced: bool = False
    game_status: str = "scheduled"
    opponent_nfl_team: str = ""
    is_home_game: bool = True
    is_night_game: bool = False
    is_rookie: bool = False
    previous_week_score: float = 0.0
    previous_week_ones_digit: int = 0
    scoring_source: str = "mock"

    @property
    def is_bench(self) -> bool:
        return self.slot.startswith("BN")

    def add_effect(self, effect: str):
        if effect not in self.applied_effects:
            self.applied_effects.append(effect)

    def effective_score(self) -> float:
        """Final score this player contributes to team."""
        if self.is_dead and not self.is_starter:
            return 0.0
        return self.adjusted_score


@dataclass
class TeamState:
    team_id: int
    name: str
    owner_name: str
    race: str
    gold: float = 500.0
    players: list[PlayerState] = field(default_factory=list)
    perks: list[str] = field(default_factory=list)  # perk_ids active this week
    perk_classes: list[str] = field(default_factory=list)  # up to 2 classes
    inventory: list[str] = field(default_factory=list)  # item_ids owned
    raw_team_score: float = 0.0
    adjusted_team_score: float = 0.0
    race_gold_earned: float = 0.0
    bench_score: float = 0.0
    applied_team_effects: list = field(default_factory=list)

    def starters(self) -> list[PlayerState]:
        return [p for p in self.players if p.is_starter and not p.is_dead]

    def bench_players(self) -> list[PlayerState]:
        return [p for p in self.players if p.is_bench]

    def add_team_effect(self, effect: str):
        if effect not in self.applied_team_effects:
            self.applied_team_effects.append(effect)


@dataclass
class MatchupState:
    matchup_id: int
    week: int
    home: TeamState
    away: TeamState
    attacks: list[dict] = field(default_factory=list)
    rulings: list[dict] = field(default_factory=list)
    traces: list[RuleTrace] = field(default_factory=list)
    gold_transactions: list[dict] = field(default_factory=list)
    deaths: list[dict] = field(default_factory=list)
    ambiguities: list[dict] = field(default_factory=list)
    sequence_counter: int = 0
    is_provisional: bool = True

    def next_seq(self) -> int:
        self.sequence_counter += 1
        return self.sequence_counter

    def add_trace(self, **kwargs) -> RuleTrace:
        t = RuleTrace(
            sequence=self.next_seq(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=self.week,
            matchup_id=self.matchup_id,
            **kwargs
        )
        self.traces.append(t)
        return t

    def provisional_winner(self) -> Optional[str]:
        if self.home.adjusted_team_score > self.away.adjusted_team_score:
            return self.home.name
        elif self.away.adjusted_team_score > self.home.adjusted_team_score:
            return self.away.name
        return None  # tie

    def score_delta(self) -> float:
        return abs(self.home.adjusted_team_score - self.away.adjusted_team_score)


# ── Rules Engine ──────────────────────────────────────────────────────────────

class RulesEngine:
    """
    Deterministic rules engine.
    Call calculate(state) to run all rules in order.
    Returns the updated MatchupState with full trace.
    """

    VERSION = "v1"
    SOURCE_CITATION = "https://footbolzano.com/rules/ — 2025 Rulebook v1 (retrieved 2026-07-12)"

    def calculate(self, state: MatchupState) -> MatchupState:
        """Full deterministic calculation pipeline."""
        # Steps run in canonical order
        self._step1_validate_state(state)
        self._step2_establish_rosters(state)
        self._step3_establish_protections(state)
        # step4: raw scores already set by caller (from provider)
        self._step5_validate_attacks(state)
        self._step6_apply_score_replacements(state)
        self._step7_apply_score_transfers(state)
        self._step8_apply_player_effects(state)
        self._step9_apply_bench_effects(state)
        self._step10_raw_team_totals(state)
        self._step11_teamwide_percentage_effects(state)
        self._step12_fixed_team_bonuses(state)
        self._step13_race_gold(state)
        self._step14_gold_transactions(state)
        self._step15_auto_win_loss(state)
        self._step16_post_matchup_deaths(state)
        self._step17_manual_rulings(state)
        self._step18_round_scores(state)
        self._step19_provisional_winner(state)
        return state

    # ── Step 1: Validate ──────────────────────────────────────────────────────

    def _step1_validate_state(self, state: MatchupState):
        for team in [state.home, state.away]:
            starters = team.starters()
            # Check for duplicate player names in starters
            names = [p.name for p in starters]
            seen = set()
            for name in names:
                if name in seen:
                    state.add_trace(
                        rule_id="VAL_DUPLICATE_PLAYER",
                        target=f"{team.name}/{name}",
                        did_fire=False, was_invalid=True,
                        reason=f"Duplicate player '{name}' in starting lineup",
                        explanation=f"Validation: {name} appears more than once in {team.name}'s lineup. This is invalid per roster rules.",
                        is_provisional=False
                    )
                seen.add(name)

    # ── Step 2: Rosters ───────────────────────────────────────────────────────

    def _step2_establish_rosters(self, state: MatchupState):
        for team in [state.home, state.away]:
            for p in team.players:
                p.adjusted_score = p.raw_score  # start with raw = adjusted
                if p.status in ("bye", "inactive") and p.is_starter:
                    state.add_trace(
                        rule_id="ROSTER_BYE_INACTIVE",
                        target=f"{team.name}/{p.name}",
                        source_value=p.raw_score,
                        prior_value=p.raw_score,
                        resulting_value=p.raw_score,
                        did_fire=True,
                        reason=f"{p.name} is {p.status}",
                        explanation=f"{p.name} ({p.status}) contributes their available score. Owners are warned not to attack bye/IR players.",
                        is_provisional=False
                    )

    # ── Step 3: Protections ───────────────────────────────────────────────────

    def _step3_establish_protections(self, state: MatchupState):
        """Record which players have protections before attacks are processed."""
        for team in [state.home, state.away]:
            for p in team.players:
                if p.is_protected:
                    state.add_trace(
                        rule_id="PROT_ESTABLISHED",
                        target=f"{team.name}/{p.name}",
                        did_fire=True,
                        reason=f"Protected by {p.protection_source}",
                        explanation=f"{p.name} is protected this week by {p.protection_source}. Wooden attack weapons cannot affect this player.",
                        is_provisional=False
                    )

    # ── Step 5: Validate Attacks ──────────────────────────────────────────────

    # Items that may target own players (not strictly opponent)
    SELF_TARGET_ITEMS = frozenset({
        "adrenaline_potion", "martyr_perk", "mercenaries",
        "inspiring_sermon", "gamblers_luck", "mace",
        "meteorite", "arcane_shot", "understudy",
        "ivory_tower_defense",
    })

    def _step5_validate_attacks(self, state: MatchupState):
        for attack in state.attacks:
            self._validate_single_attack(state, attack)

    def _validate_single_attack(self, state: MatchupState, attack: dict):
        item_id = attack.get("item_id", "")
        attacker_team = state.home if attack.get("attacker_team_id") == state.home.team_id else state.away
        target_team = state.away if attacker_team is state.home else state.home
        target_player_names = attack.get("target_player_names", [])

        # Items that can target own players — skip opponent-only validation
        if item_id in self.SELF_TARGET_ITEMS:
            # Still check not dead
            for pname in target_player_names:
                found = next(
                    (p for t in [state.home, state.away] for p in t.players if p.name == pname),
                    None
                )
                if found and found.is_dead:
                    attack["status"] = "invalid"
                    attack["invalidation_reason"] = f"{pname} is already dead"
                    state.add_trace(
                        rule_id="ATK_INVALID_DEAD",
                        target=pname,
                        was_invalid=True, did_fire=False,
                        reason=f"{pname} is dead — cannot target dead players",
                        related_item_id=item_id,
                        explanation=f"Attack rejected: {pname} is already dead.",
                        is_provisional=False
                    )
                    return
            if attack.get("status") not in ("blocked", "invalid"):
                attack["status"] = "validated"
            return

        # Check: target is on opponent
        # Check: target is not dead
        for pname in target_player_names:
            found = next((p for p in target_team.players if p.name == pname), None)
            if not found:
                attack["status"] = "invalid"
                attack["invalidation_reason"] = f"Player '{pname}' not found on {target_team.name}"
                state.add_trace(
                    rule_id="ATK_INVALID_TARGET",
                    target=f"{attacker_team.name} attacks {pname}",
                    was_invalid=True, did_fire=False,
                    reason=f"Player '{pname}' not found on target team",
                    related_item_id=item_id,
                    explanation=f"Attack invalid: {pname} does not appear on {target_team.name}'s roster.",
                    is_provisional=False
                )
                return

            if found.is_dead:
                attack["status"] = "invalid"
                attack["invalidation_reason"] = f"{pname} is already dead"
                state.add_trace(
                    rule_id="ATK_INVALID_DEAD",
                    target=f"{attacker_team.name} attacks {pname}",
                    was_invalid=True, did_fire=False,
                    reason=f"{pname} is already dead — cannot attack dead players",
                    related_item_id=item_id,
                    explanation=f"Attack rejected: {pname} is already dead. Dead players cannot be the target of attacks.",
                    is_provisional=False
                )
                return

            if found.status == "bye":
                attack["status"] = "invalid"
                attack["invalidation_reason"] = f"{pname} is on bye — invalid target per rules"
                state.add_trace(
                    rule_id="ATK_INVALID_BYE",
                    target=f"{attacker_team.name} attacks {pname}",
                    was_invalid=True, did_fire=False,
                    reason=f"Attacking a player on bye is not allowed",
                    related_item_id=item_id,
                    explanation=f"Attack invalid: You may not attack a player you know will not play (bye). Per 2025 Rulebook.",
                    is_provisional=False
                )
                return

        # Check: protection blocks wooden weapons
        is_wooden = attack.get("is_wooden", False)
        if is_wooden:
            for pname in target_player_names:
                found = next((p for p in target_team.players if p.name == pname), None)
                if found and found.is_protected:
                    attack["status"] = "blocked"
                    attack["blocked_by"] = found.protection_source
                    state.add_trace(
                        rule_id="ATK_BLOCKED_PROTECTION",
                        target=f"{pname}",
                        prior_value=found.adjusted_score,
                        resulting_value=found.adjusted_score,
                        was_blocked=True, did_fire=False,
                        reason=f"{pname} is protected by {found.protection_source} — wooden attack blocked",
                        related_item_id=item_id,
                        explanation=f"Attack BLOCKED: {found.protection_source} grants immunity to wooden weapons. {item_id} has no effect on {pname}.",
                        is_provisional=False
                    )
                    return

        # Check: antidote blocks basic poison
        if item_id in ("basic_poison", "clandestine_poison"):
            for pname in target_player_names:
                found = next((p for p in target_team.players if p.name == pname), None)
                if found and "antidote" in target_team.perks:
                    attack["status"] = "blocked"
                    attack["blocked_by"] = "Antidote (Alchemist L1)"
                    state.add_trace(
                        rule_id="ATK_BLOCKED_ANTIDOTE",
                        target=pname,
                        was_blocked=True, did_fire=False,
                        reason=f"{target_team.name} has Antidote — basic poison blocked",
                        related_item_id=item_id,
                        explanation=f"Attack BLOCKED: {target_team.name} has the Antidote perk (Alchemist L1), which grants immunity to basic poison.",
                        is_provisional=False
                    )
                    return

        # Check arrows: previous week ones digit must be 0-3 (wooden) or 0-5 (steel)
        if item_id in ("wooden_arrows", "steel_arrows"):
            digit_max = 3 if item_id == "wooden_arrows" else 5
            for pname in target_player_names:
                found = next((p for p in target_team.players if p.name == pname), None)
                if found and found.previous_week_ones_digit > digit_max:
                    attack["status"] = "invalid"
                    attack["invalidation_reason"] = f"{pname}'s previous week ones digit ({found.previous_week_ones_digit}) > {digit_max}"
                    state.add_trace(
                        rule_id="ATK_INVALID_ARROWS_DIGIT",
                        target=pname,
                        was_invalid=True, did_fire=False,
                        reason=f"Arrow attack requires previous week score ones digit 0-{digit_max}; {pname} had {found.previous_week_ones_digit}",
                        related_item_id=item_id,
                        explanation=f"Attack invalid: {item_id.replace('_', ' ').title()} requires the target's previous-week score to have ones digit 0–{digit_max}. {pname}'s ones digit was {found.previous_week_ones_digit}.",
                        is_provisional=False
                    )
                    return

        # Passed all checks — mark validated
        if attack.get("status") not in ("blocked", "invalid"):
            attack["status"] = "validated"
            state.add_trace(
                rule_id="ATK_VALIDATED",
                target=str(target_player_names),
                did_fire=True,
                reason="Attack passed all validation checks",
                related_item_id=item_id,
                explanation=f"Attack using {item_id} targeting {target_player_names} is valid and will be applied.",
                is_provisional=True
            )

    # ── Step 6: Score Replacements ────────────────────────────────────────────

    def _step6_apply_score_replacements(self, state: MatchupState):
        """Apply Insurance Policy, Wooden Wand digit-reorder, etc."""
        for attack in state.attacks:
            if attack.get("status") != "validated":
                continue

            item_id = attack.get("item_id", "")
            attacker_team = state.home if attack.get("attacker_team_id") == state.home.team_id else state.away
            target_team = state.away if attacker_team is state.home else state.home

            if item_id in ("wooden_wand", "silver_wand"):
                for pname in attack.get("target_player_names", []):
                    p = next((x for x in target_team.players if x.name == pname), None)
                    if p:
                        old = p.adjusted_score
                        # Reorder digits left of decimal ascending
                        new_score = self._reorder_digits_ascending(p.adjusted_score)
                        p.adjusted_score = new_score
                        p.score_replaced = True
                        p.add_effect(f"{item_id}: digits reordered {old:.2f} → {new_score:.2f}")
                        attack["status"] = "applied"
                        state.add_trace(
                            rule_id=f"ATK_{item_id.upper()}",
                            target=f"{target_team.name}/{pname}",
                            source_value=old,
                            prior_value=old,
                            resulting_value=new_score,
                            related_item_id=item_id,
                            reason=f"Digits left of decimal reordered ascending: {old:.2f} → {new_score:.2f}",
                            explanation=f"{item_id.replace('_',' ').title()}: {pname}'s score {old:.2f} has its integer digits sorted ascending → {new_score:.2f}.",
                            is_provisional=True
                        )

    def _reorder_digits_ascending(self, score: float) -> float:
        """Sort digits left of decimal point in ascending order."""
        integer_part = int(score)
        decimal_part = score - integer_part
        digits = sorted(str(integer_part))
        # Remove leading zeros issue: if "0" becomes the whole number
        new_int = int("".join(digits)) if digits and "".join(digits).lstrip("0") else 0
        return new_int + decimal_part

    # ── Step 7: Score Transfers ───────────────────────────────────────────────

    def _step7_apply_score_transfers(self, state: MatchupState):
        """Animal Husbandry: opponent's player scores for you instead."""
        for team in [state.home, state.away]:
            if "animal_husbandry" in team.perks:
                animal_team = next(
                    (tp for tp in [state.home, state.away] if tp is team),
                    None
                )
                # Get extra_data for chosen NFL team
                chosen_nfl = None
                for tp_data in team.perks:
                    pass  # simplified — actual chosen_nfl from perk_extra_data
                # This is handled in seed data; mark as provisional
                state.add_trace(
                    rule_id="PERK_ANIMAL_HUSBANDRY",
                    target=team.name,
                    did_fire=False,
                    is_provisional=True,
                    reason="Animal Husbandry transfer requires perk extra_data with chosen NFL team",
                    explanation="PROVISIONAL: Animal Husbandry (Druid L2) — chosen opponent NFL team players score for you instead. Requires explicit commissioner configuration.",
                )

    # ── Step 8: Player-Specific Effects ──────────────────────────────────────

    def _step8_apply_player_effects(self, state: MatchupState):
        """Apply zeroing, halving, multipliers, additions at player level."""
        for attack in state.attacks:
            if attack.get("status") != "validated":
                continue

            item_id = attack.get("item_id", "")
            attacker_team = state.home if attack.get("attacker_team_id") == state.home.team_id else state.away
            target_team = state.away if attacker_team is state.home else state.home

            # ── Wooden Sword / Steel Sword ────────────────────────────────────
            if item_id == "wooden_sword":
                for pname in attack.get("target_player_names", []):
                    p = next((x for x in target_team.players if x.name == pname), None)
                    if p and not p.score_replaced:
                        old = p.adjusted_score
                        p.adjusted_score = old / 2
                        p.add_effect(f"Wooden Sword ÷2")
                        attack["status"] = "applied"
                        state.add_trace(
                            rule_id="ATK_WOODEN_SWORD",
                            target=f"{target_team.name}/{pname}",
                            source_value=old,
                            prior_value=old,
                            resulting_value=p.adjusted_score,
                            related_item_id="wooden_sword",
                            reason="Wooden Sword: player score halved",
                            explanation=f"Wooden Sword: {pname}'s score {old:.2f} ÷ 2 = {p.adjusted_score:.2f}.",
                            is_provisional=True
                        )

            elif item_id == "steel_sword":
                for pname in attack.get("target_player_names", []):
                    p = next((x for x in target_team.players if x.name == pname), None)
                    if p:
                        old = p.adjusted_score
                        p.adjusted_score = 0.0
                        p.is_zeroed = True
                        p.add_effect("Steel Sword → 0")
                        attack["status"] = "applied"
                        state.add_trace(
                            rule_id="ATK_STEEL_SWORD",
                            target=f"{target_team.name}/{pname}",
                            source_value=old,
                            prior_value=old,
                            resulting_value=0.0,
                            related_item_id="steel_sword",
                            reason="Steel Sword: player score set to 0",
                            explanation=f"Steel Sword: {pname}'s score {old:.2f} → 0.00 (zeroed).",
                            is_provisional=True
                        )

            # ── Basic Poison / Super Poison ───────────────────────────────────
            elif item_id == "basic_poison":
                for pname in attack.get("target_player_names", []):
                    p = next((x for x in target_team.players if x.name == pname), None)
                    if p:
                        old = p.adjusted_score
                        p.adjusted_score = old / 2
                        p.add_effect("Basic Poison ÷2")
                        attack["status"] = "applied"
                        state.add_trace(
                            rule_id="ATK_BASIC_POISON",
                            target=f"{target_team.name}/{pname}",
                            source_value=old,
                            prior_value=old,
                            resulting_value=p.adjusted_score,
                            related_item_id="basic_poison",
                            reason="Basic Poison: player score halved",
                            explanation=f"Basic Poison: {pname}'s score {old:.2f} ÷ 2 = {p.adjusted_score:.2f}.",
                            is_provisional=True
                        )

            elif item_id == "super_poison":
                # Halves highest-scoring active player
                active = sorted(
                    [p for p in target_team.starters()],
                    key=lambda x: x.adjusted_score,
                    reverse=True
                )
                if active:
                    p = active[0]
                    old = p.adjusted_score
                    p.adjusted_score = old / 2
                    p.add_effect("Super Poison ÷2")
                    attack["status"] = "applied"
                    state.add_trace(
                        rule_id="ATK_SUPER_POISON",
                        target=f"{target_team.name}/{p.name}",
                        source_value=old,
                        prior_value=old,
                        resulting_value=p.adjusted_score,
                        related_item_id="super_poison",
                        reason=f"Super Poison targets highest-scoring active player: {p.name}",
                        explanation=f"Super Poison: {p.name} (highest scorer {old:.2f}) halved → {p.adjusted_score:.2f}.",
                        is_provisional=True
                    )

            # ── Twin Wooden Daggers ───────────────────────────────────────────
            elif item_id == "twin_wooden_daggers":
                targets = attack.get("target_player_names", [])[:2]
                for pname in targets:
                    p = next((x for x in target_team.players if x.name == pname), None)
                    if p:
                        old = p.adjusted_score
                        p.adjusted_score = old * 0.75
                        p.add_effect("Twin Wooden Daggers ×0.75")
                        attack["status"] = "applied"
                        state.add_trace(
                            rule_id="ATK_TWIN_WOODEN_DAGGERS",
                            target=f"{target_team.name}/{pname}",
                            source_value=old,
                            prior_value=old,
                            resulting_value=p.adjusted_score,
                            related_item_id="twin_wooden_daggers",
                            reason="Twin Wooden Daggers: each target scores ¾",
                            explanation=f"Twin Wooden Daggers: {pname}'s score {old:.2f} × 0.75 = {p.adjusted_score:.2f}.",
                            is_provisional=True
                        )

            # ── Adrenaline Potion ─────────────────────────────────────────────
            elif item_id == "adrenaline_potion":
                for pname in attack.get("target_player_names", []):
                    # Adrenaline Potion can target ANY player (own or opponent)
                    p = next(
                        (x for t in [state.home, state.away] for x in t.players if x.name == pname),
                        None
                    )
                    if p:
                        old = p.adjusted_score
                        p.adjusted_score = old * 1.25
                        p.add_effect("Adrenaline Potion ×1.25")
                        attack["status"] = "applied"
                        state.add_trace(
                            rule_id="ATK_ADRENALINE_POTION",
                            target=pname,
                            source_value=old,
                            prior_value=old,
                            resulting_value=p.adjusted_score,
                            related_item_id="adrenaline_potion",
                            reason="Adrenaline Potion: player scores 1.25×",
                            explanation=f"Adrenaline Potion: {pname}'s score {old:.2f} × 1.25 = {p.adjusted_score:.2f}.",
                            is_provisional=True
                        )

            # ── Bribe (kicker scores 0) ───────────────────────────────────────
            elif item_id == "bribe":
                for team in [state.home, state.away]:
                    if team is target_team:
                        # Fairies are immune
                        if team.race == "Fairy":
                            attack["status"] = "blocked"
                            attack["blocked_by"] = "Fairy race immunity to Bribe"
                            state.add_trace(
                                rule_id="ATK_BRIBE_BLOCKED_FAIRY",
                                target=f"{team.name} kicker",
                                was_blocked=True, did_fire=False,
                                related_item_id="bribe",
                                reason="Fairies are immune to Bribe per rulebook",
                                explanation="Attack BLOCKED: Bribe has no effect on Fairy-race teams. The kicker's score is unchanged.",
                                is_provisional=False
                            )
                        else:
                            kicker = next((p for p in team.starters() if p.position == "K"), None)
                            if kicker:
                                old = kicker.adjusted_score
                                kicker.adjusted_score = 0.0
                                kicker.is_zeroed = True
                                kicker.add_effect("Bribe → 0")
                                attack["status"] = "applied"
                                state.add_trace(
                                    rule_id="ATK_BRIBE",
                                    target=f"{team.name}/{kicker.name}",
                                    source_value=old, prior_value=old, resulting_value=0.0,
                                    related_item_id="bribe",
                                    reason="Bribe: kicker score set to 0",
                                    explanation=f"Bribe: {kicker.name}'s score {old:.2f} → 0.00.",
                                    is_provisional=True
                                )

            # ── Wooden Axe ────────────────────────────────────────────────────
            elif item_id == "wooden_axe":
                targets = attack.get("target_player_names", [])[:3]
                target_players = [
                    p for p in target_team.players if p.name in targets
                ]
                if len(target_players) == 3:
                    sorted_players = sorted(target_players, key=lambda x: x.adjusted_score)
                    lowest_score = sorted_players[0].adjusted_score
                    highest = sorted_players[-1]
                    old = highest.adjusted_score
                    highest.adjusted_score = lowest_score
                    highest.add_effect(f"Wooden Axe: replaced by lowest ({lowest_score:.2f})")
                    attack["status"] = "applied"
                    state.add_trace(
                        rule_id="ATK_WOODEN_AXE",
                        target=f"{target_team.name}/{highest.name}",
                        source_value=old, prior_value=old, resulting_value=lowest_score,
                        related_item_id="wooden_axe",
                        reason=f"Wooden Axe: highest scorer's ({highest.name} {old:.2f}) score replaced by lowest ({lowest_score:.2f})",
                        explanation=f"Wooden Axe: Among {[p.name for p in target_players]}, {highest.name} had highest score {old:.2f}. Replaced with lowest scorer's {lowest_score:.2f}.",
                        is_provisional=True
                    )

            # ── Mercenaries (rookies ×1.5) ────────────────────────────────────
            elif item_id == "mercenaries":
                for p in attacker_team.starters():
                    if p.is_rookie:
                        old = p.adjusted_score
                        p.adjusted_score = old * 1.5
                        p.add_effect("Mercenaries ×1.5")
                        attack["status"] = "applied"
                        state.add_trace(
                            rule_id="ATK_MERCENARIES",
                            target=f"{attacker_team.name}/{p.name}",
                            source_value=old, prior_value=old, resulting_value=p.adjusted_score,
                            related_item_id="mercenaries",
                            reason=f"Mercenaries: rookie {p.name} ×1.5",
                            explanation=f"Mercenaries: {p.name} is a rookie. Score {old:.2f} × 1.5 = {p.adjusted_score:.2f}.",
                            is_provisional=True
                        )

            # ── Martyr (Necromancer L1) ───────────────────────────────────────
            elif item_id == "martyr_perk":
                for pname in attack.get("target_player_names", [])[:1]:
                    p = next((x for x in attacker_team.starters() if x.name == pname), None)
                    if p:
                        old = p.adjusted_score
                        p.adjusted_score = old * 2
                        p.add_effect("Martyr ×2 (dies at week end)")
                        attack["status"] = "applied"
                        attack["causes_death"] = pname
                        state.add_trace(
                            rule_id="PERK_MARTYR",
                            target=f"{attacker_team.name}/{pname}",
                            source_value=old, prior_value=old, resulting_value=p.adjusted_score,
                            related_perk_id="martyr",
                            reason="Martyr: player scores double, dies at week's end",
                            explanation=f"Martyr: {pname} scores double ({old:.2f} × 2 = {p.adjusted_score:.2f}). This player will die at week's end.",
                            is_provisional=True
                        )

        # ── Army Sergeant (Soldier L1) ────────────────────────────────────────
        for team in [state.home, state.away]:
            if "army_sergeant" in team.perks:
                strength_pos = self._race_strength_position(team.race)
                strength_players = [
                    p for p in team.starters()
                    if self._matches_strength_position(p.position, strength_pos)
                ]
                if strength_players:
                    top = max(strength_players, key=lambda x: x.adjusted_score)
                    old = top.adjusted_score
                    top.adjusted_score = old * 1.30
                    top.add_effect("Army Sergeant +30%")
                    state.add_trace(
                        rule_id="PERK_ARMY_SERGEANT",
                        target=f"{team.name}/{top.name}",
                        source_value=old, prior_value=old, resulting_value=top.adjusted_score,
                        related_perk_id="army_sergeant",
                        reason=f"Army Sergeant: highest-scoring {strength_pos} player gains +30%",
                        explanation=f"Army Sergeant (Soldier L1): {top.name} is the highest-scoring {strength_pos} at {old:.2f}. Score × 1.30 = {top.adjusted_score:.2f}.",
                        is_provisional=True
                    )

        # ── Call of the Wild (Druid L1) ───────────────────────────────────────
        for team in [state.home, state.away]:
            if "call_of_the_wild" in team.perks:
                # Extra data holds chosen NFL team
                # Provisional: mark ambiguity if not configured
                state.add_trace(
                    rule_id="PERK_CALL_OF_WILD",
                    target=team.name,
                    did_fire=False,
                    is_provisional=True,
                    reason="Call of the Wild requires configured chosen NFL team in perk extra_data",
                    explanation="PROVISIONAL: Call of the Wild (Druid L1) — all players facing the chosen NFL team score double. Requires perk configuration."
                )

    # ── Step 9: Bench Effects ─────────────────────────────────────────────────

    def _step9_apply_bench_effects(self, state: MatchupState):
        """Attack of the Squires, Reserve Forces, Second Shift Miners, etc."""

        for team in [state.home, state.away]:
            bench = team.bench_players()
            team.bench_score = sum(p.adjusted_score for p in bench)

            # ── Attack of the Squires (Soldier L2) ───────────────────────────
            if "attack_of_squires" in team.perks:
                bench_contrib = team.bench_score * 0.5
                old = team.adjusted_team_score
                team.adjusted_team_score += bench_contrib
                team.add_team_effect(f"Attack of the Squires +{bench_contrib:.2f} (½ bench)")
                state.add_trace(
                    rule_id="PERK_ATTACK_OF_SQUIRES",
                    target=team.name,
                    source_value=team.bench_score,
                    prior_value=old,
                    resulting_value=team.adjusted_team_score,
                    related_perk_id="attack_of_squires",
                    reason=f"Attack of the Squires: ½ bench score ({team.bench_score:.2f}) = +{bench_contrib:.2f}",
                    explanation=f"Attack of the Squires (Soldier L2): Half of bench total {team.bench_score:.2f} added → +{bench_contrib:.2f}.",
                    is_provisional=True
                )

            # ── Reserve Forces (Strategist L2) ───────────────────────────────
            if "reserve_forces" in team.perks:
                active_bench = [p for p in bench if p.status not in ("bye", "inactive", "dead")]
                if active_bench:
                    top_bench = max(active_bench, key=lambda x: x.adjusted_score)
                    top_bench.is_protected = True
                    top_bench.protection_source = "Reserve Forces (Strategist L2)"
                    contrib = top_bench.adjusted_score
                    old = team.adjusted_team_score
                    team.adjusted_team_score += contrib
                    team.add_team_effect(f"Reserve Forces +{contrib:.2f} ({top_bench.name})")
                    state.add_trace(
                        rule_id="PERK_RESERVE_FORCES",
                        target=f"{team.name}/{top_bench.name}",
                        source_value=contrib, prior_value=old, resulting_value=team.adjusted_team_score,
                        related_perk_id="reserve_forces",
                        reason=f"Reserve Forces: {top_bench.name} (top bench) contributes {contrib:.2f} and is immune to all attacks",
                        explanation=f"Reserve Forces (Strategist L2): {top_bench.name} contributes {contrib:.2f} to team score and is immune to attacks.",
                        is_provisional=True
                    )

            # ── Salary Bonus (Financier L1) ───────────────────────────────────
            if "salary_bonus" in team.perks:
                bonus_gold = 2 * team.bench_score
                state.gold_transactions.append({
                    "team_id": team.team_id,
                    "transaction_type": "perk_bonus",
                    "amount": bonus_gold,
                    "description": f"Salary Bonus: 2 × bench score {team.bench_score:.2f} = {bonus_gold:.2f} gold",
                    "related_perk_id": "salary_bonus",
                    "is_provisional": True,
                })
                state.add_trace(
                    rule_id="PERK_SALARY_BONUS",
                    target=team.name,
                    source_value=team.bench_score,
                    resulting_value=bonus_gold,
                    related_perk_id="salary_bonus",
                    reason=f"Salary Bonus: 2 × bench score {team.bench_score:.2f} = {bonus_gold:.2f} gold",
                    explanation=f"Salary Bonus (Financier L1): Team earns 2 × bench total = {bonus_gold:.2f} additional gold.",
                    is_provisional=True
                )

    # ── Step 10: Raw Team Totals ──────────────────────────────────────────────

    def _step10_raw_team_totals(self, state: MatchupState):
        for team in [state.home, state.away]:
            starter_total = sum(p.adjusted_score for p in team.starters())
            team.raw_team_score = sum(p.raw_score for p in team.starters())
            # adjusted_team_score may already have bench additions from step 9
            team.adjusted_team_score += starter_total
            state.add_trace(
                rule_id="CALC_RAW_TEAM_TOTAL",
                target=team.name,
                source_value=starter_total,
                prior_value=0.0,
                resulting_value=team.adjusted_team_score,
                reason=f"Starter total: {starter_total:.2f} + bench additions = {team.adjusted_team_score:.2f}",
                explanation=f"Team total after starters: {starter_total:.2f}. With bench effects: {team.adjusted_team_score:.2f}.",
                is_provisional=True
            )

    # ── Step 11: Teamwide Percentage Effects ──────────────────────────────────

    def _step11_teamwide_percentage_effects(self, state: MatchupState):
        for team in [state.home, state.away]:

            # ── Militia (Soldier L2): all active players +20% ─────────────────
            if "militia" in team.perks:
                old = team.adjusted_team_score
                # Militia applies to individual player scores (already in total)
                # Per rules: "all active players' scores are increased by 20%"
                # We apply as a 20% increase to the starters' contribution
                militia_bonus = sum(p.adjusted_score for p in team.starters()) * 0.20
                team.adjusted_team_score = old + militia_bonus
                team.add_team_effect(f"Militia +20% = +{militia_bonus:.2f}")
                state.add_trace(
                    rule_id="PERK_MILITIA",
                    target=team.name,
                    source_value=old,
                    prior_value=old,
                    resulting_value=team.adjusted_team_score,
                    related_perk_id="militia",
                    reason=f"Militia: all active players +20% → +{militia_bonus:.2f}",
                    explanation=f"Militia (Soldier L2): All active players' scores increased by 20%. Bonus: {militia_bonus:.2f}.",
                    is_provisional=True
                )

            # ── Confounding Charm (Druid L1, spell) ──────────────────────────
            # Applied via attack mechanism — handled in player effects

            # ── Team Unity (Druid L2) ─────────────────────────────────────────
            if "team_unity" in team.perks:
                starters = team.starters()
                all_home = all(p.is_home_game for p in starters)
                all_away = all(not p.is_home_game for p in starters)
                if all_home or all_away:
                    old = team.adjusted_team_score
                    raw = team.raw_team_score
                    bonus = raw * 0.40
                    team.adjusted_team_score = old + bonus
                    team.add_team_effect(f"Team Unity +40% raw = +{bonus:.2f}")
                    state.add_trace(
                        rule_id="PERK_TEAM_UNITY",
                        target=team.name,
                        source_value=raw,
                        prior_value=old,
                        resulting_value=team.adjusted_team_score,
                        related_perk_id="team_unity",
                        reason=f"All starters {'home' if all_home else 'away'} — Team Unity +40% of raw {raw:.2f} = +{bonus:.2f}",
                        explanation=f"Team Unity (Druid L2): All active players are {'home' if all_home else 'away'} teams. +40% of raw team score {raw:.2f} = +{bonus:.2f}.",
                        is_provisional=True
                    )
                else:
                    state.add_trace(
                        rule_id="PERK_TEAM_UNITY",
                        target=team.name,
                        did_fire=False,
                        reason="Team Unity: starters are NOT all home or all away — no bonus",
                        explanation="Team Unity (Druid L2): Condition not met. Starters are a mix of home and away games.",
                        is_provisional=True
                    )

    # ── Step 12: Fixed Bonuses ────────────────────────────────────────────────

    def _step12_fixed_team_bonuses(self, state: MatchupState):
        for team in [state.home, state.away]:
            # ── Inspiring Sermon ──────────────────────────────────────────────
            for attack in state.attacks:
                if attack.get("item_id") == "inspiring_sermon" and attack.get("attacker_team_id") == team.team_id:
                    if attack.get("status") in ("validated", "applied"):
                        starters = team.starters()
                        all_sunday = all(
                            p.game_status in ("finished", "active", "scheduled") and
                            not p.is_home_game == False  # simplified: check game day
                            for p in starters
                        )
                        # Provisional: mark as pending — actual Sunday check is data-dependent
                        old = team.adjusted_team_score
                        team.adjusted_team_score += 10
                        attack["status"] = "applied"
                        team.add_team_effect("Inspiring Sermon +10")
                        state.add_trace(
                            rule_id="ITEM_INSPIRING_SERMON",
                            target=team.name,
                            source_value=10,
                            prior_value=old,
                            resulting_value=team.adjusted_team_score,
                            related_item_id="inspiring_sermon",
                            reason="Inspiring Sermon: +10 points (all active players play Sunday — provisional)",
                            explanation="Inspiring Sermon: +10 points added. PROVISIONAL: requires all active players to play on Sunday. This is unverified — commissioner review required.",
                            is_provisional=True
                        )

    # ── Step 13: Race Gold ────────────────────────────────────────────────────

    def _step13_race_gold(self, state: MatchupState):
        for team in [state.home, state.away]:
            gold = self._calculate_race_gold(team, state)
            team.race_gold_earned = gold
            state.gold_transactions.append({
                "team_id": team.team_id,
                "transaction_type": "race_income",
                "amount": gold,
                "description": f"Race gold ({team.race} strength position formula)",
                "is_provisional": True,
            })
            state.add_trace(
                rule_id="GOLD_RACE_INCOME",
                target=team.name,
                source_value=gold,
                resulting_value=gold,
                reason=f"{team.race} race gold formula: {gold:.2f}",
                explanation=f"Race Gold: {team.race} uses {self._race_strength_position(team.race)} formula. Earned: {gold:.2f} gold.",
                is_provisional=True
            )

    def _calculate_race_gold(self, team: TeamState, state: MatchupState) -> float:
        race = team.race
        starters = team.starters()

        if race == "Human":
            qbs = [p for p in starters if p.position == "QB"]
            if not qbs:
                return 0.0
            qb_score = qbs[0].raw_score
            return 5 * qb_score

        elif race == "Elf":
            wrs = sorted([p for p in starters if p.position == "WR"], key=lambda x: x.raw_score, reverse=True)
            top2 = wrs[:2]
            return 2 * sum(p.raw_score for p in top2) + 10

        elif race == "Dwarf":
            idps = sorted([p for p in starters if p.position in ("DL", "LB", "DB")], key=lambda x: x.raw_score, reverse=True)
            top2 = idps[:2]
            return 3 * sum(p.raw_score for p in top2) + 10

        elif race == "Halfling":
            rbs = sorted([p for p in starters if p.position == "RB"], key=lambda x: x.raw_score, reverse=True)
            if not rbs:
                return 0.0
            return 4 * rbs[0].raw_score

        elif race == "Fairy":
            ks = [p for p in starters if p.position == "K"]
            if not ks:
                return 50.0
            k_score = ks[0].raw_score
            return 5 * k_score + 50

        elif race == "Troll":
            tes = sorted([p for p in starters if p.position == "TE"], key=lambda x: x.raw_score, reverse=True)
            if not tes:
                return 20.0
            return 5 * tes[0].raw_score + 20

        return 0.0

    def _race_strength_position(self, race: str) -> str:
        return {
            "Human": "QB", "Elf": "WR", "Dwarf": "IDP",
            "Halfling": "RB", "Fairy": "K", "Troll": "TE"
        }.get(race, "QB")

    def _matches_strength_position(self, position: str, strength: str) -> bool:
        if strength == "IDP":
            return position in ("DL", "LB", "DB")
        return position == strength

    # ── Step 14: Gold Transactions ────────────────────────────────────────────

    def _step14_gold_transactions(self, state: MatchupState):
        """Process purchases already validated. Deduct gold, check balances."""
        for team in [state.home, state.away]:
            for attack in state.attacks:
                if attack.get("attacker_team_id") == team.team_id:
                    cost = attack.get("cost_gold", 0)
                    if cost > 0 and attack.get("status") not in ("invalid",):
                        if team.gold < cost:
                            attack["status"] = "invalid"
                            attack["invalidation_reason"] = f"Insufficient gold: needed {cost}, had {team.gold:.2f}"
                            state.add_trace(
                                rule_id="GOLD_INSUFFICIENT",
                                target=team.name,
                                source_value=cost,
                                prior_value=team.gold,
                                resulting_value=team.gold,
                                was_invalid=True, did_fire=False,
                                related_item_id=attack.get("item_id"),
                                reason=f"Insufficient gold: needed {cost}, had {team.gold:.2f}",
                                explanation=f"Purchase REJECTED: {team.name} needed {cost} gold for {attack.get('item_id')} but only had {team.gold:.2f}.",
                                is_provisional=False
                            )
                        else:
                            team.gold -= cost
                            state.gold_transactions.append({
                                "team_id": team.team_id,
                                "transaction_type": "purchase",
                                "amount": -cost,
                                "description": f"Purchased {attack.get('item_id')} for {cost} gold",
                                "related_item_id": attack.get("item_id"),
                                "is_provisional": True,
                            })
                            state.add_trace(
                                rule_id="GOLD_PURCHASE",
                                target=team.name,
                                source_value=cost,
                                prior_value=team.gold + cost,
                                resulting_value=team.gold,
                                related_item_id=attack.get("item_id"),
                                reason=f"Purchased {attack.get('item_id')}: -{cost} gold",
                                explanation=f"Gold deducted: {team.name} spent {cost} gold on {attack.get('item_id')}. Balance: {team.gold:.2f}.",
                                is_provisional=True
                            )

        # Apply race gold and other pending transactions
        for txn in state.gold_transactions:
            team = state.home if txn["team_id"] == state.home.team_id else state.away
            if txn["transaction_type"] == "race_income":
                team.gold += txn["amount"]

    # ── Step 15: Auto Win/Loss ────────────────────────────────────────────────

    def _step15_auto_win_loss(self, state: MatchupState):
        """Check Out of Collateral and other automatic win conditions."""
        for team in [state.home, state.away]:
            if "out_of_collateral" in team.perks:
                qb = next((p for p in team.starters() if p.position == "QB"), None)
                if qb:
                    # We'd need passing yards — approximate from raw score
                    # PROVISIONAL: requires passing yards data
                    state.ambiguities.append({
                        "rule_id": "PERK_OUT_OF_COLLATERAL",
                        "title": "Out of Collateral: requires passing yards data",
                        "description": "Out of Collateral requires QB passing yards. Current provider only has total raw score.",
                        "provisional_interpretation": "If passing yards not available, perk is held in abeyance pending commissioner ruling.",
                        "status": "open"
                    })
                    state.add_trace(
                        rule_id="PERK_OUT_OF_COLLATERAL",
                        target=f"{team.name}/QB",
                        did_fire=False, is_provisional=True,
                        reason="Requires QB passing yards data not available in current provider",
                        explanation="PROVISIONAL AMBIGUITY: Out of Collateral requires passing yards. If QB has < 175 yards: auto loss. ≥ 350 yards: auto win. Otherwise: +65 pts. Held pending data availability.",
                    )

    # ── Step 16: Post-Matchup Deaths ──────────────────────────────────────────

    def _step16_post_matchup_deaths(self, state: MatchupState):
        """Process deaths from Martyr, Soul Reaper, etc."""
        for attack in state.attacks:
            if attack.get("causes_death"):
                pname = attack["causes_death"]
                for team in [state.home, state.away]:
                    p = next((x for x in team.players if x.name == pname), None)
                    if p:
                        p.is_dead = True
                        p.death_source = attack.get("item_id", "unknown")
                        state.deaths.append({
                            "player": pname,
                            "team": team.name,
                            "cause": attack.get("item_id"),
                            "week": state.week,
                            "persistent": True,
                            "description": f"{pname} died via {attack.get('item_id')} — must be dropped immediately per rulebook"
                        })
                        state.add_trace(
                            rule_id="DEATH_POST_WEEK",
                            target=f"{team.name}/{pname}",
                            did_fire=True,
                            reason=f"{pname} died via {attack.get('item_id')}",
                            explanation=f"POST-WEEK DEATH: {pname} was killed by {attack.get('item_id')}. Must be dropped immediately. Cannot be picked up unless a specific item/perk allows it.",
                            is_provisional=False
                        )

        # Soul Reaper: all opponent players with raw ≤ 5 are killed
        for team in [state.home, state.away]:
            if "soul_reaper" in team.perks:
                opp = state.away if team is state.home else state.home
                for p in opp.starters():
                    if p.raw_score <= 5.0 and not p.is_dead:
                        p.is_dead = True
                        p.death_source = "Soul Reaper"
                        state.deaths.append({
                            "player": p.name,
                            "team": opp.name,
                            "cause": "Soul Reaper",
                            "week": state.week,
                            "persistent": True,
                            "description": f"{p.name} raw score ≤ 5 — killed by Soul Reaper at week's end"
                        })
                        state.add_trace(
                            rule_id="PERK_SOUL_REAPER",
                            target=f"{opp.name}/{p.name}",
                            source_value=p.raw_score,
                            resulting_value=0.0,
                            did_fire=True,
                            related_perk_id="soul_reaper",
                            reason=f"Soul Reaper: {p.name} raw score {p.raw_score:.2f} ≤ 5",
                            explanation=f"Soul Reaper (Necromancer L1): {p.name} had raw score {p.raw_score:.2f} ≤ 5. Dies at week's end.",
                            is_provisional=False
                        )

    # ── Step 17: Manual Rulings ───────────────────────────────────────────────

    def _step17_manual_rulings(self, state: MatchupState):
        for ruling in state.rulings:
            if not ruling.get("is_applied", False):
                continue
            team = state.home if ruling.get("team_id") == state.home.team_id else state.away
            adj = ruling.get("score_adjustment", 0.0)
            gold_adj = ruling.get("gold_adjustment", 0.0)

            if adj != 0:
                old = team.adjusted_team_score
                team.adjusted_team_score += adj
                team.add_team_effect(f"Commissioner Ruling: {'+' if adj >= 0 else ''}{adj:.2f}")
                state.add_trace(
                    rule_id="RULING_SCORE_ADJ",
                    target=team.name,
                    source_value=adj,
                    prior_value=old,
                    resulting_value=team.adjusted_team_score,
                    related_ruling_id=ruling.get("id"),
                    reason=f"Commissioner ruling: {ruling.get('description', 'Manual adjustment')}",
                    explanation=f"Commissioner Ruling: {ruling.get('description')}. Score adjusted by {adj:+.2f}.",
                    is_provisional=False
                )

            if gold_adj != 0:
                team.gold += gold_adj
                state.gold_transactions.append({
                    "team_id": team.team_id,
                    "transaction_type": "adjustment",
                    "amount": gold_adj,
                    "description": f"Commissioner ruling: {ruling.get('description')}",
                    "is_provisional": False,
                })

    # ── Step 18: Rounding ─────────────────────────────────────────────────────

    def _step18_round_scores(self, state: MatchupState):
        """Round to 2 decimal places. Gambler's Luck rounds up to nearest 10."""
        for team in [state.home, state.away]:
            # Check for Gambler's Luck item usage
            gamblers_luck = any(
                a.get("item_id") == "gamblers_luck" and
                a.get("attacker_team_id") == team.team_id and
                a.get("status") in ("validated", "applied")
                for a in state.attacks
            )
            old = team.adjusted_team_score
            if gamblers_luck:
                team.adjusted_team_score = math.ceil(old / 10) * 10
                state.add_trace(
                    rule_id="ITEM_GAMBLERS_LUCK",
                    target=team.name,
                    source_value=old,
                    prior_value=old,
                    resulting_value=team.adjusted_team_score,
                    related_item_id="gamblers_luck",
                    reason=f"Gambler's Luck: round up to nearest 10 → {team.adjusted_team_score}",
                    explanation=f"Gambler's Luck: Score {old:.2f} rounded up to nearest 10 = {team.adjusted_team_score}.",
                    is_provisional=True
                )
            else:
                team.adjusted_team_score = round(team.adjusted_team_score, 2)

    # ── Step 19: Provisional Winner ───────────────────────────────────────────

    def _step19_provisional_winner(self, state: MatchupState):
        winner = state.provisional_winner()
        loser_name = state.away.name if winner == state.home.name else state.home.name
        state.add_trace(
            rule_id="CALC_PROVISIONAL_WINNER",
            target="matchup",
            did_fire=True,
            reason=f"Provisional winner: {winner or 'TIE'} ({state.home.adjusted_team_score:.2f} vs {state.away.adjusted_team_score:.2f})",
            explanation=f"PROVISIONAL RESULT: {state.home.name} {state.home.adjusted_team_score:.2f} vs {state.away.name} {state.away.adjusted_team_score:.2f}. {'Winner: ' + winner if winner else 'TIE.'}",
            is_provisional=True
        )
