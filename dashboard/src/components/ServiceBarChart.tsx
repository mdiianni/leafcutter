import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from 'recharts';
import { formatCurrency, SERVICE_COLORS } from '../utils';
import type { ServiceRollup } from '../types';

interface ServiceBarChartProps {
  data: ServiceRollup[];
  theme: 'light' | 'dark';
}

export default function ServiceBarChart({ data, theme }: ServiceBarChartProps) {
  const sorted = [...data].sort((a, b) => b.saving - a.saving).slice(0, 12);
  const textColor = theme === 'dark' ? '#94a3b8' : '#64748b';
  const gridColor = theme === 'dark' ? '#334155' : '#e2e8f0';

  return (
    <div className="card" style={{ height: '100%' }}>
      <div className="card-header">
        <span style={{ fontSize: 16 }}>📊</span>
        <span className="card-title">Savings by Service</span>
      </div>
      <div className="card-body" style={{ height: 320 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={sorted} layout="vertical" margin={{ top: 0, right: 40, bottom: 0, left: 60 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={gridColor} horizontal={false} />
            <XAxis
              type="number"
              tickFormatter={v => formatCurrency(v, true)}
              tick={{ fontSize: 11, fill: textColor }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              type="category"
              dataKey="service"
              tick={{ fontSize: 12, fill: textColor }}
              axisLine={false}
              tickLine={false}
              width={55}
            />
            <Tooltip
              formatter={(value) => [formatCurrency(Number(value ?? 0)), 'Saving/mo']}
              contentStyle={{
                background: theme === 'dark' ? '#1e293b' : '#fff',
                border: `1px solid ${gridColor}`,
                borderRadius: 8,
                fontSize: 13,
                color: theme === 'dark' ? '#f1f5f9' : '#1e293b',
              }}
            />
            <Bar dataKey="saving" radius={[0, 4, 4, 0]} maxBarSize={24}>
              {sorted.map((_, i) => (
                <Cell key={i} fill={SERVICE_COLORS[i % SERVICE_COLORS.length]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
