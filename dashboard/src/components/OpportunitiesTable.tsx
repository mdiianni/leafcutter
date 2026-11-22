import { useState, useMemo } from 'react';
import { getSeverityColor, formatCurrency } from '../utils';
import type { Opportunity } from '../types';

interface OpportunitiesTableProps {
  opportunities: Opportunity[];
}

type SortCol = 'service' | 'resource' | 'type' | 'severity' | 'saving';
type SortDir = 'asc' | 'desc';

const PAGE_SIZE = 25;
const SEV_ORDER: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, DEPENDENCY: 4, INFO: 5 };

function SortIcon({ col, sortCol, sortDir }: { col: SortCol; sortCol: SortCol; sortDir: SortDir }) {
  if (col !== sortCol) return <span style={{ opacity: 0.3, marginLeft: 4 }}>⇅</span>;
  return <span style={{ marginLeft: 4 }}>{sortDir === 'asc' ? '↑' : '↓'}</span>;
}

/** Wraps matched substrings in a <mark> so they are visually highlighted. */
function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
  const parts = text.split(regex);
  return (
    <>
      {parts.map((part, i) =>
        regex.test(part)
          ? <mark key={i} className="search-highlight">{part}</mark>
          : <span key={i}>{part}</span>
      )}
    </>
  );
}

