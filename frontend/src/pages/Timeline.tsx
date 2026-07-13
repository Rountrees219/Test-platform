import React, { useEffect, useState, useCallback } from 'react';
import type { TimelineEvent } from '../types';
import { fetchTimeline, fetchDemoMatchup } from '../utils/api';

const EVENT_ICONS: Record<string, string> = {
  simulation_event: '🎮',
  score_update: '📊',
  attack_submitted: '⚔️',
  attack_validated: '✅',
  attack_blocked: '🛡️',
  attack_invalid: '❌',
  gold_earned: '🪙',
  gold_spent: '💸',
  death: '💀',
  protection: '🛡️',
  ruling: '⚖️',
  milestone: '🏆',
};

function getEventIcon(type: string): string {
  return EVENT_ICONS[type] || '📋';
}

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return ts;
  }
}

const Timeline: React.FC = () => {
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    try {
      const matchup = await fetchDemoMatchup();
      const data = await fetchTimeline(matchup.matchup_id);
      setEvents(data.events);
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

  const toggleExpand = (id: number) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allTypes = Array.from(new Set(events.map(e => e.type)));

  const filtered = events.filter(e => {
    if (typeFilter && e.type !== typeFilter) return false;
    if (filter) {
      const f = filter.toLowerCase();
      return (
        e.description.toLowerCase().includes(f) ||
        e.type.toLowerCase().includes(f) ||
        JSON.stringify(e.data).toLowerCase().includes(f)
      );
    }
    return true;
  });

  if (loading) {
    return (
      <div className="loading-spinner">
        <div className="spinner" />
        <span>Loading timeline…</span>
      </div>
    );
  }

  if (error) {
    return <div className="error-box">{error}</div>;
  }

  return (
    <div>
      <div className="section-header">
        <div>
          <div className="section-title">📅 Event Timeline & Ledger</div>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: 4 }}>
            Chronological log of all matchup events — {events.length} total events
          </div>
        </div>
        <button className="btn btn-secondary" onClick={load} style={{ fontSize: '0.75rem' }}>↻ Refresh</button>
      </div>

      {/* Filters */}
      <div style={{ padding: '0 24px 16px', display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <input
          type="text"
          placeholder="🔍 Search events…"
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
          value={typeFilter}
          onChange={e => setTypeFilter(e.target.value)}
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            padding: '6px 12px',
            color: 'var(--text-primary)',
            fontSize: '0.8rem',
          }}
        >
          <option value="">All types</option>
          {allTypes.map(t => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
          {filtered.length} events
        </span>
      </div>

      {/* Timeline */}
      <div style={{ padding: '0 24px', maxWidth: 1200, margin: '0 auto' }}>
        <div style={{ position: 'relative' }}>
          {/* Vertical line */}
          <div style={{
            position: 'absolute',
            left: 17,
            top: 0,
            bottom: 0,
            width: 2,
            background: 'var(--border)',
            borderRadius: 1,
          }} />

          {filtered.map((event) => {
            const isExpanded = expanded.has(event.id);
            const hasData = event.data && Object.keys(event.data).length > 0;
            const dataStr = hasData ? JSON.stringify(event.data, null, 2) : '';

            return (
              <div
                key={event.id}
                style={{
                  display: 'flex',
                  gap: 16,
                  marginBottom: 12,
                  position: 'relative',
                }}
              >
                {/* Icon bubble */}
                <div
                  style={{
                    width: 36,
                    height: 36,
                    background: 'var(--bg-card)',
                    border: '2px solid var(--border)',
                    borderRadius: '50%',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: '0.9rem',
                    flexShrink: 0,
                    zIndex: 1,
                  }}
                >
                  {getEventIcon(event.type)}
                </div>

                {/* Content */}
                <div
                  style={{
                    flex: 1,
                    background: 'var(--bg-card)',
                    border: '1px solid var(--border)',
                    borderRadius: 10,
                    padding: '10px 14px',
                    cursor: hasData ? 'pointer' : 'default',
                  }}
                  onClick={() => hasData && toggleExpand(event.id)}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                    <span
                      style={{
                        fontSize: '0.65rem',
                        background: 'var(--bg-secondary)',
                        border: '1px solid var(--border)',
                        padding: '1px 7px',
                        borderRadius: 999,
                        color: 'var(--text-muted)',
                      }}
                    >
                      {event.type}
                    </span>
                    <span style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginLeft: 'auto' }}>
                      #{event.sequence} · {formatTime(event.timestamp)}
                    </span>
                  </div>
                  <div style={{ fontWeight: 600, fontSize: '0.85rem' }}>{event.description}</div>

                  {/* Score / gold deltas */}
                  <div style={{ display: 'flex', gap: 12, marginTop: 4 }}>
                    {event.score_delta !== undefined && event.score_delta !== null && event.score_delta !== 0 && (
                      <span style={{
                        fontSize: '0.72rem',
                        color: event.score_delta > 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                      }}>
                        {event.score_delta > 0 ? '+' : ''}{event.score_delta.toFixed(2)} pts
                      </span>
                    )}
                    {event.gold_delta !== undefined && event.gold_delta !== null && event.gold_delta !== 0 && (
                      <span style={{
                        fontSize: '0.72rem',
                        color: event.gold_delta > 0 ? 'var(--accent-gold)' : '#9ca3af',
                      }}>
                        {event.gold_delta > 0 ? '+' : ''}{event.gold_delta} 🪙
                      </span>
                    )}
                  </div>

                  {/* Expanded data */}
                  {isExpanded && hasData && (
                    <pre
                      style={{
                        marginTop: 8,
                        background: 'var(--bg-secondary)',
                        borderRadius: 6,
                        padding: '8px 10px',
                        fontSize: '0.68rem',
                        fontFamily: 'var(--font-mono)',
                        color: 'var(--text-secondary)',
                        overflowX: 'auto',
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                      }}
                    >
                      {dataStr}
                    </pre>
                  )}
                  {hasData && (
                    <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 4 }}>
                      {isExpanded ? '▲ collapse' : '▼ expand data'}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {filtered.length === 0 && (
          <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 48 }}>
            No events match your filters
          </div>
        )}
      </div>
    </div>
  );
};

export default Timeline;
