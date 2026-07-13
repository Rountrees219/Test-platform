"""
SQLAlchemy models for Footbolzano Live Scoring.
"""
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, JSON, ForeignKey, Text, Enum as SAEnum
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import enum

from app.core.database import Base


def utcnow():
    return datetime.now(timezone.utc)


# ── Enumerations ──────────────────────────────────────────────────────────────

class Race(str, enum.Enum):
    HUMAN = "Human"
    DWARF = "Dwarf"
    FAIRY = "Fairy"
    HALFLING = "Halfling"
    ELF = "Elf"
    TROLL = "Troll"


class PositionSlot(str, enum.Enum):
    QB = "QB"
    RB1 = "RB1"
    RB2 = "RB2"
    WR1 = "WR1"
    WR2 = "WR2"
    WR3 = "WR3"
    TE = "TE"
    FLEX = "FLEX"
    K = "K"
    IDP1 = "IDP1"
    IDP2 = "IDP2"
    BN1 = "BN1"
    BN2 = "BN2"
    BN3 = "BN3"
    BN4 = "BN4"
    BN5 = "BN5"


class MatchupStatus(str, enum.Enum):
    UPCOMING = "upcoming"
    LIVE = "live"
    PROVISIONAL = "provisional"
    FINALIZED = "finalized"
    REOPENED = "reopened"


class PlayerStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    INJURED = "injured"
    BYE = "bye"
    FINISHED = "finished"
    DEAD = "dead"


class AttackStatus(str, enum.Enum):
    PENDING = "pending"
    VALIDATED = "validated"
    BLOCKED = "blocked"
    INVALID = "invalid"
    APPLIED = "applied"


class EventType(str, enum.Enum):
    SCORE_UPDATE = "score_update"
    ATTACK_DECLARED = "attack_declared"
    ATTACK_VALIDATED = "attack_validated"
    ATTACK_BLOCKED = "attack_blocked"
    ATTACK_INVALID = "attack_invalid"
    ATTACK_APPLIED = "attack_applied"
    PROTECTION_TRIGGERED = "protection_triggered"
    PERK_TRIGGERED = "perk_triggered"
    GOLD_EARNED = "gold_earned"
    GOLD_SPENT = "gold_spent"
    GOLD_TRANSFER = "gold_transfer"
    ITEM_PURCHASED = "item_purchased"
    PLAYER_DIED = "player_died"
    COMMISSIONER_RULING = "commissioner_ruling"
    SCORE_CORRECTION = "score_correction"
    MATCHUP_FINALIZED = "matchup_finalized"
    RECALCULATION = "recalculation"
    SIMULATION_EVENT = "simulation_event"
    MANUAL_SCORE_ENTRY = "manual_score_entry"
    AMBIGUITY_FLAGGED = "ambiguity_flagged"
    AMBIGUITY_RESOLVED = "ambiguity_resolved"


class RuleApprovalStatus(str, enum.Enum):
    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    PENDING_COMMISSIONER = "pending_commissioner"
    REJECTED = "rejected"


# ── Core Tables ───────────────────────────────────────────────────────────────

class Season(Base):
    __tablename__ = "seasons"
    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    name = Column(String(100))
    rulebook_version = Column(String(20), default="v1")
    effective_start = Column(String(30))
    effective_end = Column(String(30))
    source_url = Column(String(500))
    access_timestamp = Column(String(50))
    created_at = Column(DateTime, default=utcnow)

    teams = relationship("Team", back_populates="season")
    matchups = relationship("Matchup", back_populates="season")
    weeks = relationship("Week", back_populates="season")


class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey("seasons.id"))
    name = Column(String(100), nullable=False)
    owner_name = Column(String(100), nullable=False)
    race = Column(SAEnum(Race), nullable=False)
    gold = Column(Float, default=500.0)
    is_demo = Column(Boolean, default=True)
    crest_emoji = Column(String(10), default="⚔️")
    division = Column(String(50))
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    total_points = Column(Float, default=0.0)

    season = relationship("Season", back_populates="teams")
    players = relationship("PlayerRoster", back_populates="team")
    perks = relationship("TeamPerk", back_populates="team")
    inventory = relationship("Inventory", back_populates="team")
    gold_transactions = relationship("GoldTransaction", back_populates="team")
    home_matchups = relationship("Matchup", foreign_keys="Matchup.home_team_id", back_populates="home_team")
    away_matchups = relationship("Matchup", foreign_keys="Matchup.away_team_id", back_populates="away_team")


