import { useState } from 'react';
import { formatCurrency, formatPct, getSeverityColor } from '../utils';
import type { Account } from '../types';

interface AccountTableProps {
  accounts: Account[];
  selectedAccount: string | null;
  onSelectAccount: (id: string | null) => void;
}

type SortCol = 'name' | 'invoice_mo' | 'total_saving_mo' | 'saving_pct' | 'sp_coverage_pct';
type SortDir = 'asc' | 'desc';

const PAGE_SIZE = 10;

function SortIcon({ col, sortCol, sortDir }: { col: SortCol; sortCol: SortCol; sortDir: SortDir }) {
  if (col !== sortCol) return <span style={{ opacity: 0.3, marginLeft: 4 }}>⇅</span>;
  return <span style={{ marginLeft: 4 }}>{sortDir === 'asc' ? '↑' : '↓'}</span>;
}

export default function AccountTable({ accounts, selectedAccount, onSelectAccount }: AccountTableProps) {
  const [sortCol, setSortCol]   = useState<SortCol>('total_saving_mo');
  const [sortDir, setSortDir]   = useState<SortDir>('desc');
  const [page, setPage]         = useState(1);

  const handleSort = (col: SortCol) => {
    if (col === sortCol) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortCol(col);
      setSortDir('desc');
    }
    setPage(1);
  };

  const sorted = [...accounts].sort((a, b) => {
    let av: string | number = a[sortCol] ?? 0;
    let bv: string | number = b[sortCol] ?? 0;
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    if (av < bv) return sortDir === 'asc' ? -1 : 1;
    if (av > bv) return sortDir === 'asc' ? 1 : -1;
    return 0;
  });

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const slice = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const envClass = (env: string) => {
    if (env === 'dev') return 'env-dev';
    if (env === 'stg') return 'env-stg';
    if (env === 'prd') return 'env-prd';
    return '';
  };

  const thStyle: React.CSSProperties = { cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' };

  return (
    <div className="card">
      <div className="card-header">
        <span style={{ fontSize: 16 }}>📋</span>
        <span className="card-title">Account Summary</span>
        <span style={{ marginLeft: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
          {accounts.length} account{accounts.length !== 1 ? 's' : ''}
        </span>
        {selectedAccount && (
          <button
            onClick={() => onSelectAccount(null)}
            style={{ marginLeft: 'auto', background: 'none', border: '1px solid var(--border)', borderRadius: 6, padding: '2px 10px', fontSize: 12, color: 'var(--text-secondary)', cursor: 'pointer' }}
          >
            Clear filter
          </button>
        )}
      </div>
      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th style={thStyle} onClick={() => handleSort('name')}>
                Account <SortIcon col="name" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th>Group</th>
              <th>Env</th>
              <th style={thStyle} onClick={() => handleSort('invoice_mo')}>
                Invoice/mo <SortIcon col="invoice_mo" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th style={thStyle} onClick={() => handleSort('total_saving_mo')}>
                Saving/mo <SortIcon col="total_saving_mo" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th style={thStyle} onClick={() => handleSort('saving_pct')}>
                Saving % <SortIcon col="saving_pct" sortCol={sortCol} sortDir={sortDir} />
              </th>
              <th
                style={thStyle}
                onClick={() => handleSort('sp_coverage_pct')}
                title="Compute Savings Plan coverage AWS has already allocated to this account (from its own Cost Explorer)"
              >
                SP Coverage <SortIcon col="sp_coverage_pct" sortCol={sortCol} sortDir={sortDir} />
              </th>
            </tr>
          </thead>
          <tbody>
            {slice.map(acc => (
              <tr
                key={acc.account_id}
                className={selectedAccount === acc.account_id ? 'selected' : ''}
                onClick={() => onSelectAccount(selectedAccount === acc.account_id ? null : acc.account_id)}
              >
                <td>
                  <div style={{ fontWeight: 600 }}>
                    {acc.name}
                    {acc.is_payer && (
                      <span title="Payer/management account — invoice excluded from totals to avoid double-counting" style={{ marginLeft: 6, fontSize: 10, background: '#7c3aed', color: '#fff', borderRadius: 4, padding: '1px 5px', verticalAlign: 'middle', fontWeight: 500 }}>PAYER</span>
                    )}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{acc.account_id}</div>
                </td>
                <td style={{ color: 'var(--text-secondary)' }}>{acc.group}</td>
                <td>
                  <span className={`badge ${envClass(acc.env)}`}>{acc.env}</span>
                </td>
                <td style={acc.is_payer ? { color: 'var(--text-secondary)', fontStyle: 'italic' } : {}}>
                  {formatCurrency(acc.invoice_mo)}
                  {acc.is_payer && <span title="Excluded from totals (payer account)" style={{ marginLeft: 4, fontSize: 10, color: '#7c3aed' }}>⊘</span>}
                </td>
                <td style={{ color: '#22c55e', fontWeight: 600 }}>{formatCurrency(acc.total_saving_mo)}</td>
                <td>
                  <div className="progress-bar-wrap">
                    <div className="progress-bar-bg">
                      <div
                        className="progress-bar-fill"
                        style={{
                          width: `${Math.min(acc.saving_pct, 100)}%`,
                          background: acc.saving_pct > 40 ? getSeverityColor('CRITICAL') : acc.saving_pct > 20 ? '#f97316' : '#22c55e',
                        }}
                      />
                    </div>
                    <span style={{ fontSize: 12, color: 'var(--text-secondary)', minWidth: 36, textAlign: 'right' }}>
                      {formatPct(acc.saving_pct)}
                    </span>
                  </div>
                </td>
                <td>
                  {(acc.sp_coverage_pct ?? 0) > 0 ? (
                    <div title={`${formatCurrency(acc.sp_allocated_mo ?? 0)}/mo already covered by allocated Compute Savings Plan`}>
                      <span style={{ fontWeight: 600 }}>{formatPct(acc.sp_coverage_pct ?? 0)}</span>
                      <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{formatCurrency(acc.sp_allocated_mo ?? 0)}/mo</div>
                    </div>
                  ) : (
                    <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div className="pagination">
          <span style={{ marginRight: 8, fontSize: 12, color: 'var(--text-secondary)' }}>
            {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, sorted.length)} of {sorted.length}
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
