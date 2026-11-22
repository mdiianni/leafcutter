import { Sun, Moon } from 'lucide-react';
// Imported (not referenced via /public) and force-inlined as a base64 data URI so
// the logo survives vite-plugin-singlefile's HTML export (standalone file, no
// server, works from file://) as well as the normal dev/preview server.
import logoUrl from '../assets/logo.png?inline';

const ENVS = ['dev', 'stg', 'prd'] as const;
type Env = typeof ENVS[number] | 'all';

// All AWS regions for the region filter dropdown
const AWS_REGIONS = [
  'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
  'ca-central-1', 'ca-west-1',
  'eu-central-1', 'eu-central-2', 'eu-west-1', 'eu-west-2', 'eu-west-3',
  'eu-north-1', 'eu-south-1', 'eu-south-2',
  'ap-northeast-1', 'ap-northeast-2', 'ap-northeast-3',
  'ap-southeast-1', 'ap-southeast-2', 'ap-southeast-3', 'ap-southeast-4',
  'ap-south-1', 'ap-south-2', 'ap-east-1',
  'me-south-1', 'me-central-1',
  'af-south-1', 'il-central-1', 'sa-east-1',
] as const;

interface HeaderProps {
  theme: 'light' | 'dark';
  onToggleTheme: () => void;
  groups: string[];
  selectedGroup: string;
  onGroupChange: (g: string) => void;
  selectedEnv: Env;
  onEnvChange: (e: Env) => void;
  selectedRegion: string;
  onRegionChange: (r: string) => void;
  generatedAt: string;
  reportVersion: string;
}

export default function Header({
  theme, onToggleTheme,
  groups, selectedGroup, onGroupChange,
  selectedEnv, onEnvChange,
  selectedRegion, onRegionChange,
  generatedAt,
  reportVersion,
}: HeaderProps) {
  const date = generatedAt ? new Date(generatedAt).toLocaleString() : '';

  return (
    <header className="header">
      <div className="header-logo">
        <img className="header-logo-icon" src={logoUrl} alt="leafcutter logo" />
        <span className="header-title">leafcutter</span>
      </div>
      <div className="header-spacer" />
      {reportVersion && (
        <span className="header-version" title="Report version">v{reportVersion}</span>
      )}
      <span className="header-meta">Last updated: {date}</span>

      {/* Group filter */}
      <select
        className="group-select"
        value={selectedGroup}
        onChange={e => onGroupChange(e.target.value)}
      >
        <option value="all">All Groups</option>
        {groups.map(g => (
          <option key={g} value={g}>{g}</option>
        ))}
      </select>

      {/* Region filter */}
      <select
        className="group-select"
        value={selectedRegion}
        onChange={e => onRegionChange(e.target.value)}
        title="Filter by AWS region"
      >
        <option value="all">All Regions</option>
        {AWS_REGIONS.map(r => (
          <option key={r} value={r}>{r}</option>
        ))}
      </select>

      {/* Env filter pills */}
      <div className="env-pills">
        {(['all', ...ENVS] as Env[]).map(e => (
          <button
            key={e}
            className={`env-pill${selectedEnv === e ? ' active' : ''}${e !== 'all' ? ` env-pill-${e}` : ''}`}
            onClick={() => onEnvChange(e)}
          >
            {e === 'all' ? 'All Envs' : e.toUpperCase()}
          </button>
        ))}
      </div>

      <button className="theme-btn" onClick={onToggleTheme} title="Toggle theme">
        {theme === 'light' ? <Moon size={16} /> : <Sun size={16} />}
      </button>
    </header>
  );
}
