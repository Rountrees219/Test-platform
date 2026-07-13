# ⚔️ Footbolzano Live Scoring Demo

> **DEMO MODE** — All NFL scoring data is simulated using seeded RNG (seed=42). This application is independent of footbolzano.com and not affiliated with the site.

A complete, standalone, real-time scoring application for the **Footbolzano** custom fantasy-football league. Features a deterministic rules engine, live simulation via SSE, and a full audit trail of every scoring decision.

---

## 🏆 Features

### Live Matchup Dashboard
- Real-time score display with provisional winner indicator
- Full roster tables with raw → adjusted score breakdowns
- Player-level effect annotations (Army Sergeant, Adrenaline Potion, etc.)
- Protection, death, and bench status indicators
- SSE-powered live simulation controls (start / pause / resume / stop / jump to end)

### Score Explainer
- Filterable rule trace list with 17+ entries per matchup
- Every calculation step: raw scoring → race gold → attacks → perks → rulings
- Blocked/Invalid/Fired status per rule application
- Source/value deltas for each step

### Timeline / Event Ledger
- Chronological log of all 40+ simulation events
- Filterable by type, team, or keyword
- Expandable event data payloads
- Score + gold delta annotations

### Attack Dashboard
- All attacks with status (applied / blocked / invalid / pending)
- Per-team attack breakdown
- Attack rules quick reference
- Gold cost tracking

### Gold Economy Ledger
- Race gold formula reference for all 6 races
- Per-team balance, income, and spending
- Transaction log with provisional flags

### Admin Panel
- Audit report (scores, traces, events)
- Rules catalog (30+ rules with approval status)
- Ambiguity register (8 unresolved rule interpretations)
- Database seeding interface
- Full API reference

---

## 🏗️ Architecture

```
footbolzano/
├── backend/                    # FastAPI Python backend
│   ├── app/
│   │   ├── api/                # REST endpoints
│   │   │   ├── matchup.py      # GET /api/matchup/demo, /api/matchup/{id}
│   │   │   ├── admin.py        # Seed, audit, rules, ambiguities
│   │   │   ├── gold.py         # Gold ledger endpoints
│   │   │   ├── events.py       # Timeline endpoint
│   │   │   └── simulation.py   # SSE + simulation control
│   │   ├── core/
│   │   │   ├── config.py       # Settings
│   │   │   ├── database.py     # SQLAlchemy + SQLite
│   │   │   └── seed_demo.py    # Demo data seeder
│   │   ├── models/
│   │   │   └── models.py       # All ORM models
│   │   ├── providers/
│   │   │   ├── mock_live.py    # Seeded deterministic simulation (seed=42)
│   │   │   └── manual_entry.py # Manual score entry
│   │   ├── rules/
│   │   │   ├── engine.py       # Deterministic scoring engine (20 steps)
│   │   │   ├── catalog.py      # Rules catalog
│   │   │   └── ambiguity_register.py  # 8 registered ambiguities
│   │   ├── tests/
│   │   │   └── test_engine.py  # 25 pytest tests
│   │   └── main.py             # FastAPI app
│   ├── requirements.txt
│   ├── Dockerfile
│   └── pytest.ini
├── frontend/                   # React + TypeScript frontend
│   ├── src/
│   │   ├── pages/
│   │   │   ├── LiveMatchup.tsx     # Main live score screen
│   │   │   ├── ScoreExplainer.tsx  # Rule trace viewer
│   │   │   ├── Timeline.tsx        # Event timeline
│   │   │   ├── AttackDashboard.tsx # Attack tracking
│   │   │   ├── GoldLedger.tsx      # Gold economy
│   │   │   └── AdminPanel.tsx      # Admin tools
│   │   ├── components/
│   │   │   ├── TeamPanel.tsx       # Team score card
│   │   │   ├── PlayerRow.tsx       # Player table row
│   │   │   ├── AttackCard.tsx      # Attack display
│   │   │   └── TraceList.tsx       # Rule trace list
│   │   ├── types/index.ts          # TypeScript types
│   │   ├── utils/api.ts            # API client
│   │   └── styles.css              # Global dark theme
│   ├── Dockerfile
│   ├── nginx.conf
│   └── vite.config.ts
└── docker-compose.yml
```

---

## ⚙️ Rules Engine

The scoring engine (`app/rules/engine.py`) runs 20 deterministic steps:

| Step | Description |
|------|-------------|
| 1 | Validate league state |
| 2 | Establish active rosters |
| 3 | Establish protections and immunities |
| 4 | Import raw NFL/platform scoring |
| 5 | Validate attacks and item targets |
| 6 | Apply score replacements |
| 7 | Apply score transfers |
| 8 | Apply player-specific effects (zeroing, division, multiplication, additions) |
| 9 | Apply bench effects (Attack of the Squires, Reserve Forces) |
| 10 | Calculate raw team totals |
| 11 | Apply teamwide percentage effects (Militia +20%) |
| 12 | Apply fixed team bonuses/penalties |
| 13 | Calculate race gold |
| 14 | Process purchases and gold transactions |
| 15 | Process automatic win/loss conditions |
| 16 | Process post-matchup deaths and persistent consequences |
| 17 | Apply approved manual rulings |
| 18 | Round scores |
| 19 | Determine provisional winner |
| 20 | Finalize after correction window |

### Race Gold Formulas

| Race | Formula |
|------|---------|
| 🧝 Elf | `2 × (top 2 WR scores) + 10` |
| ⚔️ Human | `5 × QB score` |
| 🧚 Fairy | `5 × Kicker score + 50` |
| 🍀 Halfling | `4 × top RB score` |
| 👹 Troll | `5 × top TE score + 20` |
| ⛏️ Dwarf | `3 × (top 2 IDP scores) + 10` |

