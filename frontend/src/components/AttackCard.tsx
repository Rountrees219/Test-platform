import React from 'react';
import type { AttackState } from '../types';
import { STATUS_ATTACK } from '../types';

interface AttackCardProps {
  attack: AttackState;
  homeTeamId: number;
  homeTeamName: string;
  awayTeamName: string;
}

const ITEM_EMOJIS: Record<string, string> = {
  wooden_sword: '🗡️',
  steel_sword: '⚔️',
  basic_poison: '☠️',
  adrenaline_potion: '⚗️',
  twin_wooden_daggers: '🔱',
  wooden_axe: '🪓',
  bribe: '💰',
  wooden_arrows: '🏹',
  steel_arrows: '🎯',
  martyr_perk: '✝️',
  soul_reaper: '💀',
  tower_shield: '🛡️',
  reserve_forces: '🪖',
};

const ITEM_NAMES: Record<string, string> = {
  wooden_sword: 'Wooden Sword',
  steel_sword: 'Steel Sword',
  basic_poison: 'Basic Poison',
  adrenaline_potion: 'Adrenaline Potion',
  twin_wooden_daggers: 'Twin Wooden Daggers',
  wooden_axe: 'Wooden Axe',
  bribe: 'Bribe',
  wooden_arrows: 'Wooden Arrows',
  steel_arrows: 'Steel Arrows',
  martyr_perk: 'Martyr (Perk)',
  soul_reaper: 'Soul Reaper (Perk)',
  tower_shield: 'Tower Shield (Perk)',
  reserve_forces: 'Reserve Forces (Perk)',
};

const AttackCard: React.FC<AttackCardProps> = ({
  attack,
  homeTeamId,
  homeTeamName,
  awayTeamName,
}) => {
  const status = STATUS_ATTACK[attack.status] || { label: attack.status, color: '#888', icon: '?' };
  const isAttackerHome = attack.attacker_team_id === homeTeamId;
  const attackerName = isAttackerHome ? homeTeamName : awayTeamName;
  const targetName = isAttackerHome ? awayTeamName : homeTeamName;
  const itemEmoji = ITEM_EMOJIS[attack.item_id] || '⚡';
  const itemName = ITEM_NAMES[attack.item_id] || attack.item_id.replace(/_/g, ' ');
  // API returns target_players, fallback to target_player_names
  const targets = attack.target_players || attack.target_player_names || [];

  return (
    <div
      style={{
        background: 'var(--bg-secondary)',
        border: `1px solid ${status.color}30`,
        borderRadius: 10,
        padding: '12px 16px',
        marginBottom: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: '1.2rem' }}>{itemEmoji}</span>
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 700, fontSize: '0.88rem' }}>{itemName}</div>
          <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>
            {attackerName}
            {attack.item_id.includes('perk') || attack.item_id.includes('reaper') ? ' (perk effect)' : ` → ${targetName}`}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span
            style={{
              color: status.color,
              fontSize: '0.7rem',
              fontWeight: 700,
              background: `${status.color}15`,
              border: `1px solid ${status.color}30`,
              padding: '2px 8px',
              borderRadius: 999,
              letterSpacing: '0.05em',
            }}
          >
            {status.icon} {status.label}
          </span>
          {attack.cost_gold > 0 && (
            <span style={{ fontSize: '0.7rem', color: 'var(--accent-gold)' }}>
              🪙 {attack.cost_gold}g
            </span>
          )}
        </div>
      </div>

      {/* Targets */}
      {targets.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 4 }}>
          <span style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>Targets:</span>
          {targets.map((name: string, i: number) => (
            <span
              key={i}
              style={{
                fontSize: '0.68rem',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid var(--border)',
                padding: '1px 6px',
                borderRadius: 4,
              }}
            >
              {name}
            </span>
          ))}
        </div>
      )}

      {/* Reason */}
      {attack.status === 'blocked' && attack.blocked_by && (
        <div style={{ fontSize: '0.72rem', color: '#fbbf24', marginTop: 4 }}>
          🛡️ Blocked by: {attack.blocked_by}
        </div>
      )}
      {attack.status === 'invalid' && attack.invalidation_reason && (
        <div style={{ fontSize: '0.72rem', color: '#fca5a5', marginTop: 4 }}>
          ❌ {attack.invalidation_reason}
        </div>
      )}
      {attack.causes_death && (
        <div style={{ fontSize: '0.72rem', color: '#f87171', marginTop: 4 }}>
          💀 Causes death: {attack.causes_death}
        </div>
      )}
    </div>
  );
};

export default AttackCard;
