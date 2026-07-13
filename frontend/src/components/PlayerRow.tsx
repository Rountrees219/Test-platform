import React from 'react';
import type { PlayerState } from '../types';
import { POSITION_COLORS } from '../types';

interface PlayerRowProps {
  player: PlayerState;
  showBench?: boolean;
  highlight?: boolean;
}

function formatScore(score: number): string {
  return score.toFixed(2);
}

const PlayerRow: React.FC<PlayerRowProps> = ({ player, showBench = true, highlight = false }) => {
  if (!showBench && player.slot.startsWith('BN')) return null;

  const posColor = POSITION_COLORS[player.position] || '#888';
  const isDead = player.is_dead;
  const isProtected = player.is_protected;
  const isBench = player.slot.startsWith('BN');
  const delta = player.adjusted_score - player.raw_score;

  return (
    <tr
      className={isDead ? 'player-dead' : isProtected ? 'player-protected' : ''}
      style={highlight ? { background: 'rgba(245,200,66,0.05)' } : undefined}
    >
      <td>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span
            style={{
              background: `${posColor}20`,
              color: posColor,
              border: `1px solid ${posColor}50`,
              borderRadius: 4,
              padding: '1px 6px',
              fontSize: '0.65rem',
              fontWeight: 700,
              minWidth: 28,
              textAlign: 'center',
            }}
          >
            {player.position}
          </span>
          <div>
            <div
              className="name-col"
              style={{
                fontWeight: 600,
                fontSize: '0.85rem',
                color: isDead ? '#fca5a5' : 'var(--text-primary)',
                display: 'flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              {player.name}
              {isDead && <span title={`Died: ${player.death_source}`}>💀</span>}
              {isProtected && <span title={`Protected: ${player.protection_source}`}>🛡️</span>}
              {player.is_rookie && <span title="Rookie">⭐</span>}
            </div>
            <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>
              {player.slot} · {player.nfl_team}
              {isBench && <span style={{ marginLeft: 4, color: '#6366f1' }}>BENCH</span>}
            </div>
          </div>
        </div>
      </td>
      <td className="num">
        <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
          {formatScore(player.raw_score)}
        </span>
      </td>
      <td className="num">
        <span
          style={{
            fontWeight: 700,
            fontSize: '0.9rem',
            color: isDead ? '#9ca3af' : delta > 0 ? 'var(--accent-green)' : delta < 0 ? 'var(--accent-red)' : 'var(--text-primary)',
          }}
        >
          {formatScore(player.adjusted_score)}
        </span>
        {delta !== 0 && (
          <span
            style={{
              fontSize: '0.65rem',
              color: delta > 0 ? 'var(--accent-green)' : 'var(--accent-red)',
              marginLeft: 4,
            }}
          >
            {delta > 0 ? '+' : ''}{formatScore(delta)}
          </span>
        )}
      </td>
      <td>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3 }}>
          {player.applied_effects.slice(0, 3).map((eff, i) => (
            <span key={i} className="effect-tag">{eff}</span>
          ))}
          {player.applied_effects.length > 3 && (
            <span className="effect-tag">+{player.applied_effects.length - 3}</span>
          )}
        </div>
      </td>
      <td>
        <span
          style={{
            fontSize: '0.65rem',
            color: player.game_status === 'finished'
              ? 'var(--accent-green)'
              : player.game_status === 'active'
              ? 'var(--accent-gold)'
              : 'var(--text-muted)',
          }}
        >
          {player.game_status}
        </span>
      </td>
    </tr>
  );
};

export default PlayerRow;
