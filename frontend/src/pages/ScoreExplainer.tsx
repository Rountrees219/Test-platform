import React, { useEffect, useState, useCallback } from 'react';
import type { MatchupState } from '../types';
import { fetchDemoMatchup } from '../utils/api';
import TraceList from '../components/TraceList';

const ScoreExplainer: React.FC = () => {
  const [matchup, setMatchup] = useState<MatchupState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [selectedTeam, setSelectedTeam] = useState<'all' | 'home' | 'away'>('all');
  const [showFired, setShowFired] = useState(true);
  const [showBlocked, setShowBlocked] = useState(true);
  const [showInvalid, setShowInvalid] = useState(true);

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
    return (
      <div className="loading-spinner">
        <div className="spinner" />
        <span>Loading score data…</span>
      </div>
    );
  }

  if (error || !matchup) {
    return <div className="error-box">{error || 'No data'}</div>;
  }

  const traces = matchup.audit_trail || matchup.traces || [];

  // Filter traces
  let filtered = traces.filter(t => {
    if (!showFired && t.did_fire && !t.was_blocked && !t.was_invalid) return false;
    if (!showBlocked && t.was_blocked) return false;
    if (!showInvalid && t.was_invalid) return false;
    return true;
  });

  if (selectedTeam !== 'all') {
    const teamName = selectedTeam === 'home' ? matchup.home.name : matchup.away.name;
    filtered = filtered.filter(t => t.target.includes(teamName) || t.target.includes(matchup.home.name.split(' ')[0]));
  }

  // Group by step
  const steps: Record<string, typeof traces> = {};
  for (const t of filtered) {
    const key = t.rule_id.split('_')[0];
    if (!steps[key]) steps[key] = [];
    steps[key].push(t);
  }

  return (
    <div>
      <div className="section-header">
        <div>
          <div className="section-title">📋 Score Explanation & Audit Trail</div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
            Full deterministic rule trace — every calculation step logged
          </div>
        </div>
        <button className="btn btn-secondary" onClick={load} style={{ fontSize: '0.75rem' }}>↻ Refresh</button>
      </div>

      {/* Score Summary */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, padding: '0 24px 16px', maxWidth: 1200, margin: '0 auto' }}>
        {[matchup.home, matchup.away].map(team => (
          <div key={team.team_id} className="card">
            <div className="card-title">{team.name}</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
              <div>
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: 2 }}>Raw Score</div>
                <div style={{ fontWeight: 700, fontSize: '1.3rem' }}>{team.raw_score.toFixed(2)}</div>
              </div>
              <div>
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: 2 }}>Effects</div>
                <div style={{
                  fontWeight: 700,
                  fontSize: '1.3rem',
                  color: (team.adjusted_score - team.raw_score) > 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                }}>
                  {(team.adjusted_score - team.raw_score) >= 0 ? '+' : ''}{(team.adjusted_score - team.raw_score).toFixed(2)}
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: 2 }}>Adjusted</div>
                <div style={{ fontWeight: 700, fontSize: '1.3rem', color: 'var(--accent-gold)' }}>{team.adjusted_score.toFixed(2)}</div>
              </div>
            </div>
            {team.applied_effects.length > 0 && (
              <div style={{ marginTop: 10 }}>
                {team.applied_effects.map((eff, i) => (
                  <div key={i} style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', padding: '2px 0' }}>
                    • {eff}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Filter Controls */}
      <div style={{ padding: '0 24px 12px', display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
        <input
          type="text"
          placeholder="🔍 Search traces…"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            padding: '6px 12px',
            color: 'var(--text-primary)',
            fontSize: '0.8rem',
            flex: 1,
            maxWidth: 300,
          }}
        />
        <select
          value={selectedTeam}
          onChange={e => setSelectedTeam(e.target.value as 'all' | 'home' | 'away')}
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            padding: '6px 12px',
            color: 'var(--text-primary)',
            fontSize: '0.8rem',
          }}
        >
          <option value="all">All Teams</option>
          <option value="home">{matchup.home.name}</option>
          <option value="away">{matchup.away.name}</option>
        </select>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.78rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={showFired} onChange={e => setShowFired(e.target.checked)} />
          ✅ Fired
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.78rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={showBlocked} onChange={e => setShowBlocked(e.target.checked)} />
          🛡️ Blocked
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.78rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={showInvalid} onChange={e => setShowInvalid(e.target.checked)} />
          ❌ Invalid
        </label>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
          {filtered.length} traces
        </span>
      </div>

      {/* Trace List */}
      <div style={{ padding: '0 24px', maxWidth: 1200, margin: '0 auto' }}>
        <div className="card">
          <TraceList traces={filtered} filter={filter} maxItems={200} />
        </div>
      </div>

      {/* Ambiguities */}
      {(matchup.ambiguities || matchup.ambiguities_triggered || []).length > 0 && (
        <div style={{ padding: '16px 24px 0', maxWidth: 1200, margin: '0 auto' }}>
          <div className="card">
            <div className="card-title">⚠️ Ambiguities ({(matchup.ambiguities || matchup.ambiguities_triggered || []).length})</div>
            {(matchup.ambiguities || matchup.ambiguities_triggered || []).map((amb, i) => (
              <div
                key={i}
                style={{
                  padding: '8px 12px',
                  background: 'rgba(245,200,66,0.06)',
                  border: '1px solid rgba(245,200,66,0.2)',
                  borderRadius: 6,
                  fontSize: '0.78rem',
                  marginBottom: 6,
                }}
              >
                ⚠️ {amb}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default ScoreExplainer;