### Attack Items

| Item | Effect | Wooden? |
|------|--------|---------|
| Wooden Sword | Target ÷2 | ✅ |
| Steel Sword | Target → 0 | ❌ |
| Basic Poison | Target ÷2 | ❌ |
| Adrenaline Potion | Own player ×1.25 | ❌ |
| Twin Wooden Daggers | Target ×0.75 | ✅ |
| Wooden Axe | Replace highest with lowest in group | ✅ |
| Bribe | Kicker → 0 (Fairies immune) | ❌ |
| Wooden Arrows | Target → 0 if prev-week ones digit ≤3 | ✅ |

### Perks Implemented

- **Army Sergeant** (Soldier L1): +30% on strength-position top scorer
- **Militia** (Soldier L2): +20% all active players
- **Attack of the Squires** (Soldier L2): ½ bench score added
- **Martyr** (Necromancer L1): Pick player, doubles score, player dies
- **Soul Reaper** (Necromancer L1): Players with raw ≤5 die
- **Tower Shield** (Warrior L1): Protection vs wooden weapons
- **Reserve Forces** (Strategist L2): Top bench player immune + scores
- **Salary Bonus** (Financier L1): Extra gold = 2× bench score

---

## 🎮 Demo Matchup (seed=42)

The seeded demo features:
- **Goldenrod Gloryboys** (Elf, Elara Brightwood) — Perks: Army Sergeant, Militia, Attack of the Squires
- **Iron Hexes** (Halfling, Gareth Stonewall) — Perks: Martyr, Soul Reaper

### Seeded Attacks
| Attack | Item | Result |
|--------|------|--------|
| A | Wooden Arrows → Josh Allen | ❌ INVALID (ones digit 8 > 3) |
| B | Adrenaline Potion → Tyreek Hill | ✅ APPLIED (×1.25) |
| C | Wooden Axe → Hill/Mahomes/Adams | 🛡️ BLOCKED (Adams has Tower Shield) |
| D | Martyr → Derrick Henry | ✅ APPLIED (×2, Henry dies) |
| E | Steel Sword → Josh Allen | ❌ INVALID (insufficient gold) |

### Commissioner Ruling
- Tyreek Hill +2.0 pts (CBS stat correction)

---

## 🚀 Quick Start

### Option 1: Docker Compose (Recommended)

```bash
cd footbolzano
docker-compose up --build
# Frontend: http://localhost:3000
# Backend API: http://localhost:8000
# API Docs: http://localhost:8000/docs
```

### Option 2: Manual

**Backend:**
```bash
cd footbolzano/backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# In another terminal:
curl -X POST http://localhost:8000/api/admin/seed
```

**Frontend:**
```bash
cd footbolzano/frontend
npm install
npm run dev
# Opens http://localhost:3000
```

---

## 🧪 Running Tests

```bash
cd footbolzano/backend
python -m pytest app/tests/test_engine.py -v
# Expected: 25 passed
```

Test coverage includes:
- Raw player scoring
- All 6 race gold formulas (Elf, Human, Fairy, Halfling, Troll, Dwarf)
- Wooden sword ÷2 effect
- Protection blocking wooden weapons
- Adrenaline potion ×1.25 (self-targeting)
- Militia +20% teamwide
- Attack of the Squires bench contribution
- Insufficient gold blocking purchase
- Martyr death mechanic
- Commissioner ruling application
- Deterministic replay (same state = same result)
- Invalid arrows (ones digit validation)
- Cannot attack dead players
- Soul Reaper kills low scorers
- Bribe + Fairy immunity
- Twin wooden daggers ×0.75
- Score finalization lock
- Mock provider determinism (seed=42)
- Mock provider replay final scores
- Wooden axe replace highest-with-lowest
- Full end-to-end demo matchup

---

## 📡 API Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/matchup/demo` | Full demo matchup with scores and traces |
| GET | `/api/matchup/{id}` | Matchup by ID |
| POST | `/api/admin/seed` | Seed demo data |
| GET | `/api/admin/matchup/{id}/audit-report` | Full audit report |
| GET | `/api/admin/rules` | Rules catalog |
| GET | `/api/admin/ambiguities` | Ambiguity register |
| GET | `/api/events/matchup/{id}/timeline` | Event timeline |
| GET | `/api/gold/team/{id}/ledger` | Team gold ledger |
| GET | `/api/gold/matchup/{id}/summary` | Gold summary |
| POST | `/api/simulation/{id}/control` | Control simulation |
| GET | `/api/simulation/{id}/sse` | SSE live event stream |
| GET | `/api/simulation/{id}/status` | Simulation status |
| GET | `/docs` | Interactive API docs (Swagger UI) |

---

## ⚠️ Registered Ambiguities

8 rule interpretations are flagged as unresolved in the ambiguity register:

- **AMB-001**: Execution order when multiple attacks target the same player
- **AMB-002**: Militia timing vs. Army Sergeant stacking order
- **AMB-003**: Out of Collateral scoring data source
- **AMB-004**: Bribe against a player not on the active roster
- **AMB-005**: Martyr + Soul Reaper interaction (double death)
- **AMB-006**: Twin daggers applied before or after other reductions
- **AMB-007**: Race gold computed before or after score adjustments
- **AMB-008**: Protection perk carries over from week to week

---

## ⚠️ Disclaimer

This application is an **independent demonstration tool**. It is not affiliated with, endorsed by, or connected to footbolzano.com or its operators. All NFL player scores are **simulated** using a seeded random number generator and are not real NFL statistics. Rules are interpreted from the publicly available 2025 Footbolzano Rulebook (v1).
