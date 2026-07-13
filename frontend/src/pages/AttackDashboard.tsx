import React, { useEffect, useState, useCallback } from 'react';
import type { MatchupState } from '../types';
import { fetchDemoMatchup } from '../utils/api';
import AttackCard from '../components/AttackCard';

const AttackDashboard: React.FC = () => {
  const [matchup, setMatchup] = useState<MatchupState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('');

  const load = useCallback(async () => {
    try {
      const data = await fetchDemoMatchup();
      setMatchup(data);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return <div className="loading-spinner"><div className="spinner" /><span>Loading attacks…</span></div>;
  }
  if (error || !matchup) {
    return <div className="error-box">{error || 'No data'}</div>;
  }

  const attacks = matchup.attacks || [];
  const filtered = statusFilter ? attacks.filter(a => a.status === statusFilter) : attacks;

  const counts = {
    total: attacks.length,
    applied: attacks.filter(a => a.status === 'applied' || a.status === 'validated').length,
    blocked: attacks.filter(a => a.status === 'blocked').length,
    invalid: attacks.filter(a => a.status === 'invalid').length,
    pending: attacks.filter(a => a.status === 'pending').length,
  };

  const homeAttacks = filtered.filter(a => a.attacker_team_id === matchup.home.team_id);
  const awayAttacks = filtered.filter(a => a.attacker_team_id === matchup.away.team_id);

  return (
    <div>
      <div className="section-header">
        <div>
          <div className="section-title">⚔️ Attack Dashboard</div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
            Weapons, items, and perk effects for this week
          </div>
        </div>
        <button className="btn btn-secondary" onClick={load} style={{ fontSize: '0.75rem' }}>↻ Refresh</button>
      </div>

      {/* Stats Row */}
      <div style={{ display: 'flex', gap: 10, padding: '0 24px 16px', flexWrap: 'wrap' }}>
        {[
          { label: 'Total', value: counts.total, color: 'var(--text-primary)', filter: '' },
          { label: 'Applied', value: counts.applied, color: 'var(--accent-green)', filter: 'applied' },
          { label: 'Blocked', value: counts.blocked, color: 'var(--accent-gold)', filter: 'blocked' },
          { label: 'Invalid', value: counts.invalid, color: 'var(--accent-red)', filter: 'invalid' },
          { label: 'Pending', value: counts.pending, color: 'var(--text-muted)', filter: 'pending' },
        ].map(stat => (
          <button
            key={stat.label}
            className="btn btn-secondary"
            style={{
              borderColor: statusFilter === stat.filter ? stat.color : 'var(--border)',
              color: stat.color,
              fontWeight: 700,
              padding: '8px 16px',
            }}
            onClick={() => setStatusFilter(prev => prev === stat.filter ? '' : stat.filter)}
          >
            {stat.label}: {stat.value}
          </button>
        ))}
      </div>

      {/* Attack Cost Summary */}
      <div style={{ padding: '0 24px 16px' }}>
        <div className="card">
          <div className="card-title">💰 Gold Spent on Attacks</div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            {[matchup.home, matchup.away].map(team => {
              const teamAttacks = attacks.filter(a => a.attacker_team_id === team.team_id);
              const totalCost = teamAttacks.reduce((sum, a) => sum + (a.cost_gold || 0), 0);
              return (
                <div key={team.team_id}>
                  <div style={{ fontWeight: 700, marginBottom: 6 }}>{team.name}</div>
                  <div style={{ display: 'flex', gap: 12 }}>
                    <span className="gold-pill">🪙 Spent: {totalCost}g</span>
                    <span className="gold-pill" style={{ background: 'rgba(34,197,94,0.1)', borderColor: 'rgba(34,197,94,0.25)', color: 'var(--accent-green)' }}>
                      💰 Balance: {team.gold.toFixed(0)}g
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* Attacks Grid */}
      <div style={{ padding: '0 24px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
        {/* Home Attacks */}
        <div>
          <div style={{ fontWeight: 700, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>🏠 {matchup.home.name}</span>
            <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>({homeAttacks.length} attacks)</span>
          </div>
          {homeAttacks.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', padding: '20px 0' }}>
              No attacks from this team
              {statusFilter ? ` with status "${statusFilter}"` : ''}
            </div>
          ) : (
            homeAttacks.map((attack, i) => (
              <AttackCard
                key={i}
                attack={attack}
                homeTeamId={matchup.home.team_id}
                homeTeamName={matchup.home.name}
                awayTeamName={matchup.away.name}
              />
            ))
          )}
        </div>

        {/* Away Attacks */}
        <div>
          <div style={{ fontWeight: 700, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>✈️ {matchup.away.name}</span>
            <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>({awayAttacks.length} attacks)</span>
          </div>
          {awayAttacks.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', padding: '20px 0' }}>
              No attacks from this team
              {statusFilter ? ` with status "${statusFilter}"` : ''}
            </div>
          ) : (
            awayAttacks.map((attack, i) => (
              <AttackCard
                key={i}
                attack={attack}
                homeTeamId={matchup.home.team_id}
                homeTeamName={matchup.home.name}
                awayTeamName={matchup.away.name}
              />
            ))
          )}
        </div>
      </div>

      {/* Attack Rules Reference */}
      <div style={{ padding: '20px 24px 0' }}>
        <div className="card">
          <div className="card-title">📖 Attack Rules Reference</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 8 }}>
            {[
              { icon: '🗡️', name: 'Wooden Sword', cost: 70, effect: 'Target player score ÷2', race: 'Any', wooden: true },
              { icon: '⚔️', name: 'Steel Sword', cost: 80, effect: 'Target player score → 0', race: 'Human', wooden: false },
              { icon: '☠️', name: 'Basic Poison', cost: 30, effect: 'Target player score ÷2 (no wooden immunity)', race: 'Any', wooden: false },
              { icon: '⚗️', name: 'Adrenaline Potion', cost: 25, effect: 'Own player score ×1.25', race: 'Any', wooden: false },
              { icon: '🔱', name: 'Twin Wooden Daggers', cost: 50, effect: 'Target player score ×0.75', race: 'Any', wooden: true },
              { icon: '🪓', name: 'Wooden Axe', cost: 70, effect: 'Replace highest score with lowest in target group', race: 'Any', wooden: true },
              { icon: '💰', name: 'Bribe', cost: 60, effect: 'Target kicker score → 0 (Fairies immune)', race: 'Any', wooden: false },
              { icon: '🏹', name: 'Wooden Arrows', cost: 70, effect: 'Target score → 0 if prev-week ones digit ≤3', race: 'Elf', wooden: true },
            ].map((item, i) => (
              <div
                key={i}
                style={{
                  background: 'var(--bg-secondary)',
                  border: '1px solid var(--border)',
                  borderRadius: 8,
                  padding: '10px 12px',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                  <span style={{ fontSize: '1.1rem' }}>{item.icon}</span>
                  <span style={{ fontWeight: 700, fontSize: '0.85rem' }}>{item.name}</span>
                  <span className="gold-pill" style={{ marginLeft: 'auto', fontSize: '0.65rem' }}>🪙 {item.cost}g</span>
                </div>
                <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)' }}>{item.effect}</div>
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 4, display: 'flex', gap: 6 }}>
                  {item.race !== 'Any' && <span>Race: {item.race}</span>}
                  {item.wooden && <span style={{ color: '#a16207' }}>🪵 Wooden (blockable)</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export default AttackDashboard;
