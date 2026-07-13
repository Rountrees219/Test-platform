import React from 'react';
import type { RuleTrace } from '../types';

interface TraceListProps {
  traces: RuleTrace[];
  maxItems?: number;
  filter?: string;
}

const RULE_ICONS: Record<string, string> = {
  'RAW_SCORE': '📊',
  'ARMY_SGT': '🎖️',
  'MILITIA': '⚔️',
  'ATK_OF_SQUIRES': '🪖',
  'ATK_VALIDATED': '✅',
  'ATK_BLOCKED': '🛡️',
  'ATK_BLOCKED_PROTECTION': '🛡️',
  'ATK_INVALID': '❌',
  'ATK_INVALID_DEAD': '💀',
  'ATK_INVALID_TARGET': '❓',
  'ATK_INVALID_ARROWS_DIGIT': '🏹',
  'ATK_INVALID_BYE': '🚫',
  'WOODEN_SWORD': '🗡️',
  'STEEL_SWORD': '⚔️',
  'ADRENALINE_POTION': '⚗️',
  'TWIN_DAGGERS': '🔱',
  'WOODEN_AXE': '🪓',
  'BRIBE': '💰',
  'BASIC_POISON': '☠️',
  'MARTYR_DOUBLE': '✝️',
  'MARTYR_DEATH': '💀',
  'SOUL_REAPER': '💀',
  'RACE_GOLD_ELF': '🧝',
  'RACE_GOLD_HUMAN': '⚔️',
  'RACE_GOLD_FAIRY': '🧚',
  'RACE_GOLD_HALFLING': '🍀',
  'RACE_GOLD_TROLL': '👹',
  'RACE_GOLD_DWARF': '⛏️',
  'RULING_SCORE_ADJ': '⚖️',
  'SCORE_FINALIZED': '🏁',
  'WINNER': '🏆',
};

function getIcon(ruleId: string): string {
  for (const [key, icon] of Object.entries(RULE_ICONS)) {
    if (ruleId.startsWith(key) || ruleId === key) return icon;
  }
  return '🔧';
}

function getDelta(trace: RuleTrace): number {
  return trace.resulting_value - trace.prior_value;
}

const TraceList: React.FC<TraceListProps> = ({ traces, maxItems = 100, filter }) => {
  const filtered = traces
    .filter(t => {
      if (!filter) return true;
      const f = filter.toLowerCase();
      return (
        t.rule_id.toLowerCase().includes(f) ||
        t.target.toLowerCase().includes(f) ||
        t.explanation.toLowerCase().includes(f) ||
        t.reason.toLowerCase().includes(f)
      );
    })
    .slice(0, maxItems);

  if (filtered.length === 0) {
    return (
      <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 32 }}>
        No rule traces to display
      </div>
    );
  }

  return (
    <div>
      {filtered.map((trace, idx) => {
        const delta = getDelta(trace);
        const hasDelta = delta !== 0 && !isNaN(delta);

        return (
          <div
            key={idx}
            className="trace-item"
            style={{
              opacity: !trace.did_fire && !trace.was_blocked && !trace.was_invalid ? 0.5 : 1,
            }}
          >
            <div className="trace-seq">#{trace.sequence}</div>
            <div className="trace-icon">{getIcon(trace.rule_id)}</div>
            <div className="trace-body">
              <div className="trace-rule">{trace.rule_id}</div>
              <div style={{ fontWeight: 600, fontSize: '0.82rem', color: 'var(--text-primary)', marginTop: 1 }}>
                {trace.target}
              </div>
              <div className="trace-expl">{trace.explanation || trace.reason}</div>
              {(trace.was_blocked || trace.was_invalid) && (
                <div style={{ marginTop: 3 }}>
                  {trace.was_blocked && (
                    <span style={{ fontSize: '0.65rem', color: '#fbbf24', background: 'rgba(251,191,36,0.1)', padding: '1px 6px', borderRadius: 3 }}>
                      🛡️ BLOCKED
                    </span>
                  )}
                  {trace.was_invalid && (
                    <span style={{ fontSize: '0.65rem', color: '#fca5a5', background: 'rgba(252,165,165,0.1)', padding: '1px 6px', borderRadius: 3 }}>
                      ❌ INVALID
                    </span>
                  )}
                </div>
              )}
              {trace.is_provisional && (
                <span style={{ fontSize: '0.6rem', color: 'var(--text-muted)' }}>⚠️ provisional</span>
              )}
            </div>
            {hasDelta && (
              <div className={`trace-delta ${delta > 0 ? 'positive' : 'negative'}`}>
                {delta > 0 ? '+' : ''}{delta.toFixed(2)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

export default TraceList;