class Week(Base):
    __tablename__ = "weeks"
    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey("seasons.id"))
    week_number = Column(Integer, nullable=False)
    start_date = Column(String(30))
    end_date = Column(String(30))
    attack_deadline = Column(String(50))
    roster_freeze_start = Column(String(50))
    roster_freeze_end = Column(String(50))
    is_active = Column(Boolean, default=False)

    season = relationship("Season", back_populates="weeks")
    matchups = relationship("Matchup", back_populates="week")


class Matchup(Base):
    __tablename__ = "matchups"
    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey("seasons.id"))
    week_id = Column(Integer, ForeignKey("weeks.id"))
    home_team_id = Column(Integer, ForeignKey("teams.id"))
    away_team_id = Column(Integer, ForeignKey("teams.id"))
    status = Column(SAEnum(MatchupStatus), default=MatchupStatus.UPCOMING)
    home_raw_score = Column(Float, default=0.0)
    away_raw_score = Column(Float, default=0.0)
    home_adjusted_score = Column(Float, default=0.0)
    away_adjusted_score = Column(Float, default=0.0)
    provisional_winner_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    final_winner_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    simulation_seed = Column(Integer, default=42)
    simulation_state = Column(String(20), default="stopped")  # stopped/running/paused/finished
    simulation_speed = Column(Float, default=1.0)
    simulation_event_index = Column(Integer, default=0)
    last_updated = Column(DateTime, default=utcnow, onupdate=utcnow)
    finalized_at = Column(DateTime, nullable=True)
    is_demo = Column(Boolean, default=True)

    season = relationship("Season", back_populates="matchups")
    week = relationship("Week", back_populates="matchups")
    home_team = relationship("Team", foreign_keys=[home_team_id], back_populates="home_matchups")
    away_team = relationship("Team", foreign_keys=[away_team_id], back_populates="away_matchups")
    events = relationship("Event", back_populates="matchup", order_by="Event.sequence_number")
    audit_entries = relationship("AuditEntry", back_populates="matchup", order_by="AuditEntry.sequence_number")
    ambiguities = relationship("Ambiguity", back_populates="matchup")


class NFLPlayer(Base):
    __tablename__ = "nfl_players"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    nfl_team = Column(String(50))
    position = Column(String(10))
    is_rookie = Column(Boolean, default=False)
    is_demo = Column(Boolean, default=True)


class PlayerRoster(Base):
    __tablename__ = "player_rosters"
    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"))
    matchup_id = Column(Integer, ForeignKey("matchups.id"))
    nfl_player_id = Column(Integer, ForeignKey("nfl_players.id"))
    slot = Column(SAEnum(PositionSlot), nullable=False)
    is_starter = Column(Boolean, default=True)
    status = Column(SAEnum(PlayerStatus), default=PlayerStatus.ACTIVE)
    raw_score = Column(Float, default=0.0)
    adjusted_score = Column(Float, default=0.0)
    projected_score = Column(Float, default=0.0)
    is_locked = Column(Boolean, default=False)
    is_protected = Column(Boolean, default=False)
    protection_source = Column(String(100))
    is_dead = Column(Boolean, default=False)
    death_source = Column(String(100))
    applied_effects = Column(JSON, default=list)
    game_status = Column(String(50), default="scheduled")
    opponent_nfl_team = Column(String(50))
    is_home_game = Column(Boolean, default=True)
    is_night_game = Column(Boolean, default=False)
    scoring_source = Column(String(50), default="mock")
    previous_week_score = Column(Float, default=0.0)
    previous_week_ones_digit = Column(Integer, default=0)

    team = relationship("Team", back_populates="players")
    nfl_player = relationship("NFLPlayer")


# ── Rules ─────────────────────────────────────────────────────────────────────

