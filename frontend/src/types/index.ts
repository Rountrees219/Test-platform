// Footbolzano Live Scoring — TypeScript Types

export interface PlayerState {
  roster_id: number;
  name: string;
  nfl_team: string;
  position: string;
  slot: string;
  is_starter: boolean;
  status: string;
  raw_score: number;
  adjusted_score: number;
  projected_score: number;
  applied_effects: string[];
  is_protected: boolean;
  protection_source: string;
  is_dead: boolean;
  death_source: string;
  game_status: string;
  opponent_nfl_team: string;
  is_home_game: boolean;
  is_night_game: boolean;
  is_rookie: boolean;
  scoring_source: string;
  previous_week_score: number;
  previous_week_ones_digit: number;
}

export interface TeamState {
  team_id: number;
  name: string;
  owner_name: string;
  race: string;
  gold: number;
  race_gold_earned: number;
  raw_score: number;
  adjusted_score: number;
  bench_score: number;
  applied_effects: string[];
  perks: string[];
  players: PlayerState[];
  attacks: AttackState[];
}

export interface AttackState {
  id?: number;
  attacker_team_id: number;
  target_team_id?: number;
  item_id: string;
  target_player_names: string[];  // frontend normalized field
  target_players?: string[];       // API field name
  is_wooden: boolean;
  cost_gold: number;
  status: 'pending' | 'validated' | 'blocked' | 'invalid' | 'applied';
  blocked_by?: string;
  invalidation_reason?: string;
  causes_death?: string;
  extra_data?: Record<string, unknown>;
}

export interface MatchupState {
  matchup_id: number;
  week: number;
  status: string;
  simulation_state: string;
  simulation_speed: number;
  is_demo: boolean;
  last_updated: string;
  is_provisional: boolean;
  provisional_winner: string | null;
  home: TeamState;
  away: TeamState;
  attacks: AttackState[];
  gold_transactions: GoldTransaction[];
  traces?: RuleTrace[];       // only in engine output
  audit_trail?: RuleTrace[];  // API response field
  ambiguities_triggered?: string[];
  ambiguities?: string[];     // API field
  finalized: boolean;
  winner?: string;
  source_register?: SourceRegister[];
  deaths?: string[];
  rulings?: unknown[];
}

export interface RuleTrace {
  sequence: number;
  timestamp: string;
  week: number;
  matchup_id: number;
  rule_id: string;
  rule_version: string;
  source_value: number;
  target: string;
  prior_value: number;
  resulting_value: number;
  did_fire: boolean;
  was_blocked: boolean;
  was_invalid: boolean;
  reason: string;
  related_attack_id?: number;
  related_item_id?: string;
  related_perk_id?: string;
  related_ruling_id?: number;
  is_provisional: boolean;
  explanation: string;
}

export interface GoldTransaction {
  team_id: number;
  transaction_type: string;
  amount: number;
  description: string;
  is_provisional: boolean;
}

export interface SourceRegister {
  url: string;
  title: string;
  access_timestamp: string;
  rulebook_version: string;
}

export interface TimelineEvent {
  id: number;
  sequence: number;
  type: string;
  timestamp: string;
  week: number;
  team_id?: number;
  player_roster_id?: number;
  description: string;
  data: Record<string, unknown>;
  score_delta?: number;
  gold_delta?: number;
}

export interface SimulationStatus {
  matchup_id: number;
  state?: 'stopped' | 'running' | 'paused' | 'finished';
  simulation_state?: string;  // API field
  current_step?: number;
  total_steps?: number;
  simulation_event_index?: number;
  speed?: number;
  simulation_speed?: number;
  provider?: {
    total_events: number;
    executed_events: number;
    is_finished: boolean;
    is_simulated: boolean;
    seed: number;
  };
  last_event?: TimelineEvent;
}

export interface GoldLedgerEntry {
  week: number;
  description: string;
  amount: number;
  balance_after: number;
  transaction_type: string;
  is_provisional: boolean;
}

export interface GoldLedger {
  team_id: number;
  team_name: string;
  opening_balance: number;
  closing_balance: number;
  total_income: number;
  total_spent: number;
  entries: GoldLedgerEntry[];
}

export type Race = 'Elf' | 'Human' | 'Fairy' | 'Halfling' | 'Troll' | 'Dwarf';

export const RACE_EMOJIS: Record<string, string> = {
  Elf: '🧝',
  Human: '⚔️',
  Fairy: '🧚',
  Halfling: '🍀',
  Troll: '👹',
  Dwarf: '⛏️',
};

export const RACE_COLORS: Record<string, string> = {
  Elf: '#22c55e',
  Human: '#3b82f6',
  Fairy: '#a855f7',
  Halfling: '#f59e0b',
  Troll: '#ef4444',
  Dwarf: '#78716c',
};

export const POSITION_COLORS: Record<string, string> = {
  QB: '#f59e0b',
  RB: '#22c55e',
  WR: '#3b82f6',
  TE: '#a855f7',
  K: '#ec4899',
  DL: '#ef4444',
  LB: '#f97316',
  DB: '#14b8a6',
};

export const STATUS_ATTACK: Record<string, { label: string; color: string; icon: string }> = {
  pending: { label: 'Pending', color: '#94a3b8', icon: '⏳' },
  validated: { label: 'Valid', color: '#22c55e', icon: '✅' },
  blocked: { label: 'Blocked', color: '#f59e0b', icon: '🛡️' },
  invalid: { label: 'Invalid', color: '#ef4444', icon: '❌' },
  applied: { label: 'Applied', color: '#6366f1', icon: '⚡' },
};
