import React from 'react';
import type { TeamState } from '../types';
import { RACE_EMOJIS, RACE_COLORS } from '../types';
import PlayerRow from './PlayerRow';

interface TeamPanelProps {
  team: TeamState;
  isHome?: boolean;
  showBench?: boolean;
}

const TeamPanel: React.FC<TeamPanelProps> = ({ team, showBench = true }) => {
  const raceColor = RACE_COLORS[team.race] || '#888';
  const raceEmoji = RACE_EMOJIS[team.race] || '⚔️';
  const starters = team.players.filter(p => !p.slot.startsWith('BN'));
  const bench = team.players.filter(p => p.slot.startsWith('BN'));
  const delta = team.adjusted_score - team.raw_score;

  return (
    <div className="card" style={{ borderColor: `${raceColor}30` }}>
      {/* Team Header */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <span style={{ fontSize: '1.4rem' }}>{raceEmoji}</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 800, fontSize: '1rem' }}>{team.name}</div>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
              {team.owner_name}
            </div>
          </div>
          <div
            className="badge badge-race"
            style={{
              background: `${raceColor}18`,
              borderColor: `${raceColor}40`,
              color: raceColor,
            }}
          >
            {team.race}
          </div>
        </div>

        {/* Scores */}
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12, marginBottom: 10 }}>
          <div>
            <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', letterSpacing: '0.08em', textTransform: 'uppercase' }}>
              Adjusted Score
            </div>
            <div
              className="score-big"
              style={{ color: raceColor }}
            >
              {team.adjusted_score.toFixed(2)}
            </div>
          </div>
          <div style={{ paddingBottom: 6 }}>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
              Raw: {team.raw_score.toFixed(2)}
            </div>
            {delta !== 0 && (
              <div
                style={{
                  fontSize: '0.75rem',
                  fontWeight: 700,
                  color: delta > 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                }}
              >
                Effects: {delta > 0 ? '+' : ''}{delta.toFixed(2)}
              </div>
            )}
          </div>
        </div>

        {/* Gold */}
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
          <span className="gold-pill">
            🪙 {team.gold.toFixed(0)}g
          </span>
          {team.race_gold_earned > 0 && (
            <span className="gold-pill" style={{ background: 'rgba(34,197,94,0.1)', borderColor: 'rgba(34,197,94,0.25)', color: 'var(--accent-green)' }}>
              ⚡ +{team.race_gold_earned.toFixed(0)}g race
            </span>
          )}
        </div>

        {/* Applied Effects */}
        {team.applied_effects.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 8 }}>
            {team.applied_effects.map((eff, i) => (
              <span key={i} className="effect-tag" style={{ fontSize: '0.68rem' }}>{eff}</span>
            ))}
          </div>
        )}

        {/* Perks */}
        {team.perks.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {team.perks.map(p => (
              <span
                key={p}
                style={{
                  fontSize: '0.65rem',
                  background: 'rgba(168,85,247,0.1)',
                  border: '1px solid rgba(168,85,247,0.25)',
                  color: '#c084fc',
                  padding: '1px 7px',
                  borderRadius: 999,
                }}
              >
                🔮 {p.replace(/_/g, ' ')}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Player Table */}
      <div style={{ overflowX: 'auto' }}>
        <table className="data-table">
          <thead>
            <tr>
              <th>Player</th>
              <th className="num" style={{ textAlign: 'right' }}>Raw</th>
              <th className="num" style={{ textAlign: 'right' }}>Score</th>
              <th>Effects</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {starters.map(p => (
              <PlayerRow key={p.roster_id} player={p} />
            ))}
            {showBench && bench.length > 0 && (
              <>
                <tr>
                  <td
                    colSpan={5}
                    style={{
                      padding: '6px 12px',
                      fontSize: '0.65rem',
                      letterSpacing: '0.1em',
                      textTransform: 'uppercase',
                      color: 'var(--text-muted)',
                      background: 'rgba(99,102,241,0.05)',
                      borderBottom: '1px solid var(--border)',
                    }}
                  >
                    Bench — Bench Score: {team.bench_score.toFixed(2)}
                  </td>
                </tr>
                {bench.map(p => (
                  <PlayerRow key={p.roster_id} player={p} showBench />
                ))}
              </>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default TeamPanel;