class Rule(Base):
    __tablename__ = "rules"
    id = Column(Integer, primary_key=True)
    rule_id = Column(String(50), unique=True, nullable=False)
    rule_name = Column(String(100), nullable=False)
    version = Column(String(20), default="v1")
    source_citation = Column(String(500))
    effective_start = Column(String(30))
    effective_end = Column(String(30))
    category = Column(String(50))  # race_gold, attack, protection, multiplier, bench, teamwide, death, manual
    trigger_timing = Column(Integer)  # execution order step
    target_type = Column(String(50))  # player, team, gold
    eligibility_conditions = Column(JSON)
    calculation = Column(Text)
    priority = Column(Integer, default=100)
    stacking_behavior = Column(String(50), default="sequential")
    exceptions = Column(JSON, default=list)
    invalidation_conditions = Column(JSON, default=list)
    outputs = Column(JSON, default=list)
    explanation_template = Column(Text)
    approval_status = Column(SAEnum(RuleApprovalStatus), default=RuleApprovalStatus.CONFIRMED)
    commissioner_note = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


# ── Perks & Inventory ─────────────────────────────────────────────────────────

class Perk(Base):
    __tablename__ = "perks"
    id = Column(Integer, primary_key=True)
    perk_id = Column(String(50), unique=True)
    name = Column(String(100), nullable=False)
    perk_class = Column(String(50))  # Soldier, Necromancer, Financier, etc.
    level = Column(Integer)  # 1, 2, 3
    description = Column(Text)
    rule_id = Column(String(50))
    cost_gold = Column(Float, default=0)
    is_spell = Column(Boolean, default=False)


class TeamPerk(Base):
    __tablename__ = "team_perks"
    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"))
    perk_id = Column(String(50), ForeignKey("perks.perk_id"))
    week_acquired = Column(Integer)
    is_active_this_week = Column(Boolean, default=True)
    activation_class = Column(String(50))  # which class slot this is playing under this week
    extra_data = Column(JSON, default=dict)  # e.g., chosen animal for Animal Husbandry

    team = relationship("Team", back_populates="perks")
    perk = relationship("Perk", foreign_keys=[perk_id])


class StoreItem(Base):
    __tablename__ = "store_items"
    id = Column(Integer, primary_key=True)
    item_id = Column(String(50), unique=True)
    name = Column(String(100), nullable=False)
    store = Column(String(50))  # weapon_store, armory, general_store, boutique, apothecary, freemium
    cost_gold = Column(Float)
    cost_usd = Column(Float, default=0)
    race_restriction = Column(String(50))  # null = any race
    requires_item = Column(String(50))  # prerequisite item_id
    requires_perk = Column(String(50))  # prerequisite perk_id
    description = Column(Text)
    is_weapon = Column(Boolean, default=False)
    is_wooden = Column(Boolean, default=False)
    effect_type = Column(String(50))
    effect_params = Column(JSON, default=dict)


class Inventory(Base):
    __tablename__ = "inventory"
    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"))
    item_id = Column(String(50), ForeignKey("store_items.item_id"))
    quantity = Column(Integer, default=1)
    week_purchased = Column(Integer)
    is_used = Column(Boolean, default=False)
    week_used = Column(Integer, nullable=True)

    team = relationship("Team", back_populates="inventory")
    item = relationship("StoreItem", foreign_keys=[item_id])


# ── Attacks ───────────────────────────────────────────────────────────────────

class Attack(Base):
    __tablename__ = "attacks"
    id = Column(Integer, primary_key=True)
    matchup_id = Column(Integer, ForeignKey("matchups.id"))
    attacker_team_id = Column(Integer, ForeignKey("teams.id"))
    target_team_id = Column(Integer, ForeignKey("teams.id"))
    item_id = Column(String(50))
    perk_id = Column(String(50), nullable=True)
    target_player_ids = Column(JSON, default=list)  # PlayerRoster ids
    target_position_slots = Column(JSON, default=list)
    submitted_at = Column(DateTime, default=utcnow)
    cost_gold = Column(Float, default=0)
    status = Column(SAEnum(AttackStatus), default=AttackStatus.PENDING)
    validation_result = Column(JSON, default=dict)
    score_effect = Column(JSON, default=dict)
    blocked_by = Column(String(100))
    invalidation_reason = Column(Text)
    extra_data = Column(JSON, default=dict)


