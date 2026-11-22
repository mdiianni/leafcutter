export function formatCurrency(value: number, compact = false): string {
  if (compact && value >= 1000) {
    return `$${(value / 1000).toFixed(1)}k`;
  }
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

export function formatPct(value: number): string {
  return `${value.toFixed(1)}%`;
}

export function getSeverityColor(severity: string): string {
  const colors: Record<string, string> = {
    CRITICAL: '#ef4444',
    HIGH: '#f97316',
    MEDIUM: '#eab308',
    LOW: '#22c55e',
    INFO: '#94a3b8',
  };
  return colors[severity] ?? '#94a3b8';
}

export const SERVICE_COLORS = [
  '#3b82f6', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316',
  '#22c55e', '#eab308', '#06b6d4', '#6366f1', '#84cc16',
  '#f43f5e', '#a855f7',
];
