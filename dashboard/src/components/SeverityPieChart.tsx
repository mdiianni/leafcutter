import { PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { getSeverityColor, formatCurrency } from '../utils';
import type { Account } from '../types';

interface SeverityPieChartProps {
  accounts: Account[];
  theme: 'light' | 'dark';
}

const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'];

export default function SeverityPieChart({ accounts, theme }: SeverityPieChartProps) {
  const aggregated: Record<string, number> = {};
  for (const sev of SEVERITIES) aggregated[sev] = 0;

  for (const acc of accounts) {
    for (const [sev, val] of Object.entries(acc.severity_breakdown)) {
      if (aggregated[sev] !== undefined) aggregated[sev] += val;
      else aggregated[sev] = val;
    }
  }

  const data = Object.entries(aggregated)
    .filter(([, v]) => v > 0)
    .map(([name, value]) => ({ name, value }));

  const gridColor = theme === 'dark' ? '#334155' : '#e2e8f0';

  return (
    <div className="card" style={{ height: '100%' }}>
      <div className="card-header">
        <span style={{ fontSize: 16 }}>🥧</span>
        <span className="card-title">Severity Breakdown</span>
      </div>
      <div className="card-body" style={{ height: 320 }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={data}
              cx="50%"
              cy="45%"
              innerRadius={60}
              outerRadius={100}
              paddingAngle={2}
              dataKey="value"
            >
              {data.map((entry) => (
                <Cell key={entry.name} fill={getSeverityColor(entry.name)} />
              ))}
            </Pie>
            <Tooltip
              formatter={(value, name) => [formatCurrency(Number(value ?? 0)), String(name)]}
              contentStyle={{
                background: theme === 'dark' ? '#1e293b' : '#fff',
                border: `1px solid ${gridColor}`,
                borderRadius: 8,
                fontSize: 13,
                color: theme === 'dark' ? '#f1f5f9' : '#1e293b',
              }}
            />
            <Legend
              formatter={(value) => (
                <span style={{ color: theme === 'dark' ? '#94a3b8' : '#64748b', fontSize: 12 }}>{value}</span>
              )}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