# ── Events & Audit ────────────────────────────────────────────────────────────

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    sequence_number = Column(Integer)
    matchup_id = Column(Integer, ForeignKey("matchups.id"))
    event_type = Column(SAEnum(EventType))
    timestamp = Column(DateTime, default=utcnow)
    week_number = Column(Integer)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    player_roster_id = Column(Integer, ForeignKey("player_rosters.id"), nullable=True)
    description = Column(Text)
    data = Column(JSON, default=dict)
    is_simulated = Column(Boolean, default=False)
    is_confirmed = Column(Boolean, default=False)

    matchup = relationship("Matchup", back_populates="events")


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    id = Column(Integer, primary_key=True)
    sequence_number = Column(Integer)
    timestamp = Column(DateTime, default=utcnow)
    week_number = Column(Integer)
    matchup_id = Column(Integer, ForeignKey("matchups.id"))
    rule_id = Column(String(50))
    rule_version = Column(String(20))
    source_value = Column(Float)
    target_description = Column(String(200))
    prior_value = Column(Float)
    resulting_value = Column(Float)
    did_fire = Column(Boolean, default=True)
    was_blocked = Column(Boolean, default=False)
    was_invalid = Column(Boolean, default=False)
    reason = Column(Text)
    related_attack_id = Column(Integer, nullable=True)
    related_item_id = Column(String(50), nullable=True)
    related_perk_id = Column(String(50), nullable=True)
    related_ruling_id = Column(Integer, nullable=True)
    is_provisional = Column(Boolean, default=True)
    explanation = Column(Text)

    matchup = relationship("Matchup", back_populates="audit_entries")


class GoldTransaction(Base):
    __tablename__ = "gold_transactions"
    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id"))
    matchup_id = Column(Integer, ForeignKey("matchups.id"), nullable=True)
    week_number = Column(Integer)
    transaction_type = Column(String(50))  # opening, race_income, quest, purchase, perk, interest, transfer, fine, adjustment
    amount = Column(Float)  # positive = gain, negative = spend
    description = Column(Text)
    balance_after = Column(Float)
    is_provisional = Column(Boolean, default=True)
    timestamp = Column(DateTime, default=utcnow)
    related_item_id = Column(String(50), nullable=True)

    team = relationship("Team", back_populates="gold_transactions")


class Ambiguity(Base):
    __tablename__ = "ambiguities"
    id = Column(Integer, primary_key=True)
    matchup_id = Column(Integer, ForeignKey("matchups.id"), nullable=True)
    rule_id = Column(String(50), nullable=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    provisional_interpretation = Column(Text)
    source_citation = Column(String(500))
    status = Column(String(30), default="open")  # open, approved, rejected
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    commissioner_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    matchup = relationship("Matchup", back_populates="ambiguities")


class CommissionerRuling(Base):
    __tablename__ = "commissioner_rulings"
    id = Column(Integer, primary_key=True)
    matchup_id = Column(Integer, ForeignKey("matchups.id"), nullable=True)
    week_number = Column(Integer)
    ruling_type = Column(String(50))  # score_adjustment, perk_ruling, attack_ruling, other
    description = Column(Text)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    player_roster_id = Column(Integer, ForeignKey("player_rosters.id"), nullable=True)
    score_adjustment = Column(Float, default=0.0)
    gold_adjustment = Column(Float, default=0.0)
    issued_by = Column(String(100), default="Commissioner")
    issued_at = Column(DateTime, default=utcnow)
    is_applied = Column(Boolean, default=False)
    applied_at = Column(DateTime, nullable=True)
    ambiguity_id = Column(Integer, ForeignKey("ambiguities.id"), nullable=True)


class SimulationEvent(Base):
    __tablename__ = "simulation_events"
    id = Column(Integer, primary_key=True)
    matchup_id = Column(Integer, ForeignKey("matchups.id"))
    event_index = Column(Integer)  # order in the simulation
    delay_seconds = Column(Float, default=5.0)  # time after previous event
    player_roster_id = Column(Integer, ForeignKey("player_rosters.id"))
    score_delta = Column(Float)  # how much raw score changes
    new_raw_score = Column(Float)
    event_description = Column(String(200))
    game_status = Column(String(50))
    seed = Column(Integer, default=42)
    was_executed = Column(Boolean, default=False)
    executed_at = Column(DateTime, nullable=True)
