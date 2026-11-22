export interface ServiceInfo {
  saving: number;
  count: number;
  ts: string;
}

export interface Opportunity {
  service: string;
  resource: string;
  type: string;
  severity: string;
  saving: number;
  saving_raw?: number;
  adjusted?: boolean;
  current: number;
  rec: string;
  _account?: string;
}

export interface TrendPoint {
  date: string;
  saving: number;
  sp_coverage_pct?: number;
}

export interface Account {
  account_id: string;
  name: string;
  env: string;
  group: string;
  region?: string;
  is_payer?: boolean;
  invoice_mo: number;
  total_saving_mo: number;
  total_saving_yr: number;
  saving_pct: number;
  services: Record<string, ServiceInfo>;
  severity_breakdown: Record<string, number>;
  top_opportunities: Opportunity[];
  trend: TrendPoint[];
  sp_coverage_pct?: number;
  sp_allocated_mo?: number;
  edp_discount_pct?: number;
}

export interface Summary {
  total_accounts: number;
  total_invoice_mo: number;
  total_saving_mo: number;
  total_saving_yr: number;
  saving_pct: number;
}

export interface ServiceRollup {
  service: string;
  saving: number;
  checks?: number;
}

export interface DashboardData {
  generated_at: string;
  report_version: string;
  summary: Summary;
  groups: string[];
  accounts: Account[];
  aggregate_trend: TrendPoint[];
  service_rollup: ServiceRollup[];
}
