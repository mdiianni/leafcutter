import { useEffect, useState } from 'react';
import './App.css';
import type { DashboardData, Account, ServiceRollup } from './types';
import Header from './components/Header';
import KpiCards from './components/KpiCards';
import AccountTable from './components/AccountTable';
import ServiceBarChart from './components/ServiceBarChart';
import SeverityPieChart from './components/SeverityPieChart';
import TrendLineChart from './components/TrendLineChart';
import OpportunitiesTable from './components/OpportunitiesTable';

type Theme = 'light' | 'dark';
type Env = 'all' | 'dev' | 'stg' | 'prd';

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => {
    return (localStorage.getItem('theme') as Theme) ?? 'light';
  });

  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedGroup, setSelectedGroup] = useState('all');
  const [selectedEnv, setSelectedEnv]     = useState<Env>('all');
  const [selectedRegion, setSelectedRegion] = useState('all');
  const [selectedAccount, setSelectedAccount] = useState<string | null>(null);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  useEffect(() => {
    // In the self-contained static export, data is injected as window.__DASHBOARD_DATA__
    const inlined = (window as any).__DASHBOARD_DATA__;
    if (inlined) {
      setData(inlined as DashboardData);
      setLoading(false);
      return;
    }
    fetch('/dashboard_data.json')
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d: DashboardData) => {
        setData(d);
        setLoading(false);
      })
      .catch(e => {
        setError(e.message);
        setLoading(false);
      });
  }, []);

  const toggleTheme = () => setTheme(t => t === 'light' ? 'dark' : 'light');

  if (loading) return <div className="loading">Loading dashboard data…</div>;
  if (error || !data) return <div className="loading" style={{ color: '#ef4444' }}>Error: {error ?? 'No data'}</div>;

  // Apply group + env + region filters
  const filteredAccounts: Account[] = data.accounts.filter(a => {
    const groupOk  = selectedGroup  === 'all' || a.group  === selectedGroup;
    const envOk    = selectedEnv    === 'all' || a.env    === selectedEnv;
    const regionOk = selectedRegion === 'all' || a.region === selectedRegion;
    return groupOk && envOk && regionOk;
  });

  const activeAccounts: Account[] = selectedAccount
    ? filteredAccounts.filter(a => a.account_id === selectedAccount)
    : filteredAccounts;

  const filteredSummary = {
    total_accounts: filteredAccounts.filter(a => !a.is_payer).length,
    total_invoice_mo: filteredAccounts.filter(a => !a.is_payer).reduce((s, a) => s + a.invoice_mo, 0),
    total_saving_mo: filteredAccounts.filter(a => !a.is_payer).reduce((s, a) => s + a.total_saving_mo, 0),
    total_saving_yr: filteredAccounts.filter(a => !a.is_payer).reduce((s, a) => s + a.total_saving_yr, 0),
    saving_pct: filteredAccounts.filter(a => !a.is_payer).length > 0
      ? (filteredAccounts.filter(a => !a.is_payer).reduce((s, a) => s + a.total_saving_mo, 0) /
         filteredAccounts.filter(a => !a.is_payer).reduce((s, a) => s + a.invoice_mo, 0)) * 100
      : 0,
  };

  const serviceMap: Record<string, number> = {};
  for (const acc of activeAccounts) {
    for (const [svc, info] of Object.entries(acc.services)) {
      serviceMap[svc] = (serviceMap[svc] ?? 0) + info.saving;
    }
  }
  const filteredServiceRollup: ServiceRollup[] = Object.entries(serviceMap)
    .map(([service, saving]) => ({ service, saving }))
    .sort((a, b) => b.saving - a.saving);

  const allOpportunities = activeAccounts.flatMap(a =>
    a.top_opportunities.map(o => ({ ...o, _account: a.name }))
  ).sort((a, b) => b.saving - a.saving);

  const handleGroupChange = (g: string) => {
    setSelectedGroup(g);
    setSelectedAccount(null);
  };

  const handleEnvChange = (e: Env) => {
    setSelectedEnv(e);
    setSelectedAccount(null);
  };

  const handleRegionChange = (r: string) => {
    setSelectedRegion(r);
    setSelectedAccount(null);
  };

  return (
    <div className="app">
      <Header
        theme={theme}
        onToggleTheme={toggleTheme}
        groups={data.groups}
        selectedGroup={selectedGroup}
        onGroupChange={handleGroupChange}
        selectedEnv={selectedEnv}
        onEnvChange={handleEnvChange}
        selectedRegion={selectedRegion}
        onRegionChange={handleRegionChange}
        generatedAt={data.generated_at}
        reportVersion={data.report_version ?? ''}
      />
      <main className="main-content">
        <KpiCards summary={filteredSummary} accounts={activeAccounts} />

        <AccountTable
          accounts={filteredAccounts}
          selectedAccount={selectedAccount}
          onSelectAccount={setSelectedAccount}
        />

        <div className="charts-grid">
          <ServiceBarChart
            data={filteredServiceRollup}
            theme={theme}
          />
          <SeverityPieChart
            accounts={activeAccounts}
            theme={theme}
          />
        </div>

        <TrendLineChart data={data} filteredAccounts={activeAccounts} theme={theme} />

        <OpportunitiesTable opportunities={allOpportunities} />
      </main>
    </div>
  );
}
