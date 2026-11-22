import { useEffect, useRef, useState } from 'react';
import { formatCurrency, formatPct, getSeverityColor } from '../utils';
import type { Account, Summary } from '../types';

function useAnimatedCount(target: number, duration = 800): number {
  const [value, setValue] = useState(0);
  const raf = useRef<number>(0);
  const start = useRef<number | null>(null);

  useEffect(() => {
    start.current = null;
    const from = 0;

    const tick = (ts: number) => {
      if (!start.current) start.current = ts;
      const elapsed = ts - start.current;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setValue(from + (target - from) * eased);
      if (progress < 1) {
        raf.current = requestAnimationFrame(tick);
      }
    };

    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, [target, duration]);

  return value;
}

interface KpiCardsProps {
  summary: Summary;
  accounts: Account[];
}

export default function KpiCards({ summary, accounts }: KpiCardsProps) {
  const animSavingMo = useAnimatedCount(summary.total_saving_mo);
  const animSavingYr = useAnimatedCount(summary.total_saving_yr);
  const animSpend = useAnimatedCount(summary.total_invoice_mo);
  const animPct = useAnimatedCount(summary.saving_pct);

  const criticalOpps = accounts.flatMap(a => a.top_opportunities.filter(o => o.severity === 'CRITICAL'));
  const criticalSaving = criticalOpps.reduce((s, o) => s + o.saving, 0);
  const animCritical = useAnimatedCount(criticalSaving);

  return (
    <div className="kpi-grid">
      <div className="kpi-card" style={{ '--kpi-color': '#3b82f6' } as React.CSSProperties}>
        <span className="kpi-icon">💰</span>
        <div className="kpi-label">Total Potential Saving</div>
        <div className="kpi-value">{formatCurrency(animSavingMo)}<span style={{ fontSize: 16, fontWeight: 400, color: 'var(--text-secondary)' }}>/mo</span></div>
        <div className="kpi-sub">{formatCurrency(animSavingYr)}/yr</div>
      </div>

      <div className="kpi-card" style={{ '--kpi-color': '#8b5cf6' } as React.CSSProperties}>
        <span className="kpi-icon">🏦</span>
        <div className="kpi-label">Total Monthly Spend</div>
        <div className="kpi-value">{formatCurrency(animSpend)}</div>
        <div className="kpi-sub">{accounts.length} accounts</div>
      </div>

      <div className="kpi-card" style={{ '--kpi-color': '#14b8a6' } as React.CSSProperties}>
        <span className="kpi-icon">📊</span>
        <div className="kpi-label">Saving % of Spend</div>
        <div className="kpi-value">{formatPct(animPct)}</div>
        <div className="kpi-sub">of total spend</div>
      </div>

      <div className="kpi-card" style={{ '--kpi-color': getSeverityColor('CRITICAL') } as React.CSSProperties}>
        <span className="kpi-icon">🔴</span>
        <div className="kpi-label">Critical Opportunities</div>
        <div className="kpi-value">{criticalOpps.length}</div>
        <div className="kpi-sub">{formatCurrency(animCritical)}/mo at risk</div>
      </div>
    </div>
  );
}
