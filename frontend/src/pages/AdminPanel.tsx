import React, { useEffect, useState, useCallback } from 'react';
import { fetchAuditReport, fetchRules, fetchAmbiguities, seedDatabase } from '../utils/api';

interface AuditReport {
  matchup_id: number;
  generated_at: string;
  status: string;
  home_team: string;
  away_team: string;
  home_raw_score: number;
  away_raw_score: number;
  home_adjusted_score: number;
  away_adjusted_score: number;
  events: unknown[];
  traces: unknown[];
}

interface Rule {
  rule_id: string;
  rule_name: string;
  version: string;
  category: string;
  trigger_timing: number;
  calculation: string;
  explanation_template: string;
  approval_status: string;
  commissioner_note?: string;
}

interface Ambiguity {
  id: string;
  description: string;
  status: string;
  default_interpretation: string;
  commissioner_ruling?: string;
}

type AdminTab = 'audit' | 'rules' | 'ambiguities' | 'seed';

const AdminPanel: React.FC = () => {
  const [activeTab, setActiveTab] = useState<AdminTab>('audit');
  const [audit, setAudit] = useState<AuditReport | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [ambiguities, setAmbiguities] = useState<Ambiguity[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [seedResult, setSeedResult] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);

  const loadAudit = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchAuditReport(1);
      setAudit(data);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRules = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchRules();
      setRules(data.rules || []);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadAmbiguities = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchAmbiguities();
      setAmbiguities(data.ambiguities || []);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const handleSeed = async () => {
    setSeeding(true);
    setSeedResult(null);
    try {
      const result = await seedDatabase();
      setSeedResult(`✅ Database seeded! Matchup ID: ${result.matchup_id}`);
    } catch (e: unknown) {
      setSeedResult(`❌ Seed error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSeeding(false);
    }
  };

  useEffect(() => {
    if (activeTab === 'audit') loadAudit();
    else if (activeTab === 'rules') loadRules();
    else if (activeTab === 'ambiguities') loadAmbiguities();
  }, [activeTab, loadAudit, loadRules, loadAmbiguities]);

  return (
    <div>
      <div className="section-header">
        <div>
          <div className="section-title">🔧 Admin Panel</div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
            Audit report, rules catalog, ambiguity register, and data management
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="tabs">
        {([
          { id: 'audit' as AdminTab, label: '📋 Audit Report', icon: '📋' },
          { id: 'rules' as AdminTab, label: '📜 Rules Catalog', icon: '📜' },
          { id: 'ambiguities' as AdminTab, label: '⚠️ Ambiguities', icon: '⚠️' },
          { id: 'seed' as AdminTab, label: '🌱 Data Seed', icon: '🌱' },
        ] as { id: AdminTab; label: string; icon: string }[]).map(tab => (
          <button
            key={tab.id}
            className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="tab-content">
        {error && <div className="error-box">{error}</div>}
        {loading && <div className="loading-spinner"><div className="spinner" /><span>Loading…</span></div>}

        {/* Audit Report */}
        {activeTab === 'audit' && audit && !loading && (
          <div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 12, marginBottom: 20 }}>
              {[
                { label: 'Status', value: audit.status, color: 'var(--accent-teal)' },
                { label: 'Home Team', value: audit.home_team, color: 'var(--text-primary)' },
                { label: 'Away Team', value: audit.away_team, color: 'var(--text-primary)' },
                { label: 'Home Raw', value: audit.home_raw_score?.toFixed(2), color: 'var(--text-secondary)' },
                { label: 'Away Raw', value: audit.away_raw_score?.toFixed(2), color: 'var(--text-secondary)' },
                { label: 'Home Final', value: audit.home_adjusted_score?.toFixed(2), color: 'var(--accent-gold)' },
                { label: 'Away Final', value: audit.away_adjusted_score?.toFixed(2), color: 'var(--accent-gold)' },
                { label: 'Events', value: String((audit.events || []).length), color: 'var(--accent-purple)' },
              ].map((item, i) => (
                <div key={i} className="card" style={{ padding: '12px 16px' }}>
                  <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginBottom: 4 }}>{item.label}</div>
                  <div style={{ fontWeight: 700, color: item.color, fontSize: '0.95rem' }}>{item.value}</div>
                </div>
              ))}
            </div>
            <div className="card">
              <div className="card-title">Generated: {new Date(audit.generated_at).toLocaleString()}</div>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                Matchup ID: {audit.matchup_id} ·
                Trace count: {(audit.traces || []).length} ·
                Events: {(audit.events || []).length}
              </div>
              <div style={{ marginTop: 12, padding: '10px 12px', background: 'var(--bg-secondary)', borderRadius: 6, fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                ℹ️ Full audit report is available via the API at{' '}
                <code style={{ color: 'var(--accent-teal)' }}>/api/admin/matchup/1/audit-report</code>
              </div>
            </div>
          </div>
        )}

        {/* Rules Catalog */}
        {activeTab === 'rules' && !loading && (
          <div>
            <div style={{ marginBottom: 12, fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              {rules.length} rules in catalog
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {rules.map((rule, i) => (
                <div
                  key={i}
                  className="card"
                  style={{ padding: '12px 16px' }}
                >
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                        <code style={{ fontSize: '0.7rem', color: 'var(--accent-teal)', fontFamily: 'var(--font-mono)' }}>
                          {rule.rule_id}
                        </code>
                        <span
                          style={{
                            fontSize: '0.6rem',
                            background: rule.approval_status === 'confirmed'
                              ? 'rgba(34,197,94,0.1)' : 'rgba(245,200,66,0.1)',
                            color: rule.approval_status === 'confirmed'
                              ? 'var(--accent-green)' : 'var(--accent-gold)',
                            border: `1px solid ${rule.approval_status === 'confirmed' ? 'rgba(34,197,94,0.3)' : 'rgba(245,200,66,0.3)'}`,
                            padding: '1px 6px',
                            borderRadius: 999,
                          }}
                        >
                          {rule.approval_status}
                        </span>
                        {rule.category && (
                          <span style={{ fontSize: '0.6rem', color: 'var(--text-muted)' }}>
                            {rule.category}
                          </span>
                        )}
                        <span style={{ fontSize: '0.6rem', color: 'var(--text-muted)', marginLeft: 'auto' }}>
                          Step {rule.trigger_timing}
                        </span>
                      </div>
                      <div style={{ fontWeight: 600, fontSize: '0.85rem', marginBottom: 2 }}>{rule.rule_name}</div>
                      {rule.calculation && (
                        <div style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>
                          {rule.calculation}
                        </div>
                      )}
                      {rule.explanation_template && (
                        <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 3 }}>
                          {rule.explanation_template}
                        </div>
                      )}
                      {rule.commissioner_note && (
                        <div style={{ marginTop: 4, fontSize: '0.7rem', color: '#fbbf24' }}>
                          ⚠️ {rule.commissioner_note}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Ambiguities */}
        {activeTab === 'ambiguities' && !loading && (
          <div>
            <div style={{ marginBottom: 12, fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              {ambiguities.length} registered ambiguities in the current rulebook
            </div>
            {ambiguities.map((amb, i) => (
              <div
                key={i}
                className="card"
                style={{
                  marginBottom: 10,
                  borderLeft: `3px solid ${amb.status === 'resolved' ? 'var(--accent-green)' : 'var(--accent-gold)'}`,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <code style={{ fontSize: '0.7rem', color: 'var(--accent-teal)', fontFamily: 'var(--font-mono)' }}>
                    {amb.id}
                  </code>
                  <span
                    style={{
                      fontSize: '0.65rem',
                      padding: '1px 8px',
                      borderRadius: 999,
                      background: amb.status === 'resolved' ? 'rgba(34,197,94,0.1)' : 'rgba(245,200,66,0.1)',
                      color: amb.status === 'resolved' ? 'var(--accent-green)' : 'var(--accent-gold)',
                      border: `1px solid ${amb.status === 'resolved' ? 'rgba(34,197,94,0.3)' : 'rgba(245,200,66,0.3)'}`,
                    }}
                  >
                    {amb.status}
                  </span>
                </div>
                <div style={{ fontWeight: 600, fontSize: '0.88rem', marginBottom: 6 }}>{amb.description}</div>
                <div style={{ fontSize: '0.78rem', color: 'var(--text-secondary)', padding: '8px 12px', background: 'var(--bg-secondary)', borderRadius: 6, marginBottom: 6 }}>
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.65rem' }}>DEFAULT INTERPRETATION: </span>
                  {amb.default_interpretation}
                </div>
                {amb.commissioner_ruling && (
                  <div style={{ fontSize: '0.78rem', color: 'var(--accent-green)', padding: '6px 10px', background: 'rgba(34,197,94,0.06)', borderRadius: 6 }}>
                    ✅ Commissioner Ruling: {amb.commissioner_ruling}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Seed */}
        {activeTab === 'seed' && (
          <div>
            <div className="card">
              <div className="card-title">🌱 Seed Demo Database</div>
              <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: 16 }}>
                This will populate the database with the demo matchup: <strong>Goldenrod Gloryboys</strong> (Elf) vs <strong>Iron Hexes</strong> (Halfling),
                complete with 40 simulation events (seed=42), attacks, perks, rulings, and the full rules catalog.
              </p>
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: 8 }}>What gets seeded:</div>
                <ul style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', paddingLeft: 20, lineHeight: 2 }}>
                  <li>Full rules catalog + perk definitions</li>
                  <li>All store items (weapons, potions, etc.)</li>
                  <li>Demo teams with full rosters (NFL players seed=42)</li>
                  <li>5 seeded attacks (valid, blocked, invalid variants)</li>
                  <li>1 commissioner ruling (Tyreek Hill +2.0)</li>
                  <li>40 deterministic simulation events</li>
                  <li>8 registered ambiguities</li>
                </ul>
              </div>
              <button
                className="btn btn-primary"
                onClick={handleSeed}
                disabled={seeding}
              >
                {seeding ? '🌱 Seeding…' : '🌱 Seed Database'}
              </button>
              {seedResult && (
                <div
                  style={{
                    marginTop: 12,
                    padding: '10px 14px',
                    background: seedResult.startsWith('✅') ? 'rgba(34,197,94,0.08)' : 'rgba(239,68,68,0.08)',
                    border: `1px solid ${seedResult.startsWith('✅') ? 'rgba(34,197,94,0.25)' : 'rgba(239,68,68,0.25)'}`,
                    borderRadius: 8,
                    fontSize: '0.82rem',
                    color: seedResult.startsWith('✅') ? '#86efac' : '#fca5a5',
                  }}
                >
                  {seedResult}
                </div>
              )}
            </div>

            {/* API Reference */}
            <div className="card" style={{ marginTop: 16 }}>
              <div className="card-title">🔌 API Reference</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {[
                  { method: 'GET', path: '/api/matchup/demo', desc: 'Full matchup state with scores, traces' },
                  { method: 'GET', path: '/api/matchup/{id}', desc: 'Matchup by ID' },
                  { method: 'POST', path: '/api/admin/seed', desc: 'Seed demo data' },
                  { method: 'GET', path: '/api/admin/matchup/{id}/audit-report', desc: 'Full audit report' },
                  { method: 'GET', path: '/api/admin/rules', desc: 'Rules catalog' },
                  { method: 'GET', path: '/api/admin/ambiguities', desc: 'Ambiguity register' },
                  { method: 'GET', path: '/api/events/matchup/{id}/timeline', desc: 'Event timeline' },
                  { method: 'GET', path: '/api/gold/team/{id}/ledger', desc: 'Team gold ledger' },
                  { method: 'GET', path: '/api/gold/matchup/{id}/summary', desc: 'Gold summary' },
                  { method: 'POST', path: '/api/simulation/{id}/control', desc: 'Control simulation (start/pause/resume/stop)' },
                  { method: 'GET', path: '/api/simulation/{id}/sse', desc: 'Live SSE event stream' },
                  { method: 'GET', path: '/api/simulation/{id}/status', desc: 'Simulation status' },
                  { method: 'GET', path: '/docs', desc: 'Interactive API docs (Swagger UI)' },
                ].map((endpoint, i) => (
                  <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '6px 0', borderBottom: '1px solid var(--border)', fontSize: '0.8rem' }}>
                    <span
                      style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: '0.65rem',
                        padding: '2px 6px',
                        borderRadius: 3,
                        background: endpoint.method === 'GET' ? 'rgba(34,197,94,0.12)' : 'rgba(245,200,66,0.12)',
                        color: endpoint.method === 'GET' ? '#86efac' : 'var(--accent-gold)',
                        minWidth: 38,
                        textAlign: 'center',
                        fontWeight: 700,
                      }}
                    >
                      {endpoint.method}
                    </span>
                    <code style={{ color: 'var(--accent-teal)', fontFamily: 'var(--font-mono)', fontSize: '0.75rem', flex: 1 }}>
                      {endpoint.path}
                    </code>
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.72rem' }}>{endpoint.desc}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default AdminPanel;
