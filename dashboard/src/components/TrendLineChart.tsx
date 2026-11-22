import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, ReferenceLine,
} from 'recharts';
import { formatCurrency } from '../utils';
import type { DashboardData, TrendPoint } from '../types';

interface TrendLineChartProps {
  data: DashboardData;
  theme: 'light' | 'dark';
  filteredAccounts?: DashboardData['accounts'];
}

// Build per-group trend from account-level trend arrays
function buildGroupTrend(accounts: DashboardData['accounts']): Record<string, TrendPoint[]> {
  const byGroup: Record<string, Record<string, number>> = {};
  for (const acc of accounts) {
    for (const pt of acc.trend ?? []) {
      if (!byGroup[acc.group]) byGroup[acc.group] = {};
      byGroup[acc.group][pt.date] = (byGroup[acc.group][pt.date] ?? 0) + pt.saving;
    }
  }
  const result: Record<string, TrendPoint[]> = {};
  for (const [group, dateMap] of Object.entries(byGroup)) {
    result[group] = Object.entries(dateMap)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, saving]) => ({ date, saving }));
  }
  return result;
}

const GROUP_COLORS = [
  '#22d3ee', '#f59e0b', '#10b981', '#818cf8', '#f87171',
  '#34d399', '#fb923c', '#a78bfa', '#60a5fa', '#e879f9',
];

export default function TrendLineChart({ data, theme, filteredAccounts }: TrendLineChartProps) {
  const textColor  = theme === 'dark' ? '#94a3b8' : '#64748b';
  const gridColor  = theme === 'dark' ? '#334155' : '#e2e8f0';
  const cardBg     = theme === 'dark' ? '#1e293b' : '#fff';
  const borderColor = theme === 'dark' ? '#334155' : '#e2e8f0';

  const aggTrend = data.aggregate_trend ?? [];
  const accounts = filteredAccounts ?? data.accounts;

  // If fewer than 2 snapshots, show an informational placeholder
  if (aggTrend.length < 2) {
    return (
      <div className="card">
        <div className="card-header">
          <span style={{ fontSize: 16 }}>📈</span>
          <span className="card-title">Savings Trend Over Time</span>
        </div>
        <div className="card-body" style={{
          height: 260, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 12,
          color: textColor, textAlign: 'center',
        }}>
          <span style={{ fontSize: 36 }}>📊</span>
          <span style={{ fontSize: 15, fontWeight: 600 }}>
            {aggTrend.length === 1
              ? 'First snapshot recorded ✓'
              : 'No snapshot data yet'}
          </span>
          <span style={{ fontSize: 13, maxWidth: 380, lineHeight: 1.6 }}>
            {aggTrend.length === 1
              ? `Baseline captured on ${aggTrend[0].date} — ${formatCurrency(aggTrend[0].saving)} saving identified. Re-run the analysis after implementing improvements to see the trend appear here.`
              : 'Run the analysis at least twice (on different days) to see savings trends over time. Each run records a snapshot automatically.'}
          </span>
          {aggTrend.length === 1 && (
            <div style={{
              marginTop: 8, padding: '10px 20px', borderRadius: 8,
              background: theme === 'dark' ? '#0f172a' : '#f8fafc',
              border: `1px solid ${borderColor}`, fontSize: 13,
            }}>
              <span style={{ color: '#22d3ee', fontWeight: 700 }}>
                {formatCurrency(aggTrend[0].saving)}
              </span>
              <span style={{ color: textColor }}> /month identified • </span>
              <span style={{ color: '#10b981', fontWeight: 700 }}>
                {formatCurrency(aggTrend[0].saving * 12)}
              </span>
              <span style={{ color: textColor }}> /year potential</span>
            </div>
          )}
        </div>
      </div>
    );
  }

  // Build group-level series from filtered accounts
  const groupTrend = buildGroupTrend(accounts);
  const groups = Object.keys(groupTrend);
  const showGroups = groups.length <= 8; // only draw per-group lines if manageable

  // Merge all dates across aggregate + group trends
  const allDates = Array.from(new Set([
    ...aggTrend.map(p => p.date),
    ...Object.values(groupTrend).flatMap(pts => pts.map(p => p.date)),
  ])).sort();

  // Build chart data rows
  const chartData = allDates.map(date => {
    const aggPt = aggTrend.find(p => p.date === date);
    const row: Record<string, number | string> = {
      date,
      'Total': aggPt ? aggPt.saving : 0,
    };
    if (showGroups) {
      for (const [group, pts] of Object.entries(groupTrend)) {
        const pt = pts.find(p => p.date === date);
        row[group] = pt ? pt.saving : 0;
      }
    }
    return row;
  });

  // Calculate % change from first to last
  const firstVal = aggTrend[0]?.saving ?? 0;
  const lastVal  = aggTrend[aggTrend.length - 1]?.saving ?? 0;
  const pctChange = firstVal > 0 ? ((lastVal - firstVal) / firstVal) * 100 : 0;
  const improved = lastVal < firstVal; // saving went down = we fixed things!

  return (
    <div className="card">
      <div className="card-header">
        <span style={{ fontSize: 16 }}>📈</span>
        <span className="card-title">Savings Trend Over Time</span>
        {aggTrend.length >= 2 && (
          <span style={{
            marginLeft: 'auto', fontSize: 12, fontWeight: 600, padding: '2px 10px',
            borderRadius: 12,
            background: improved ? '#10b98120' : '#f59e0b20',
            color: improved ? '#10b981' : '#f59e0b',
          }}>
            {improved ? '✅' : '📌'} {Math.abs(pctChange).toFixed(1)}%
            {improved ? ' savings realised' : ' opportunity identified'}
            {' '}({aggTrend.length} snapshots)
          </span>
        )}
      </div>
      <div className="card-body" style={{ height: 320 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 60 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 11, fill: textColor }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              tickFormatter={v => formatCurrency(v, true)}
              tick={{ fontSize: 11, fill: textColor }}
              axisLine={false}
              tickLine={false}
            />
            <Tooltip
              formatter={(value, name) => [formatCurrency(Number(value ?? 0)), String(name)]}
              contentStyle={{
                background: cardBg,
                border: `1px solid ${borderColor}`,
                borderRadius: 8,
                fontSize: 13,
                color: theme === 'dark' ? '#f1f5f9' : '#1e293b',
              }}
            />
            <Legend
              formatter={(value) => (
                <span style={{ color: textColor, fontSize: 12 }}>{value}</span>
              )}
            />
            {/* Baseline reference line */}
            <ReferenceLine
              y={firstVal}
              stroke={gridColor}
              strokeDasharray="4 4"
              label={{ value: 'Baseline', fill: textColor, fontSize: 10 }}
            />
            {/* Aggregate total line — always shown, bold */}
            <Line
              key="total"
              type="monotone"
              dataKey="Total"
              stroke="#22d3ee"
              strokeWidth={2.5}
              dot={{ r: 4, fill: '#22d3ee' }}
              activeDot={{ r: 6 }}
            />
            {/* Per-group lines when ≤8 groups */}
            {showGroups && groups.map((group, i) => (
              <Line
                key={group}
                type="monotone"
                dataKey={group}
                stroke={GROUP_COLORS[i % GROUP_COLORS.length]}
                strokeWidth={1.5}
                strokeDasharray="5 3"
                dot={{ r: 2 }}
                activeDot={{ r: 4 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
