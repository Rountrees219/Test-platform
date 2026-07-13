import React, { useEffect, useState, useCallback } from 'react';
import type { MatchupState, SimulationStatus } from '../types';
import { RACE_COLORS } from '../types';
import { fetchDemoMatchup, controlSimulation, fetchSimulationStatus, seedDatabase } from '../utils/api';
import TeamPanel from '../components/TeamPanel';
import AttackCard from '../components/AttackCard';

const LiveMatchup: React.FC = () => {
  const [matchup, setMatchup] = useState<MatchupState | null>(null);
  const [simStatus, setSimStatus] = useState<SimulationStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showBench, setShowBench] = useState(true);
  const [seeding, setSeeding] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchDemoMatchup();
      setMatchup(data);
      const status = await fetchSimulationStatus(data.matchup_id);
      setSimStatus(status);
      setError(null);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError('Failed to load matchup: ' + msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, [load]);

  const handleSeed = async () => {
    setSeeding(true);
    try {
      await seedDatabase();
      await load();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError('Seed error: ' + msg);
    } finally {
      setSeeding(false);
    }
  };

  const handleSimControl = async (action: 'start' | 'pause' | 'resume' | 'stop' | 'restart' | 'jump_to_end') => {
    if (!matchup) return;
    try {
      await controlSimulation(matchup.matchup_id, action);
      await load();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError('Sim control error: ' + msg);
    }
  };

  if (loading) {
    return (
      <div className="loading-spinner">
        <div className="spinner" />
        <span>Loading live matchup…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <div className="error-box">
          {error}
          <button className="btn btn-secondary" style={{ marginLeft: 12 }} onClick={handleSeed} disabled={seeding}>
            {seeding ? '🌱 Seeding…' : '🌱 Seed Demo Data'}
          </button>
        </div>
      </div>
    );
  }

  if (!matchup) return null;

  const homeColor = RACE_COLORS[matchup.home.race] || '#888';
  const awayColor = RACE_COLORS[matchup.away.race] || '#888';
  const simState = simStatus?.simulation_state || simStatus?.state || matchup.simulation_state;
  const currentStep = simStatus?.provider?.executed_events || simStatus?.current_step || simStatus?.simulation_event_index || 0;
  const totalSteps = simStatus?.provider?.total_events || simStatus?.total_steps || 40;
  const progress = totalSteps > 0 ? (currentStep / totalSteps) * 100 : 0;

  // Get attacks for each team
  const allAttacks = matchup.attacks || [];

  return (
    <div>
      {/* Simulation Controls */}
      <div className="sim-controls">
        <span
          className={`sim-state sim-state-${simState}`}
        >
          {simState === 'running' ? '▶ LIVE' : simState === 'paused' ? '⏸ PAUSED' : simState === 'finished' ? '🏁 FINAL' : '⏹ STOPPED'}
        </span>

        <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
          Week {matchup.week} · Step {currentStep}/{totalSteps}
        </span>

        {totalSteps > 0 && (
          <div className="progress-bar-bg" style={{ flex: 1, maxWidth: 200 }}>
            <div
              className="progress-bar-fill"
              style={{
                width: `${progress}%`,
                background: simState === 'running' ? 'var(--accent-green)' : 'var(--accent-gold)',
              }}
            />
          </div>
        )}

        <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
          {simState === 'stopped' && (
            <button className="btn btn-primary" onClick={() => handleSimControl('start')}>▶ Start</button>
          )}
          {simState === 'running' && (
            <button className="btn btn-secondary" onClick={() => handleSimControl('pause')}>⏸ Pause</button>
          )}
          {simState === 'paused' && (
            <button className="btn btn-primary" onClick={() => handleSimControl('resume')}>▶ Resume</button>
          )}
          {(simState === 'running' || simState === 'paused') && (
            <button className="btn btn-secondary" onClick={() => handleSimControl('stop')}>⏹ Stop</button>
          )}
          <button className="btn btn-secondary" onClick={() => handleSimControl('jump_to_end')}>⏭ End</button>
          <button className="btn btn-secondary" onClick={() => handleSimControl('restart')}>↺ Reset</button>
          <button
            className="btn btn-secondary"
            onClick={() => setShowBench(b => !b)}
            style={{ fontSize: '0.75rem' }}
          >
            {showBench ? '🔻 Hide Bench' : '🔺 Show Bench'}
          </button>
          <button className="btn btn-secondary" onClick={load} style={{ fontSize: '0.75rem' }}>↻</button>
        </div>
      </div>

      {/* Scoreboard Banner */}
      <div
        style={{
          background: `linear-gradient(135deg, ${homeColor}12 0%, var(--bg-secondary) 50%, ${awayColor}12 100%)`,
          border: `1px solid var(--border)`,
          borderLeft: 'none',
          borderRight: 'none',
          padding: '20px 32px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 32,
        }}
      >
        {/* Home */}
        <div style={{ textAlign: 'right', flex: 1, maxWidth: 280 }}>
          <div style={{ fontWeight: 800, fontSize: '1rem', color: homeColor }}>{matchup.home.name}</div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>{matchup.home.owner_name}</div>
        </div>

        <div style={{ textAlign: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
            <span
              style={{ fontSize: '2.8rem', fontWeight: 900, color: homeColor, letterSpacing: '-0.02em' }}
            >
              {matchup.home.adjusted_score.toFixed(2)}
            </span>
            <div style={{ color: 'var(--text-muted)', fontWeight: 800, fontSize: '1rem' }}>
              <div>VS</div>
              {matchup.is_provisional && matchup.provisional_winner && (
                <div style={{ fontSize: '0.6rem', color: 'var(--accent-gold)', letterSpacing: '0.1em' }}>
                  {matchup.provisional_winner === matchup.home.name ? '◀' : '▶'} Leading
                </div>
              )}
            </div>
            <span
              style={{ fontSize: '2.8rem', fontWeight: 900, color: awayColor, letterSpacing: '-0.02em' }}
            >
              {matchup.away.adjusted_score.toFixed(2)}
            </span>
          </div>
          <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', letterSpacing: '0.08em' }}>
            {matchup.finalized ? '🏁 FINAL' : matchup.is_provisional ? '⚠️ PROVISIONAL' : ''}
          </div>
        </div>

        {/* Away */}
        <div style={{ textAlign: 'left', flex: 1, maxWidth: 280 }}>
          <div style={{ fontWeight: 800, fontSize: '1rem', color: awayColor }}>{matchup.away.name}</div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>{matchup.away.owner_name}</div>
        </div>
      </div>

      {/* Disclaimer */}
      <div className="disclaimer-banner">
        <span>⚠️</span>
        <span>
          <strong>DEMO MODE</strong> — All scoring data is simulated via seeded RNG (seed=42).
          This application is independent of footbolzano.com.
          Rules sourced from: <a href="https://footbolzano.com/rules/" target="_blank" rel="noopener noreferrer">footbolzano.com/rules/</a>
        </span>
      </div>

      {/* Main Matchup Grid */}
      <div className="matchup-grid">
        {/* Home Team */}
        <TeamPanel team={matchup.home} isHome showBench={showBench} />

        {/* VS Column */}
        <div className="vs-divider">
          <div style={{ fontSize: '0.6rem', color: 'var(--text-muted)', letterSpacing: '0.1em' }}>WEEK</div>
          <div style={{ fontSize: '1.2rem', fontWeight: 900, color: 'var(--accent-gold)' }}>{matchup.week}</div>
          <div style={{ width: 1, flex: 1, background: 'var(--border)', minHeight: 40 }} />
        </div>

        {/* Away Team */}
        <TeamPanel team={matchup.away} isHome={false} showBench={showBench} />
      </div>

      {/* Attacks Section */}
      {allAttacks.length > 0 && (
        <div style={{ padding: '0 24px', maxWidth: 1400, margin: '0 auto' }}>
          <div className="card">
            <div className="card-title">⚔️ Attacks & Items ({allAttacks.length})</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: 8 }}>
              {allAttacks.map((attack, i) => (
                <AttackCard
                  key={i}
                  attack={attack}
                  homeTeamId={matchup.home.team_id}
                  homeTeamName={matchup.home.name}
                  awayTeamName={matchup.away.name}
                />
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Last updated */}
      <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.65rem', padding: '16px 0' }}>
        Last updated: {new Date(matchup.last_updated).toLocaleTimeString()} ·
        Source: mock data seed=42
      </div>
    </div>
  );
};

export default LiveMatchup;
