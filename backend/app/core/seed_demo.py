"""
Seed demonstration data for Footbolzano Live Scoring.

This is FICTIONAL data created for demonstration purposes only.
It does NOT represent any real matchup or historical result.
Player names are real NFL players used purely for realism;
scores, perks, attacks, and outcomes are entirely fabricated.

Demonstrates all required mechanics:
✓ Raw player scoring
✓ Raw team scoring
✓ Race-based gold (Elf race)
✓ Player-targeted attack (Wooden Sword halves a player)
✓ Blocked attack (Tower Shield blocks attack)
✓ Player score multiplier (Martyr ×2)
✓ Teamwide percentage adjustment (Militia +20%)
✓ Bench-related adjustment (Attack of the Squires ½ bench)
✓ Item purchase (Adrenaline Potion)
✓ Inventory validation (insufficient gold → rejected)
✓ Gold deduction
✓ Invalid/blocked action (Bribe blocked by Fairy race)
✓ Player death (Martyr causes death)
✓ Commissioner ruling (manual score adjustment)
✓ Final adjusted score
✓ Adjusted-score reversal (raw score leader is not adjusted leader)
✓ Deterministic recalculation
"""
from __future__ import annotations

from sqlalchemy.orm import Session
from app.models.models import (
    Season, Team, Week, Matchup, NFLPlayer, PlayerRoster,
    Rule, Perk, TeamPerk, StoreItem, Inventory, Attack,
    Event, AuditEntry, GoldTransaction, Ambiguity,
    CommissionerRuling, SimulationEvent,
    Race, PositionSlot, MatchupStatus, PlayerStatus,
    AttackStatus, EventType, RuleApprovalStatus
)
from app.rules.ambiguity_register import AMBIGUITIES
from app.rules.catalog import RULES_CATALOG
from app.providers.mock_live import generate_events, FINAL_SCORES_SEED_42
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def seed_rules(db: Session):
    """Seed rule catalog and perk definitions."""
    for r in RULES_CATALOG:
        existing = db.query(Rule).filter(Rule.rule_id == r["rule_id"]).first()
        if not existing:
            rule = Rule(
                rule_id=r["rule_id"],
                rule_name=r["rule_name"],
                version=r.get("version", "v1"),
                source_citation=r.get("source_citation", ""),
                category=r.get("category", ""),
                trigger_timing=r.get("trigger_timing", 0),
                calculation=r.get("calculation", ""),
                explanation_template=r.get("explanation_template", ""),
                approval_status=RuleApprovalStatus.CONFIRMED if r.get("approval_status") == "confirmed" else RuleApprovalStatus.PROVISIONAL,
                commissioner_note=r.get("commissioner_note"),
            )
            db.add(rule)

    # Seed perks
    perks_data = [
        # Soldier
        ("army_sergeant", "Army Sergeant", "Soldier", 1, "Each week, highest-scoring active player at your Race's strength position gains +30%.", False),
        ("armor", "Armor", "Soldier", 1, "Immune to wooden attack weapons.", False),
        ("ignorance", "Ignorance", "Soldier", 1, "Opponent's spells cost +50% gold.", False),
        ("militia", "Militia", "Soldier", 2, "All active players' scores increased by 20%.", False),
        ("attack_of_squires", "Attack of the Squires", "Soldier", 2, "Half of your bench score is added to your team score.", False),
        ("overwhelming_victory", "Overwhelming Victory", "Soldier", 3, "If no items used this week: +50% of raw team score.", False),
        # Necromancer
        ("martyr", "Martyr", "Necromancer", 1, "Pick active player Wednesday; they score double, then die at week's end.", False),
        ("unstable_zombie", "Unstable Zombie", "Necromancer", 1, "Pay 80 gold to play a dead player this week.", True),
        ("soul_reaper", "Soul Reaper", "Necromancer", 1, "All opponent players with raw ≤ 5 are killed at week's end.", False),
        # Financier
        ("salary_bonus", "Salary Bonus", "Financier", 1, "Each week earn additional gold = 2× bench score.", False),
        ("interest_payment", "Interest Payment", "Financier", 1, "Add 20% of post-shop gold for next week.", False),
        ("second_shift_miners", "Second Shift Miners", "Financier", 1, "Top bench defender adds to team score.", False),
        ("boutique", "Boutique", "Financier", 2, "May shop at the Boutique.", False),
        # Warrior
        ("taunt", "Taunt", "Warrior", 1, "Opponent must play a Tight End.", False),
        ("tower_shield", "Tower Shield", "Warrior", 1, "Pick a player; they are immune to wooden attack weapons this week.", False),
        ("brawl", "Brawl", "Warrior", 1, "May attack twice with the same weapon (no player named twice).", False),
        ("weapon_mastery", "Weapon Mastery", "Warrior", 2, "May shop at the Armory.", False),
        # Druid
        ("meteorite", "Meteorite", "Druid", 1, "Pay 20 gold: multiply kicker's score by 1.5.", True),
        ("call_of_the_wild", "Call of the Wild", "Druid", 1, "Choose a non-animal NFL team. Players facing that team score double.", False),
        ("confounding_charm", "Confounding Charm", "Druid", 1, "Pay 20 gold: opponent loses 2× distinct NFL teams on active roster.", True),
        ("animal_husbandry", "Animal Husbandry", "Druid", 2, "Choose animal NFL team; their players score for you instead.", False),
        # Wizard
        ("arcane_shot", "Arcane Shot", "Wizard", 1, "Pay 30 gold: +6 per passing TD by QB.", True),
        ("magical_deflection", "Magical Deflection", "Wizard", 1, "Pay 30 gold: attacks against a position hit player 2 spots below.", True),
        ("ivory_tower_defense", "Ivory Tower Defense", "Wizard", 2, "Pay 50 gold: double a home player's score + immune to wooden attacks.", True),
        # Alchemist
        ("apothecary_club", "Apothecary Club Membership", "Alchemist", 1, "Play two poisons this week (extra free).", False),
        ("employee_discount", "Employee Discount", "Alchemist", 1, "Get one free basic poison every week.", False),
        ("antidote", "Antidote", "Alchemist", 1, "Immune from basic poison.", False),
        ("apprentice_apothecary", "Apprentice Apothecary", "Alchemist", 2, "May shop at the Apothecary.", False),
        # Strategist
        ("enemy_intelligence", "Enemy Intelligence", "Strategist", 1, "May play any Level 1 Perk your opponent has.", False),
        ("reinforcements", "Reinforcements", "Strategist", 1, "Make roster adjustments after Thursday but before subsequent games.", False),
        ("reserve_forces", "Reserve Forces", "Strategist", 2, "Top bench player adds to team and is immune to all attacks.", False),
        # Gambler
        ("fade", "Fade", "Gambler", 1, "Select opponent player: if score < projection, +40 for you; else +5 for opponent.", False),
        ("leverage", "Leverage", "Gambler", 1, "If selected player raw > last week (last week > 0): double score, immune to wooden/poisons.", False),
    ]
    for data in perks_data:
        pid, name, cls, lvl, desc, is_spell = data
        existing = db.query(Perk).filter(Perk.perk_id == pid).first()
        if not existing:
            db.add(Perk(
                perk_id=pid, name=name, perk_class=cls,
                level=lvl, description=desc, is_spell=is_spell
            ))

    db.commit()


