import React, { useEffect, useState, useCallback } from 'react';
import { fetchGoldLedger, fetchDemoMatchup } from '../utils/api';

interface GoldEntry {
  week: number;
  description: string;
  amount: number;
  balance_after: number;
  transaction_type: string;
  is_provisional: boolean;
}

interface TeamGold {
  team_id: number;
  team_name: string;
  opening_balance: number;
  closing_balance: number;
  total_income: number;
  total_spent: number;
  entries: GoldEntry[];
}

const GoldLedger: React.FC = () => {
  const [homeGold, setHomeGold] = useState<TeamGold | null>(null);
  const [awayGold, setAwayGold] = useState<TeamGold | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTeam, setActiveTeam] = useState<'home' | 'away'>('home');

  const load = useCallback(async () => {
    try {
      const matchup = await fetchDemoMatchup();
      const [hg, ag] = await Promise.all([
        fetchGoldLedger(matchup.home.team_id),
        fetchGoldLedger(matchup.away.team_id),
      ]);
      setHomeGold({
        ...hg,
        team_name: matchup.home.name,
        opening_balance: matchup.home.gold - (matchup.home.race_gold_earned || 0),
        closing_balance: matchup.home.gold,
        total_income: matchup.home.race_gold_earned || 0,
        total_spent: 0,
        entries: hg.entries || [],
      });
      setAwayGold({
        ...ag,
        team_name: matchup.away.name,
        opening_balance: matchup.away.gold - (matchup.away.race_gold_earned || 0),
        closing_balance: matchup.away.gold,
        total_income: matchup.away.race_gold_earned || 0,
        total_spent: 0,
        entries: ag.entries || [],
      });
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

  if (loading) return <div className="loading-spinner"><div className="spinner" /><span>Loading gold data…</span></div>;
  if (error || !homeGold || !awayGold) return <div className="error-box">{error || 'No data'}</div>;

  const activeGold = activeTeam === 'home' ? homeGold : awayGold;

  return (
    <div>
      <div className="section-header">
        <div>
          <div className="section-title">🪙 Gold Economy Ledger</div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
            Gold income, spending, and balance tracking for each team
          </div>
        </div>
        <button className="btn btn-secondary" onClick={load} style={{ fontSize: '0.75rem' }}>↻ Refresh</button>
      </div>

      {/* Comparison Card */}
      <div style={{ padding: '0 24px 16px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {[homeGold, awayGold].map((team, i) => (
          <div
            key={i}
            className="card"
            style={{
              cursor: 'pointer',
              borderColor: (i === 0 ? activeTeam === 'home' : activeTeam === 'away') ? 'var(--accent-gold)' : 'var(--border)',
            }}
            onClick={() => setActiveTeam(i === 0 ? 'home' : 'away')}
          >
            <div className="card-title">{team.team_name}</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
              <div>
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>Balance</div>
                <div className="gold-pill" style={{ fontSize: '1rem', padding: '4px 12px' }}>
                  🪙 {team.closing_balance.toFixed(0)}g
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>Race Income</div>
                <div style={{ fontWeight: 700, color: 'var(--accent-green)', fontSize: '0.9rem' }}>
                  +{team.total_income.toFixed(0)}g
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>Spent</div>
                <div style={{ fontWeight: 700, color: team.total_spent > 0 ? 'var(--accent-red)' : 'var(--text-muted)', fontSize: '0.9rem' }}>
                  -{team.total_spent.toFixed(0)}g
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Race Gold Formula Reference */}
      <div style={{ padding: '0 24px 16px' }}>
        <div className="card">
          <div className="card-title">📐 Race Gold Formulas</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 8 }}>
            {[
              { race: '🧝 Elf', formula: '2 × (top 2 WR scores) + 10', example: 'WR1+WR2=28.2 → 2×28.2+10=66.4' },
              { race: '⚔️ Human', formula: '5 × QB score', example: 'QB=17.2 → 5×17.2=86' },
              { race: '🧚 Fairy', formula: '5 × Kicker score + 50', example: 'K=8.0 → 5×8+50=90' },
              { race: '🍀 Halfling', formula: '4 × top RB score', example: 'RB1=22 → 4×22=88' },
              { race: '👹 Troll', formula: '5 × top TE score + 20', example: 'TE=12 → 5×12+20=80' },
              { race: '⛏️ Dwarf', formula: '3 × (top 2 IDP scores) + 10', example: 'IDP1+IDP2=18 → 3×18+10=64' },
            ].map((r, i) => (
              <div key={i} style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', borderRadius: 8, padding: '10px 12px' }}>
                <div style={{ fontWeight: 700, fontSize: '0.85rem', marginBottom: 4 }}>{r.race}</div>
                <div style={{ fontSize: '0.8rem', fontFamily: 'var(--font-mono)', color: 'var(--accent-teal)' }}>{r.formula}</div>
                <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginTop: 3 }}>e.g. {r.example}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Active Team Ledger */}
      <div style={{ padding: '0 24px' }}>
        <div className="card">
          <div className="card-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>💳 {activeGold.team_name} — Transaction Log</span>
            <span className="gold-pill" style={{ marginLeft: 'auto' }}>
              🪙 Balance: {activeGold.closing_balance.toFixed(0)}g
            </span>
          </div>

          {activeGold.entries.length === 0 ? (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 32, fontSize: '0.85rem' }}>
              <div style={{ fontSize: '2rem', marginBottom: 8 }}>🪙</div>
              No gold transactions recorded yet.
              <br />
              <span style={{ fontSize: '0.72rem' }}>
                Race gold is calculated at end of week.
                Opening balance for {activeGold.team_name}: {activeGold.opening_balance.toFixed(0)}g
              </span>
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Week</th>
                  <th>Description</th>
                  <th>Type</th>
                  <th className="num">Amount</th>
                  <th className="num">Balance After</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {activeGold.entries.map((entry, i) => (
                  <tr key={i}>
                    <td style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>W{entry.week}</td>
                    <td style={{ fontSize: '0.82rem' }}>{entry.description}</td>
                    <td>
                      <span style={{
                        fontSize: '0.65rem',
                        background: 'var(--bg-secondary)',
                        padding: '1px 6px',
                        borderRadius: 3,
                        color: 'var(--text-muted)',
                      }}>
                        {entry.transaction_type}
                      </span>
                    </td>
                    <td className="num" style={{
                      color: entry.amount > 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                      fontWeight: 700,
                    }}>
                      {entry.amount > 0 ? '+' : ''}{entry.amount.toFixed(0)}g
                    </td>
                    <td className="num" style={{ color: 'var(--accent-gold)' }}>
                      {entry.balance_after.toFixed(0)}g
                    </td>
                    <td>
                      {entry.is_provisional ? (
                        <span style={{ fontSize: '0.65rem', color: '#fbbf24' }}>⚠️ provisional</span>
                      ) : (
                        <span style={{ fontSize: '0.65rem', color: 'var(--accent-green)' }}>✅ confirmed</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};

export default GoldLedger;
