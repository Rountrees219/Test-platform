import { useState } from 'react';
import './styles.css';
import LiveMatchup from './pages/LiveMatchup';
import ScoreExplainer from './pages/ScoreExplainer';
import Timeline from './pages/Timeline';
import AttackDashboard from './pages/AttackDashboard';
import GoldLedger from './pages/GoldLedger';
import AdminPanel from './pages/AdminPanel';

type Page = 'live' | 'explain' | 'timeline' | 'attacks' | 'gold' | 'admin';

const NAV_ITEMS: { id: Page; label: string; icon: string }[] = [
  { id: 'live', label: 'Live Matchup', icon: '🏈' },
  { id: 'explain', label: 'Score Explainer', icon: '📋' },
  { id: 'timeline', label: 'Timeline', icon: '📅' },
  { id: 'attacks', label: 'Attacks', icon: '⚔️' },
  { id: 'gold', label: 'Gold', icon: '🪙' },
  { id: 'admin', label: 'Admin', icon: '🔧' },
];

function App() {
  const [page, setPage] = useState<Page>('live');

  const renderPage = () => {
    switch (page) {
      case 'live': return <LiveMatchup />;
      case 'explain': return <ScoreExplainer />;
      case 'timeline': return <Timeline />;
      case 'attacks': return <AttackDashboard />;
      case 'gold': return <GoldLedger />;
      case 'admin': return <AdminPanel />;
      default: return <LiveMatchup />;
    }
  };

  return (
    <div className="app-container">
      <header className="app-header">
        <div className="header-inner">
          {/* Logo */}
          <div className="logo">
            <span>⚔️</span>
            <div>
              <div>FOOTBOLZANO</div>
              <div className="logo-sub">Live Scoring Demo</div>
            </div>
          </div>

          {/* Nav */}
          <nav className="nav">
            {NAV_ITEMS.map(item => (
              <button
                key={item.id}
                className={`nav-btn ${page === item.id ? 'active' : ''}`}
                onClick={() => setPage(item.id)}
              >
                <span>{item.icon}</span>
                <span>{item.label}</span>
                {item.id === 'live' && (
                  <span className="live-badge">LIVE</span>
                )}
              </button>
            ))}
          </nav>

          {/* Demo badge */}
          <div
            style={{
              fontSize: '0.65rem',
              fontWeight: 700,
              letterSpacing: '0.1em',
              color: 'var(--accent-gold)',
              background: 'rgba(245,200,66,0.1)',
              border: '1px solid rgba(245,200,66,0.25)',
              padding: '3px 8px',
              borderRadius: 4,
              whiteSpace: 'nowrap',
            }}
          >
            DEMO v1.0
          </div>
        </div>
      </header>

      <main className="main-content">
        {renderPage()}
      </main>

      <footer
        style={{
          background: 'var(--bg-secondary)',
          borderTop: '1px solid var(--border)',
          padding: '12px 24px',
          textAlign: 'center',
          fontSize: '0.65rem',
          color: 'var(--text-muted)',
        }}
      >
        Footbolzano Live Scoring Demo — Independent application.
        Rules from{' '}
        <a href="https://footbolzano.com/rules/" target="_blank" rel="noopener noreferrer">
          footbolzano.com/rules/
        </a>
        {' '}(2025 Rulebook v1) · All data simulated (seed=42) ·
        Not affiliated with footbolzano.com
      </footer>
    </div>
  );
}

export default App;