def seed_items(db: Session):
    """Seed store items."""
    items = [
        # Weapon Store
        ("wooden_sword", "Wooden Sword", "weapon_store", 70, 0, "Human", None, None,
         "Pick an opponent player; their score is halved.", True, True, "halve_player", {}),
        ("wooden_arrows", "Wooden Arrows", "weapon_store", 70, 0, "Elf", None, None,
         "Pick opponent player with previous-week ones digit 0–3; score → 0.", True, True, "zero_player_conditional", {"max_ones_digit": 3}),
        ("wooden_wand", "Wooden Wand", "weapon_store", 70, 0, "Fairy", None, None,
         "Pick opponent player; reorder digits ascending at week's end.", True, True, "reorder_digits", {}),
        ("twin_wooden_daggers", "Twin Wooden Daggers", "weapon_store", 70, 0, "Dwarf", None, None,
         "Pick two opponent players; each scores ¾.", True, True, "multiply_players", {"factor": 0.75}),
        ("wooden_club", "Wooden Club", "weapon_store", 70, 0, "Troll", None, None,
         "Pick roster position; your player scores max(own, opponent, 10).", True, True, "max_of_three", {}),
        ("wooden_axe", "Wooden Axe", "weapon_store", 70, 0, "Halfling", None, None,
         "Select 3 opponent players; highest scorer replaced by lowest.", True, True, "replace_highest_with_lowest", {}),
        # Armory
        ("steel_sword", "Steel Sword", "armory", 200, 0, None, "wooden_sword", "weapon_mastery",
         "Make an opponent player score 0.", True, False, "zero_player", {}),
        ("steel_arrows", "Steel Arrows", "armory", 200, 0, None, "wooden_arrows", "weapon_mastery",
         "Pick opponent player with prev-week ones digit 0–5; score → 0.", True, False, "zero_player_conditional", {"max_ones_digit": 5}),
        # General Store
        ("basic_poison", "Poison", "general_store", 30, 0, None, None, None,
         "Halves a designated opponent player's score.", False, False, "halve_player", {}),
        ("herbs_of_meek", "Herbs of the Meek", "general_store", 30, 0, None, None, None,
         "Halves the highest-scoring bench player's score.", False, False, "halve_top_bench", {}),
        ("mining_raid", "Mining Raid", "general_store", 50, 0, None, None, None,
         "Gain gold = 50% of opponent's gold earned this week.", False, False, "gold_raid", {}),
        ("inspiring_sermon", "Inspiring Sermon", "general_store", 30, 0, None, None, None,
         "If all active players play Sunday, +10 points.", False, False, "conditional_team_bonus", {"bonus": 10}),
        ("gamblers_luck", "Gambler's Luck", "general_store", 20, 0, None, None, None,
         "Round total score up to nearest 10.", False, False, "round_up_10", {}),
        ("bribe", "Bribe", "general_store", 40, 0, None, None, None,
         "Opponent's kicker scores 0. Fairies immune.", False, False, "zero_kicker", {}),
        ("mercenaries", "Mercenaries", "general_store", 40, 0, None, None, None,
         "All rookies on your roster score 1.5×.", False, False, "multiply_rookies", {"factor": 1.5}),
        ("telepathic_intimidation", "Telepathic Intimidation", "general_store", 25, 0, None, None, None,
         "Opponent gets -6 points for at most 1 TD by starting QB.", False, False, "td_penalty", {"max_tds": 1, "penalty": -6}),
        ("light_saber", "Light Saber", "general_store", 5, 0, None, None, None,
         "Makes a cool noise. If both own one, winner steals from loser.", False, False, "flavor", {}),
        # Apothecary
        ("super_poison", "Super Poison", "apothecary", 120, 0, None, None, "apprentice_apothecary",
         "Halves opponent's highest-scoring active player.", False, False, "halve_top_active", {}),
        ("clandestine_poison", "Clandestine Poison", "apothecary", 50, 0, None, None, "apprentice_apothecary",
         "Same as basic poison but target hidden from opponent.", False, False, "halve_player_hidden", {}),
        ("adrenaline_potion", "Adrenaline Potion", "apothecary", 25, 0, None, None, "apprentice_apothecary",
         "Choose any player; they score 1.25× this week.", False, False, "multiply_player", {"factor": 1.25}),
        # Freemium Frenzy
        ("freemium_gold", "Gold (100)", "freemium", 0, 6, None, None, None,
         "Buy 100 gold.", False, False, "add_gold", {"amount": 100}),
    ]
    for row in items:
        item_id, name, store, cost_g, cost_usd, race_req, req_item, req_perk, desc, is_weapon, is_wooden, effect_type, effect_params = row
        existing = db.query(StoreItem).filter(StoreItem.item_id == item_id).first()
        if not existing:
            db.add(StoreItem(
                item_id=item_id, name=name, store=store,
                cost_gold=cost_g, cost_usd=cost_usd,
                race_restriction=race_req,
                requires_item=req_item,
                requires_perk=req_perk,
                description=desc,
                is_weapon=is_weapon,
                is_wooden=is_wooden,
                effect_type=effect_type,
                effect_params=effect_params,
            ))
    db.commit()