export default function OpportunitiesTable({ opportunities }: OpportunitiesTableProps) {
  const [page, setPage]         = useState(1);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [sortCol, setSortCol]   = useState<SortCol>('saving');
  const [sortDir, setSortDir]   = useState<SortDir>('desc');
  const [filterService,  setFilterService]  = useState('all');
  const [filterSeverity, setFilterSeverity] = useState('all');
  const [searchQuery, setSearchQuery]       = useState('');

  // Derive unique filter options from full list
  const services  = useMemo(() => ['all', ...Array.from(new Set(opportunities.map(o => o.service))).sort()], [opportunities]);
  const severities = useMemo(() => ['all', ...['CRITICAL','HIGH','MEDIUM','LOW','DEPENDENCY','INFO'].filter(s => opportunities.some(o => o.severity === s))], [opportunities]);

  const handleSort = (col: SortCol) => {
    if (col === sortCol) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortCol(col); setSortDir(col === 'saving' ? 'desc' : 'asc'); }
    setPage(1);
  };

  const handleSearch = (value: string) => {
    setSearchQuery(value);
    setPage(1);
  };

  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    let list = opportunities;
    if (filterService  !== 'all') list = list.filter(o => o.service  === filterService);
    if (filterSeverity !== 'all') list = list.filter(o => o.severity === filterSeverity);
    if (q) {
      list = list.filter(o =>
        o.rec.toLowerCase().includes(q)       ||
        o.resource.toLowerCase().includes(q)  ||
        o.type.toLowerCase().includes(q)      ||
        o.service.toLowerCase().includes(q)
      );
    }
    return [...list].sort((a, b) => {
      let av: string | number, bv: string | number;
      if (sortCol === 'severity') { av = SEV_ORDER[a.severity] ?? 9; bv = SEV_ORDER[b.severity] ?? 9; }
      else if (sortCol === 'saving') { av = a.saving; bv = b.saving; }
      else { av = (a[sortCol] ?? '').toLowerCase(); bv = (b[sortCol] ?? '').toLowerCase(); }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
  }, [opportunities, filterService, filterSeverity, searchQuery, sortCol, sortDir]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const slice = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const toggleExpand = (key: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setExpanded(prev => { const n = new Set(prev); n.has(key) ? n.delete(key) : n.add(key); return n; });
  };

  const thStyle: React.CSSProperties = { cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' };

  const resetFilters = () => {
    setFilterService('all');
    setFilterSeverity('all');
    setSearchQuery('');
    setPage(1);
  };

  const hasFilters = filterService !== 'all' || filterSeverity !== 'all' || searchQuery.trim() !== '';

  return (
    <div className="card">
      <div className="card-header">
        <span style={{ fontSize: 16 }}>🎯</span>
        <span className="card-title">Top Opportunities</span>

        {/* Filters + Search */}
        <div className="opp-filters">
          <select
            className="group-select"
            value={filterService}
            onChange={e => { setFilterService(e.target.value); setPage(1); }}
          >
            <option value="all">All Services</option>
            {services.filter(s => s !== 'all').map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>

          <select
            className="group-select"
            value={filterSeverity}
            onChange={e => { setFilterSeverity(e.target.value); setPage(1); }}
            style={{ minWidth: 130 }}
          >
            <option value="all">All Severities</option>
            {severities.filter(s => s !== 'all').map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>

          <div className="opp-search-wrapper">
            <span className="opp-search-icon">🔍</span>
            <input
              className="opp-search"
              type="text"
              placeholder="Search recommendations…"
              value={searchQuery}
              onChange={e => handleSearch(e.target.value)}
            />
            {searchQuery && (
              <button className="opp-search-clear" onClick={() => handleSearch('')} title="Clear search">✕</button>
            )}
          </div>

          {hasFilters && (
            <button
              onClick={resetFilters}
              style={{ background: 'none', border: '1px solid var(--border)', borderRadius: 6, padding: '4px 10px', fontSize: 12, color: 'var(--text-secondary)', cursor: 'pointer' }}
            >
              ✕ Clear all
            </button>
          )}
        </div>

        <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
          {filtered.length !== opportunities.length
            ? `${filtered.length} of ${opportunities.length}`
            : `${opportunities.length} total`}
        </span>
      </div>

      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th style={thStyle} onClick={() => handleSort('service')}>
                Service <SortIcon col="service" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th style={thStyle} onClick={() => handleSort('resource')}>
                Resource <SortIcon col="resource" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th style={thStyle} onClick={() => handleSort('type')}>
                Type <SortIcon col="type" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th style={thStyle} onClick={() => handleSort('severity')}>
                Severity <SortIcon col="severity" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th style={thStyle} onClick={() => handleSort('saving')}>
                Saving/mo <SortIcon col="saving" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th>Recommendation</th>
            </tr>
          </thead>
          <tbody>
            {slice.map((opp, i) => {
              const key = `${(page - 1) * PAGE_SIZE + i}-${opp.service}-${opp.resource}`;
              const isExp = expanded.has(key);
              const recText = isExp ? opp.rec : opp.rec.slice(0, 60) + (opp.rec.length > 60 ? '…' : '');
              return (
                <tr key={key}>
                  <td>
                    <span className="badge" style={{ background: 'var(--accent-light)', color: 'var(--accent)', textTransform: 'none', fontWeight: 600 }}>
                      <Highlight text={opp.service} query={searchQuery} />
                    </span>
                  </td>
                  <td style={{ fontFamily: 'monospace', fontSize: 12, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    <Highlight text={opp.resource} query={searchQuery} />
                  </td>
                  <td style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
                    <Highlight text={opp.type} query={searchQuery} />
                  </td>
                  <td>
                    <span className="badge" style={{ background: getSeverityColor(opp.severity) + '22', color: getSeverityColor(opp.severity) }}>
                      {opp.severity}
                    </span>
                  </td>
                  <td style={{ fontWeight: 600, color: '#22c55e' }}>
                    {formatCurrency(opp.saving)}
                    {opp.adjusted && (
                      <span
                        title={`Adjusted for SP coverage + EDP discount already allocated to this account. List-price estimate was ${formatCurrency(opp.saving_raw ?? opp.saving)}/mo.`}
                        style={{ marginLeft: 6, fontSize: 10, background: '#3b82f622', color: '#3b82f6', borderRadius: 4, padding: '1px 5px', fontWeight: 600, cursor: 'help' }}
                      >
                        SP/EDP adj.
                      </span>
                    )}
                  </td>
                  <td>
                    <div className="rec-text">
                      <Highlight text={recText} query={searchQuery} />
                      {opp.rec.length > 60 && (
                        <button className="rec-toggle" onClick={e => toggleExpand(key, e)}>
                          {isExp ? ' less' : ' more'}
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {slice.length === 0 && (
              <tr>
                <td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '24px' }}>
                  {searchQuery
                    ? `No recommendations match "${searchQuery}".`
                    : 'No opportunities match the current filters.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="pagination">
          <span style={{ marginRight: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
            {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, filtered.length)} of {filtered.length}
          </span>
          <button className="page-btn" disabled={page === 1} onClick={() => setPage(1)}>«</button>
          <button className="page-btn" disabled={page === 1} onClick={() => setPage(p => p - 1)}>‹</button>
          {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
            let p: number;
            if (totalPages <= 5) p = i + 1;
            else if (page <= 3) p = i + 1;
            else if (page >= totalPages - 2) p = totalPages - 4 + i;
            else p = page - 2 + i;
            return (
              <button key={p} className={`page-btn${page === p ? ' active' : ''}`} onClick={() => setPage(p)}>{p}</button>
            );
          })}
          <button className="page-btn" disabled={page === totalPages} onClick={() => setPage(p => p + 1)}>›</button>
          <button className="page-btn" disabled={page === totalPages} onClick={() => setPage(totalPages)}>»</button>
        </div>
      )}
    </div>
  );
}