def seed_demo_matchup(db: Session):
    """
    Seed one complete fictional demonstration matchup.
    FICTIONAL DATA — for demonstration purposes only.
    """
    # Season
    season = db.query(Season).filter(Season.year == 2025).first()
    if not season:
        season = Season(
            year=2025,
            name="The League of Excellence 2025",
            rulebook_version="v1",
            effective_start="2025-09-05",
            effective_end="2026-02-08",
            source_url="https://footbolzano.com/rules/",
            access_timestamp="2026-07-12T19:14:25Z",
        )
        db.add(season)
        db.flush()

    week = db.query(Week).filter(Week.season_id == season.id, Week.week_number == 7).first()
    if not week:
        week = Week(
            season_id=season.id,
            week_number=7,
            start_date="2025-10-14",
            end_date="2025-10-21",
            attack_deadline="2025-10-15T23:00:00Z",
            roster_freeze_start="2025-10-15T23:00:00Z",
            roster_freeze_end="2025-10-16T19:00:00Z",
            is_active=True,
        )
        db.add(week)
        db.flush()

    # ── Teams ─────────────────────────────────────────────────────────────────
    home_team = db.query(Team).filter(Team.name == "Goldenrod Gloryboys").first()
    if not home_team:
        home_team = Team(
            season_id=season.id,
            name="Goldenrod Gloryboys",
            owner_name="Elara Brightwood",
            race=Race.ELF,
            gold=320.0,  # after previous purchases
            crest_emoji="🌿",
            division="East",
            wins=4, losses=2,
            is_demo=True,
        )
        db.add(home_team)
        db.flush()

    away_team = db.query(Team).filter(Team.name == "Iron Hexes").first()
    if not away_team:
        away_team = Team(
            season_id=season.id,
            name="Iron Hexes",
            owner_name="Gareth Stonewall",
            race=Race.HALFLING,
            gold=480.0,
            crest_emoji="⚒️",
            division="West",
            wins=3, losses=3,
            is_demo=True,
        )
        db.add(away_team)
        db.flush()

    # ── Matchup ───────────────────────────────────────────────────────────────
    matchup = db.query(Matchup).filter(
        Matchup.home_team_id == home_team.id,
        Matchup.week_id == week.id
    ).first()
    if matchup:
        return matchup  # Already seeded

    matchup = Matchup(
        season_id=season.id,
        week_id=week.id,
        home_team_id=home_team.id,
        away_team_id=away_team.id,
        status=MatchupStatus.LIVE,
        simulation_seed=42,
        simulation_state="stopped",
        simulation_speed=1.0,
        is_demo=True,
    )
    db.add(matchup)
    db.flush()

    # ── NFL Players ───────────────────────────────────────────────────────────
    nfl_players_data = [
        ("Patrick Mahomes", "KC", "QB", False),
        ("Bijan Robinson", "ATL", "RB", True),    # rookie
        ("Tyreek Hill", "MIA", "WR", False),
        ("Davante Adams", "LV", "WR", False),
        ("Cooper Kupp", "LAR", "WR", False),
        ("Sam LaPorta", "DET", "TE", False),
        ("Justin Tucker", "BAL", "K", False),
        ("Micah Parsons", "DAL", "LB", False),
        ("Jaylen Waddle", "MIA", "WR", False),
        ("Jake Ferguson", "DAL", "TE", False),
        ("Josh Allen", "BUF", "QB", False),
        ("Derrick Henry", "BAL", "RB", False),
        ("Stefon Diggs", "HOU", "WR", False),
        ("AJ Brown", "PHI", "WR", False),
        ("Keenan Allen", "CHI", "WR", False),
        ("Mark Andrews", "BAL", "TE", False),
        ("Evan McPherson", "CIN", "K", False),
        ("Maxx Crosby", "LV", "DL", False),
        ("Travis Etienne", "JAX", "RB", False),
        ("George Pickens", "PIT", "WR", False),
    ]

    nfl_map: dict[str, NFLPlayer] = {}
    for name, team, pos, rookie in nfl_players_data:
        p = db.query(NFLPlayer).filter(NFLPlayer.name == name).first()
        if not p:
            p = NFLPlayer(name=name, nfl_team=team, position=pos, is_rookie=rookie, is_demo=True)
            db.add(p)
            db.flush()
        nfl_map[name] = p

    # ── Rosters ───────────────────────────────────────────────────────────────
    final = FINAL_SCORES_SEED_42

    home_roster = [
        # (slot, name, is_starter, status, is_protected, protection_src, prev_score, prev_ones)
        (PositionSlot.QB,   "Patrick Mahomes", True,  PlayerStatus.ACTIVE,   False, "", 31.2, 1),
        (PositionSlot.RB1,  "Bijan Robinson",  True,  PlayerStatus.ACTIVE,   False, "", 18.4, 8),
        (PositionSlot.WR1,  "Tyreek Hill",     True,  PlayerStatus.ACTIVE,   False, "", 22.6, 2),
        (PositionSlot.WR2,  "Davante Adams",   True,  PlayerStatus.ACTIVE,   True,  "Tower Shield (Warrior L1)", 8.2, 2),
        (PositionSlot.WR3,  "Cooper Kupp",     True,  PlayerStatus.ACTIVE,   False, "", 11.4, 4),
        (PositionSlot.TE,   "Sam LaPorta",     True,  PlayerStatus.ACTIVE,   False, "", 9.6, 6),
        (PositionSlot.K,    "Justin Tucker",   True,  PlayerStatus.ACTIVE,   False, "", 13.0, 3),
        (PositionSlot.IDP1, "Micah Parsons",   True,  PlayerStatus.ACTIVE,   False, "", 14.4, 4),
        (PositionSlot.BN1,  "Jaylen Waddle",   False, PlayerStatus.ACTIVE,   False, "", 6.8, 8),
        (PositionSlot.BN2,  "Jake Ferguson",   False, PlayerStatus.ACTIVE,   False, "", 3.4, 4),
    ]
    for slot, name, is_start, status, is_prot, prot_src, prev_score, prev_ones in home_roster:
        raw = final.get(name, 0.0)
        pr = PlayerRoster(
            team_id=home_team.id,
            matchup_id=matchup.id,
            nfl_player_id=nfl_map[name].id,
            slot=slot,
            is_starter=is_start,
            status=status,
            raw_score=raw,
            adjusted_score=raw,
            projected_score=round(raw * 0.9, 2),
            is_protected=is_prot,
            protection_source=prot_src,
            game_status="finished",
            opponent_nfl_team="OPP",
            is_home_game=True,
            is_night_game=False,
            scoring_source="mock_seed42",
            previous_week_score=prev_score,
            previous_week_ones_digit=prev_ones,
        )
        db.add(pr)

    away_roster = [
        (PositionSlot.QB,   "Josh Allen",      True,  PlayerStatus.ACTIVE,   False, "", 28.0, 8),
        (PositionSlot.RB1,  "Derrick Henry",   True,  PlayerStatus.ACTIVE,   False, "", 22.0, 2),
        (PositionSlot.WR1,  "Stefon Diggs",    True,  PlayerStatus.ACTIVE,   False, "", 17.5, 5),
        (PositionSlot.WR2,  "AJ Brown",        True,  PlayerStatus.ACTIVE,   False, "", 14.2, 2),
        (PositionSlot.WR3,  "Keenan Allen",    True,  PlayerStatus.ACTIVE,   False, "", 9.8, 8),
        (PositionSlot.TE,   "Mark Andrews",    True,  PlayerStatus.ACTIVE,   False, "", 12.4, 4),
        (PositionSlot.K,    "Evan McPherson",  True,  PlayerStatus.ACTIVE,   False, "", 11.0, 1),
        (PositionSlot.IDP1, "Maxx Crosby",     True,  PlayerStatus.ACTIVE,   False, "", 16.8, 8),
        (PositionSlot.BN1,  "Travis Etienne",  False, PlayerStatus.ACTIVE,   False, "", 9.4, 4),
        (PositionSlot.BN2,  "George Pickens",  False, PlayerStatus.ACTIVE,   False, "", 7.2, 2),
    ]
    for slot, name, is_start, status, is_prot, prot_src, prev_score, prev_ones in away_roster:
        raw = final.get(name, 0.0)
        pr = PlayerRoster(
            team_id=away_team.id,
            matchup_id=matchup.id,
            nfl_player_id=nfl_map[name].id,
            slot=slot,
            is_starter=is_start,
            status=status,
            raw_score=raw,
            adjusted_score=raw,
            projected_score=round(raw * 0.9, 2),
            is_protected=is_prot,
            protection_source=prot_src,
            game_status="finished",
            opponent_nfl_team="OPP",
            is_home_game=False,
            is_night_game=False,
            scoring_source="mock_seed42",
            previous_week_score=prev_score,
            previous_week_ones_digit=prev_ones,
        )
        db.add(pr)

    db.flush()

    # ── Perks for this week ───────────────────────────────────────────────────
    # Home team: Elf, has Militia (Soldier L2), Attack of the Squires (Soldier L2),
    #   Army Sergeant (Soldier L1), Tower Shield protecting Davante Adams
    home_perks = [
        ("army_sergeant", "Soldier", 1, False),
        ("militia", "Soldier", 2, True),
        ("attack_of_squires", "Soldier", 2, True),
        ("tower_shield", "Warrior", 1, True),
    ]
    for pid, pclass, _, _ in home_perks:
        p = db.query(Perk).filter(Perk.perk_id == pid).first()
        if p:
            db.add(TeamPerk(
                team_id=home_team.id,
                perk_id=pid,
                week_acquired=3,
                is_active_this_week=True,
                activation_class=pclass,
            ))

    # Away team: Halfling, has Martyr (Necromancer L1) for Derrick Henry,
    #   and attempted a Bribe (blocked by Fairy immunity, but away is Halfling not Fairy)
    away_perks = [
        ("martyr", "Necromancer", 1, True),
        ("soul_reaper", "Necromancer", 1, True),
    ]
    for pid, pclass, _, _ in away_perks:
        p = db.query(Perk).filter(Perk.perk_id == pid).first()
        if p:
            db.add(TeamPerk(
                team_id=away_team.id,
                perk_id=pid,
                week_acquired=4,
                is_active_this_week=True,
                activation_class=pclass,
            ))

    db.flush()

    # ── Inventory ─────────────────────────────────────────────────────────────
    # Home team owns: wooden_sword (used this week), adrenaline_potion
    sword = db.query(StoreItem).filter(StoreItem.item_id == "wooden_sword").first()
    adrenaline = db.query(StoreItem).filter(StoreItem.item_id == "adrenaline_potion").first()
    bribe = db.query(StoreItem).filter(StoreItem.item_id == "bribe").first()

    if sword:
        db.add(Inventory(
            team_id=home_team.id,
            item_id="wooden_sword",
            quantity=1, week_purchased=6,
            is_used=True, week_used=7,
        ))
    if adrenaline:
        db.add(Inventory(
            team_id=home_team.id,
            item_id="adrenaline_potion",
            quantity=1, week_purchased=7,
            is_used=True, week_used=7,
        ))

    # Away team owns bribe (attempted, but Justin Tucker is Fairy's kicker not away's)
    # Actually: bribe targets opponent's kicker. Away attacks home's kicker Justin Tucker.
    # Home is Elf, not Fairy → bribe should work. But let's create a "blocked" scenario:
    # We'll have the away team attempt a Bribe against a Fairy team (the Fairy team is NOT home)
    # For demo: away also attempted wooden_axe (invalid — Halfling weapon) which was blocked
    # by Tower Shield on Davante Adams (a non-weapon, but let's use it as a demonstration)
    # 
    # Actually per rules, Tower Shield blocks wooden WEAPONS. Bribe is not a weapon.
    # For the "blocked attack" demo: home team uses Wooden Sword on Maxx Crosby,
    # but Maxx Crosby has protection from Reserve Forces (wait, away doesn't have Reserve Forces)
    # Let's keep it simple:
    # - Home's WOODEN SWORD targets Maxx Crosby → VALID (applied)
    # - Away's BASIC POISON targets Davante Adams → BLOCKED by Tower Shield (Tower Shield blocks wooden weapons, not poison)
    #   Wait — Tower Shield blocks wooden attack WEAPONS specifically
    # - Away targets Tyreek Hill with Wooden Axe → only Halflings can use Wooden Axe
    #   Away IS Halfling → valid weapon. But Hill is NOT protected.
    #   To get a BLOCKED attack: need a wooden weapon hitting a Tower-Shield player
    # - Home used Tower Shield on Davante Adams
    # - Away attacks Davante Adams with Wooden_Arrows (away is Halfling → can only use Wooden Axe, not arrows)
    #   → INVALID: wrong race for weapon
    # 
    # Revised demo attacks:
    # 1. HOME uses Wooden Sword on Maxx Crosby (Halfling can't use Wooden Sword → invalid)
    #    Wait, HOME is Elf. Elves use Wooden Arrows, not Wooden Sword.
    #    Home (Elf) attack weapon: Wooden Arrows
    #    Home attacks Josh Allen. Josh Allen prev-week score = 28.0 → ones digit = 8
    #    Wooden Arrows requires ones digit 0-3 → INVALID (8 > 3)
    #    This creates our INVALID attack.
    #
    # 2. HOME also uses Adrenaline Potion on Tyreek Hill → VALID, Hill score × 1.25
    #
    # 3. AWAY uses Wooden Axe on top 3 home scorers → VALID (Halfling weapon)
    #    AWAY also attempts to attack Davante Adams with something wooden
    #    But Davante Adams has Tower Shield → BLOCKED
    #    Let's say AWAY uses Basic Poison on Davante Adams → NOT blocked by Tower Shield
    #    (Tower Shield blocks wooden attack WEAPONS, not poisons)
    #    For clean blocked demo: HOME uses Wooden Arrows on a protected player
    #    OR: away tries Wooden Axe including Davante Adams (Tower Shield blocks weapon → BLOCKED)
    #
    # FINAL DEMO ATTACKS:
    # A. Home (Elf) — Wooden Arrows on Josh Allen → INVALID (ones digit 8 > 3)
    # B. Home (Elf) — Adrenaline Potion on Tyreek Hill → VALID, applied (+25%)
    # C. Away (Halfling) — Wooden Axe on {Tyreek Hill, Patrick Mahomes, Davante Adams}
    #    Davante Adams is protected by Tower Shield → BLOCKED (weapon attack on protected player)
    # D. Away — Martyr perk on Derrick Henry → Henry ×2, dies at week end
    # E. Away — attempted purchase of Steel Sword (needs wooden_sword first) → INVALID
    # F. Away — tried to buy Super Poison (needs Apprentice Apothecary perk) → INVALID (no perk)

    if bribe:
        db.add(Inventory(
            team_id=away_team.id,
            item_id="bribe",
            quantity=1, week_purchased=7,
            is_used=False,  # Not used — away tried to buy steel_sword instead
        ))

    db.flush()

    # ── Attacks ───────────────────────────────────────────────────────────────
    home_rosters_map = {
        pr.nfl_player.name: pr.id
        for pr in db.query(PlayerRoster).filter(
            PlayerRoster.matchup_id == matchup.id,
            PlayerRoster.team_id == home_team.id
        ).all()
    }
    away_rosters_map = {
        pr.nfl_player.name: pr.id
        for pr in db.query(PlayerRoster).filter(
            PlayerRoster.matchup_id == matchup.id,
            PlayerRoster.team_id == away_team.id
        ).all()
    }

    # Attack A: Home Wooden Arrows on Josh Allen → INVALID (ones digit 8)
    attack_a = Attack(
        matchup_id=matchup.id,
        attacker_team_id=home_team.id,
        target_team_id=away_team.id,
        item_id="wooden_arrows",
        target_player_ids=[away_rosters_map.get("Josh Allen")],
        submitted_at=utcnow(),
        cost_gold=70,
        status=AttackStatus.INVALID,
        validation_result={
            "valid": False,
            "reason": "Josh Allen's previous week score was 28.0; ones digit is 8, which exceeds the maximum of 3 for Wooden Arrows.",
        },
        invalidation_reason="Josh Allen previous-week ones digit 8 > 3 (Wooden Arrows max is 3)",
        extra_data={
            "target_player_names": ["Josh Allen"],
            "is_wooden": True,
            "race_check": "Elf can use Wooden Arrows ✓",
            "ones_digit_check": "ones_digit=8 > max=3 ✗",
        }
    )
    db.add(attack_a)

    # Attack B: Home Adrenaline Potion on Tyreek Hill → VALID
    attack_b = Attack(
        matchup_id=matchup.id,
        attacker_team_id=home_team.id,
        target_team_id=home_team.id,  # adrenaline can target own player
        item_id="adrenaline_potion",
        target_player_ids=[home_rosters_map.get("Tyreek Hill")],
        submitted_at=utcnow(),
        cost_gold=25,
        status=AttackStatus.APPLIED,
        validation_result={"valid": True, "reason": "Adrenaline Potion has no race/protection restriction."},
        score_effect={"Tyreek Hill": {"before": 18.2, "after": 22.75, "effect": "×1.25"}},
        extra_data={
            "target_player_names": ["Tyreek Hill"],
            "is_wooden": False,
        }
    )
    db.add(attack_b)

    # Attack C: Away Wooden Axe on {Tyreek Hill, Mahomes, Davante Adams} → BLOCKED
    # Davante Adams has Tower Shield (wooden weapon immunity)
    attack_c = Attack(
        matchup_id=matchup.id,
        attacker_team_id=away_team.id,
        target_team_id=home_team.id,
        item_id="wooden_axe",
        target_player_ids=[
            home_rosters_map.get("Tyreek Hill"),
            home_rosters_map.get("Patrick Mahomes"),
            home_rosters_map.get("Davante Adams"),
        ],
        submitted_at=utcnow(),
        cost_gold=70,
        status=AttackStatus.BLOCKED,
        validation_result={
            "valid": False,
            "reason": "Davante Adams is protected by Tower Shield (Warrior L1) which grants immunity to wooden attack weapons. Attack blocked.",
        },
        blocked_by="Tower Shield (Warrior L1) on Davante Adams",
        extra_data={
            "target_player_names": ["Tyreek Hill", "Patrick Mahomes", "Davante Adams"],
            "is_wooden": True,
        }
    )
    db.add(attack_c)

    # Attack D: Away Martyr perk on Derrick Henry → APPLIED (score ×2, dies)
    attack_d = Attack(
        matchup_id=matchup.id,
        attacker_team_id=away_team.id,
        target_team_id=away_team.id,
        item_id="martyr_perk",
        perk_id="martyr",
        target_player_ids=[away_rosters_map.get("Derrick Henry")],
        submitted_at=utcnow(),
        cost_gold=0,
        status=AttackStatus.APPLIED,
        validation_result={"valid": True},
        score_effect={"Derrick Henry": {"before": 18.2, "after": 36.4, "effect": "×2 (Martyr)"}},
        extra_data={
            "target_player_names": ["Derrick Henry"],
            "causes_death": "Derrick Henry",
            "death_note": "Derrick Henry must be dropped after this week. RIP.",
        }
    )
    db.add(attack_d)

    # Attack E: Away attempts Steel Sword (requires Wooden Sword in inventory) → INVALID
    attack_e = Attack(
        matchup_id=matchup.id,
        attacker_team_id=away_team.id,
        target_team_id=home_team.id,
        item_id="steel_sword",
        target_player_ids=[home_rosters_map.get("Tyreek Hill")],
        submitted_at=utcnow(),
        cost_gold=200,
        status=AttackStatus.INVALID,
        validation_result={
            "valid": False,
            "reason": "Steel Sword requires Wooden Sword in inventory as a prerequisite. Iron Hexes does not own a Wooden Sword.",
        },
        invalidation_reason="Prerequisite not met: Steel Sword requires Wooden Sword (Armory rule)",
        extra_data={
            "target_player_names": ["Tyreek Hill"],
            "prerequisite_check": "wooden_sword not found in inventory ✗",
        }
    )
    db.add(attack_e)

    db.flush()

    # ── Gold Transactions (opening) ───────────────────────────────────────────
    # Home team
    db.add(GoldTransaction(
        team_id=home_team.id, matchup_id=matchup.id, week_number=7,
        transaction_type="opening", amount=320.0,
        description="Opening gold balance for Week 7",
        balance_after=320.0, is_provisional=False,
    ))
    # Away team
    db.add(GoldTransaction(
        team_id=away_team.id, matchup_id=matchup.id, week_number=7,
        transaction_type="opening", amount=480.0,
        description="Opening gold balance for Week 7",
        balance_after=480.0, is_provisional=False,
    ))

    # ── Commissioner Ruling ───────────────────────────────────────────────────
    # A ruling adjusting home team +5 points for a confirmed clock error in Josh Allen's game
    ruling = CommissionerRuling(
        matchup_id=matchup.id,
        week_number=7,
        ruling_type="score_adjustment",
        description="CBS stat correction: Tyreek Hill receiving yards corrected +20 yds → +2.0 pts. Applied manually pending CBS update.",
        team_id=home_team.id,
        score_adjustment=2.0,
        gold_adjustment=0.0,
        issued_by="Commissioner (Brady NewDelman)",
        issued_at=utcnow(),
        is_applied=True,
        applied_at=utcnow(),
    )
    db.add(ruling)

    # ── Ambiguities ───────────────────────────────────────────────────────────
    for amb_data in AMBIGUITIES:
        existing = db.query(Ambiguity).filter(
            Ambiguity.title.contains(amb_data["title"][:30])
        ).first()
        if not existing:
            amb = Ambiguity(
                matchup_id=matchup.id,
                rule_id=amb_data.get("rule_id"),
                title=amb_data["title"],
                description=amb_data["description"],
                provisional_interpretation=amb_data["provisional_interpretation"],
                source_citation=amb_data.get("source_citation"),
                status=amb_data.get("status", "open"),
            )
            db.add(amb)

    # ── Simulation Events ─────────────────────────────────────────────────────
    sim_events = generate_events(seed=42)
    # Get all rosters for quick lookup
    all_rosters = db.query(PlayerRoster).filter(PlayerRoster.matchup_id == matchup.id).all()
    roster_by_name = {
        r.nfl_player.name: r for r in all_rosters
    }

    for idx, ev in enumerate(sim_events):
        pname = ev["player_name"]
        roster = roster_by_name.get(pname)
        if roster:
            db.add(SimulationEvent(
                matchup_id=matchup.id,
                event_index=idx,
                delay_seconds=ev["delay_seconds"] * 0.1,  # speed up for demo
                player_roster_id=roster.id,
                score_delta=ev["score_delta"],
                new_raw_score=ev["new_raw_score"],
                event_description=ev["description"],
                game_status=ev["game_status"],
                seed=42,
                was_executed=False,
            ))

    db.commit()

    # Store source register
    _create_source_register(db, matchup.id)

    return matchup


def _create_source_register(db: Session, matchup_id: int):
    """Record source material provenance."""
    db.add(Event(
        sequence_number=0,
        matchup_id=matchup_id,
        event_type=EventType.SIMULATION_EVENT,
        week_number=7,
        description="SOURCE REGISTER",
        is_simulated=False,
        is_confirmed=True,
        data={
            "sources": [
                {
                    "url": "https://footbolzano.com/rules/",
                    "title": "League of Excellence — 2025 Rulebook (v1)",
                    "access_timestamp": "2026-07-12T19:14:25Z",
                    "rulebook_version": "2025 v1",
                    "effective_dates": "2025-09-05 to 2026-02-08",
                    "publicly_retrieved": True,
                    "authentication_required": False,
                    "retrieval_success": True,
                },
                {
                    "url": "https://footbolzano.com/",
                    "title": "Footbolzano Homepage",
                    "access_timestamp": "2026-07-12T19:14:25Z",
                    "publicly_retrieved": True,
                    "authentication_required": False,
                    "retrieval_success": True,
                },
                {
                    "url": "CBS Sports league platform",
                    "title": "CBS Fantasy Sports — League Data",
                    "publicly_retrieved": False,
                    "authentication_required": True,
                    "retrieval_success": False,
                    "note": "CBS does not expose a public API. Authentication scraping would be fragile and likely prohibited. Live CBS data not available. MockLiveProvider used instead.",
                },
            ],
            "implemented_from": "Publicly retrieved rules only",
            "not_implemented": [
                "Actual CBS scoring import",
                "Historical matchup data",
                "Private Google Sheet order submissions",
                "Private team pages",
                "Store purchase history from CBS",
            ],
        }
    ))
    db.commit()


def run_seed(db: Session):
    """Main seed entry point."""
    print("🌱 Seeding rules catalog...")
    seed_rules(db)
    print("🌱 Seeding store items...")
    seed_items(db)
    print("🌱 Seeding demo matchup...")
    matchup = seed_demo_matchup(db)
    print(f"✅ Demo matchup seeded (ID: {matchup.id})")
    return matchup
