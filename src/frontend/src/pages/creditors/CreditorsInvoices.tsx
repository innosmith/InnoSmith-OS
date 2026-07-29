import { useState, useEffect, useMemo, useCallback } from 'react';
import { Search, X, FileText, ChevronLeft, ChevronRight, ExternalLink } from 'lucide-react';
import type { CreditorsFilter, StyleCtx, InvoiceRow } from './creditors-types';
import { formatCHF, buildFilterParams, Skeleton } from './creditors-helpers';
import { api } from '../../api/client';

interface Props {
  filter: CreditorsFilter;
  styleCtx: StyleCtx;
  categories: string[];
  years: number[];
}

type SortKey = 'date' | 'vendor' | 'amount_chf' | 'category' | 'country';
type SortDir = 'asc' | 'desc';

const PAGE_SIZE = 25;

function normalize(raw: Record<string, unknown>): InvoiceRow {
  return {
    ...raw,
    index: (raw.index ?? raw.invoice_id) as number | undefined,
    invoice_id: (raw.invoice_id ?? raw.index) as number | undefined,
    vendor: (raw.vendor ?? raw.Kreditor ?? '–') as string,
    date: (raw.date ?? raw.Rechnungsdatum ?? '') as string,
    amount_chf: (raw.amount_chf ?? raw.Betrag_CHF ?? raw.Betrag ?? 0) as number,
    amount: (raw.amount ?? raw.Betrag ?? raw.Betrag_CHF) as number | undefined,
    currency: (raw.currency ?? raw.Währung ?? 'CHF') as string,
    category: (raw.category ?? raw.Kategorie ?? '') as string,
    product: (raw.product ?? raw['Produkt/Dienstleistung'] ?? raw.Produkt ?? '') as string,
    filename: (raw.filename ?? raw.Dateiname ?? '') as string,
  };
}

function confidenceDisplay(score: unknown): { color: string; label: string } {
  const n = typeof score === 'number' ? score : -1;
  if (n >= 80) return { color: 'bg-green-500', label: `${n}%` };
  if (n >= 50) return { color: 'bg-amber-500', label: `${n}%` };
  if (n >= 0) return { color: 'bg-red-500', label: `${n}%` };
  return { color: 'bg-gray-300 dark:bg-gray-600', label: '–' };
}

export function CreditorsInvoices({ filter, styleCtx, categories, years }: Props) {
  const [invoices, setInvoices] = useState<InvoiceRow[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedCategory, setSelectedCategory] = useState('');
  const [selectedYear, setSelectedYear] = useState<number | ''>('');
  const [loading, setLoading] = useState(false);
  const [selectedInvoice, setSelectedInvoice] = useState<Record<string, unknown> | null>(null);
  const [invoiceDetailLoading, setInvoiceDetailLoading] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>('date');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [page, setPage] = useState(0);

  const { cardClass, textPrimary, textSecondary, textMuted, hasBg } = styleCtx;

  const loadFiltered = useCallback(async () => {
    setLoading(true);
    try {
      const p = buildFilterParams(filter);
      if (selectedCategory) p.set('categories', selectedCategory);
      p.set('limit', '200');
      const data = await api.get<Record<string, unknown>[]>(`/api/creditors/invoices/filtered?${p}`);
      setInvoices((data ?? []).map(normalize));
      setPage(0);
    } catch { setInvoices([]); }
    setLoading(false);
  }, [filter, selectedCategory]);

  useEffect(() => { loadFiltered(); }, [loadFiltered]);

  const handleSearch = async () => {
    if (!searchQuery.trim()) { loadFiltered(); return; }
    setLoading(true);
    try {
      const p = new URLSearchParams({ query: searchQuery, limit: '100' });
      if (selectedYear) p.set('year', String(selectedYear));
      const data = await api.get<Record<string, unknown>[]>(`/api/creditors/invoices?${p}`);
      setInvoices((data ?? []).map(normalize));
      setPage(0);
    } catch { setInvoices([]); }
    setLoading(false);
  };

  const openDetail = async (inv: InvoiceRow) => {
    const id = inv.invoice_id ?? inv.index;
    if (id == null) return;
    setInvoiceDetailLoading(true);
    try {
      const detail = await api.get<Record<string, unknown>>(`/api/creditors/invoice/${id}`);
      setSelectedInvoice(detail);
    } catch { setSelectedInvoice(inv as Record<string, unknown>); }
    setInvoiceDetailLoading(false);
  };

  const sorted = useMemo(() => {
    const arr = [...invoices];
    arr.sort((a, b) => {
      const va = a[sortKey] ?? '';
      const vb = b[sortKey] ?? '';
      const cmp = typeof va === 'number' && typeof vb === 'number'
        ? va - vb : String(va).localeCompare(String(vb), 'de-CH');
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return arr;
  }, [invoices, sortKey, sortDir]);

  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);
  const paged = sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const kpis = useMemo(() => {
    if (!invoices.length) return { avg: 0, max: 0, vendors: 0 };
    const amounts = invoices.map(i => i.amount_chf ?? 0);
    const vendors = new Set(invoices.map(i => i.vendor));
    return {
      avg: amounts.reduce((s, v) => s + v, 0) / amounts.length,
      max: Math.max(...amounts),
      vendors: vendors.size,
    };
  }, [invoices]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('asc'); }
  };

  const sortIcon = (key: SortKey) =>
    sortKey === key ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '';

  const inputCls =
    'rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 ' +
    'px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-indigo-500/40';

  const panelBg = hasBg ? 'bg-white/80 dark:bg-gray-900/80 backdrop-blur-sm' : '';

  return (
    <div className="flex flex-col gap-5">
      {/* KPI Strip */}
      <div className="grid grid-cols-3 gap-4">
        {[
          { label: 'Ø Rechnung', value: formatCHF(kpis.avg) },
          { label: 'Max. Rechnung', value: formatCHF(kpis.max) },
          { label: 'Anbieter', value: String(kpis.vendors) },
        ].map(k => (
          <div key={k.label} className={`${cardClass} ${panelBg} rounded-xl p-4 shadow-sm`}>
            <p className={`text-xs font-medium ${textMuted}`}>{k.label}</p>
            <p className={`text-lg font-bold ${textPrimary}`}>{k.value}</p>
          </div>
        ))}
      </div>

      {/* Filter bar */}
      <div className={`${cardClass} ${panelBg} flex flex-wrap items-end gap-3 rounded-xl p-4 shadow-sm`}>
        <div className="flex-1 min-w-[180px]">
          <label className={`block text-xs mb-1 ${textMuted}`}>Suchbegriff</label>
          <div className="relative">
            <Search className={`absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 ${textMuted}`} />
            <input
              type="text"
              placeholder="Kreditor, Produkt…"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              className={`${inputCls} w-full pl-8`}
            />
          </div>
        </div>
        <div>
          <label className={`block text-xs mb-1 ${textMuted}`}>Kategorie</label>
          <select
            value={selectedCategory}
            onChange={e => setSelectedCategory(e.target.value)}
            className={`${inputCls} min-w-[140px]`}
          >
            <option value="">Alle</option>
            {categories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div>
          <label className={`block text-xs mb-1 ${textMuted}`}>Jahr</label>
          <select
            value={selectedYear}
            onChange={e => setSelectedYear(e.target.value ? +e.target.value : '')}
            className={`${inputCls} min-w-[90px]`}
          >
            <option value="">Alle</option>
            {years.map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </div>
        <button
          onClick={handleSearch}
          className="rounded-lg bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 transition-colors"
        >
          Suchen
        </button>
      </div>

      {/* Desktop Table */}
      <div className={`${cardClass} ${panelBg} overflow-x-auto rounded-xl shadow-sm hidden md:block`}>
        {loading ? (
          <div className="flex flex-col gap-2 p-4">
            {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}
          </div>
        ) : (
          <>
            <table className="w-full text-sm">
              <thead>
                <tr className={`border-b border-gray-200 dark:border-gray-700 text-left ${textSecondary}`}>
                  {([
                    ['date', 'Datum'], ['vendor', 'Kreditor'], ['country', 'Land'],
                    ['category', 'Kategorie'], ['amount_chf', 'Betrag (CHF)'],
                  ] as [SortKey, string][]).map(([k, l]) => (
                    <th
                      key={k}
                      onClick={() => toggleSort(k)}
                      className="px-4 py-3 font-semibold cursor-pointer select-none whitespace-nowrap text-xs"
                    >
                      {l}{sortIcon(k)}
                    </th>
                  ))}
                  {['Währung', 'Produkt/DL', 'Conf.'].map(h => (
                    <th key={h} className="px-4 py-3 font-semibold whitespace-nowrap text-xs">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {paged.map((inv, i) => {
                  const conf = confidenceDisplay((inv as Record<string, unknown>).Confidence_Score);
                  return (
                    <tr
                      key={inv.invoice_id ?? i}
                      onClick={() => openDetail(inv)}
                      className={`border-b border-gray-100 dark:border-gray-800 cursor-pointer transition-colors
                        hover:bg-indigo-50/60 dark:hover:bg-indigo-900/20
                        ${i % 2 === 1 ? 'bg-gray-50/50 dark:bg-gray-800/20' : ''}`}
                    >
                      <td className={`px-4 py-3 whitespace-nowrap ${textPrimary}`}>{inv.date || '–'}</td>
                      <td className={`px-4 py-3 font-medium ${textPrimary}`}>{inv.vendor}</td>
                      <td className={`px-4 py-3 ${textSecondary}`}>{(inv as Record<string, unknown>).Land as string ?? (inv as Record<string, unknown>).country as string ?? '–'}</td>
                      <td className={`px-4 py-3 ${textSecondary}`}>{inv.category || '–'}</td>
                      <td className={`px-4 py-3 font-medium tabular-nums ${textPrimary}`}>{formatCHF(inv.amount_chf)}</td>
                      <td className={`px-4 py-3 ${textSecondary}`}>{inv.currency}</td>
                      <td className={`px-4 py-3 ${textSecondary} max-w-[180px] truncate`}>{inv.product || '–'}</td>
                      <td className="px-4 py-3">
                        <span className="flex items-center gap-1.5">
                          <span className={`inline-block h-2 w-2 rounded-full ${conf.color}`} />
                          <span className={`text-xs tabular-nums ${textMuted}`}>{conf.label}</span>
                        </span>
                      </td>
                    </tr>
                  );
                })}
                {!paged.length && (
                  <tr><td colSpan={8} className={`px-4 py-12 text-center ${textMuted}`}>Keine Rechnungen gefunden</td></tr>
                )}
              </tbody>
            </table>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className={`flex items-center justify-between px-4 py-3 border-t border-gray-100 dark:border-gray-800 ${textSecondary}`}>
                <span className="text-xs">
                  {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, sorted.length)} von {sorted.length}
                </span>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setPage(p => Math.max(0, p - 1))}
                    disabled={page === 0}
                    className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-30 transition-colors"
                  >
                    <ChevronLeft className="h-4 w-4" />
                  </button>
                  <span className={`text-xs tabular-nums px-2 ${textMuted}`}>
                    {page + 1} / {totalPages}
                  </span>
                  <button
                    onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
                    disabled={page >= totalPages - 1}
                    className="p-1.5 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-30 transition-colors"
                  >
                    <ChevronRight className="h-4 w-4" />
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Mobile Card View */}
      <div className="md:hidden flex flex-col gap-3">
        {loading ? (
          Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-28 w-full rounded-xl" />)
        ) : paged.length === 0 ? (
          <p className={`text-center py-8 ${textMuted}`}>Keine Rechnungen gefunden</p>
        ) : (
          <>
            {paged.map((inv, i) => {
              const conf = confidenceDisplay((inv as Record<string, unknown>).Confidence_Score);
              return (
                <div
                  key={inv.invoice_id ?? i}
                  onClick={() => openDetail(inv)}
                  className={`${cardClass} rounded-xl p-4 cursor-pointer active:scale-[0.98] transition-transform`}
                >
                  <div className="flex items-start justify-between mb-2">
                    <div>
                      <p className={`text-sm font-semibold ${textPrimary}`}>{inv.vendor}</p>
                      <p className={`text-xs ${textMuted}`}>{inv.date || '–'}</p>
                    </div>
                    <p className={`text-base font-bold tabular-nums ${textPrimary}`}>{formatCHF(inv.amount_chf)}</p>
                  </div>
                  <div className="flex items-center gap-2 flex-wrap">
                    {inv.category && (
                      <span className="rounded-full bg-indigo-100 dark:bg-indigo-900/40 px-2 py-0.5 text-[10px] font-medium text-indigo-700 dark:text-indigo-300">
                        {inv.category}
                      </span>
                    )}
                    <span className={`text-[10px] ${textMuted}`}>{inv.currency}</span>
                    {inv.product && <span className={`text-[10px] truncate max-w-[120px] ${textMuted}`}>{inv.product}</span>}
                    <span className="ml-auto flex items-center gap-1">
                      <span className={`h-1.5 w-1.5 rounded-full ${conf.color}`} />
                      <span className={`text-[10px] ${textMuted}`}>{conf.label}</span>
                    </span>
                  </div>
                </div>
              );
            })}

            {/* Mobile Pagination */}
            {totalPages > 1 && (
              <div className={`flex items-center justify-center gap-3 py-2 ${textSecondary}`}>
                <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
                  className="p-2 rounded-lg bg-gray-100 dark:bg-gray-800 disabled:opacity-30">
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <span className="text-xs tabular-nums">{page + 1} / {totalPages}</span>
                <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}
                  className="p-2 rounded-lg bg-gray-100 dark:bg-gray-800 disabled:opacity-30">
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* Footer info */}
      <div className={`flex items-center gap-4 text-xs ${textMuted}`}>
        <span className="flex items-center gap-1">
          <FileText className="h-3.5 w-3.5" />
          {invoices.filter(i => i.filename?.toLowerCase().endsWith('.pdf')).length} PDF-Einträge
        </span>
        <span className="ml-auto">{invoices.length} Rechnungen geladen</span>
      </div>

      {/* Detail Modal */}
      {(selectedInvoice || invoiceDetailLoading) && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 backdrop-blur-sm sm:items-center modal-safe"
          onClick={() => { if (!invoiceDetailLoading) setSelectedInvoice(null); }}
        >
          <div
            className="max-h-[min(95vh,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)))] w-full overflow-auto rounded-t-2xl bg-white pb-[env(safe-area-inset-bottom,0px)] shadow-2xl sm:max-h-[85vh] sm:max-w-6xl sm:rounded-2xl sm:pb-0 dark:bg-gray-900"
            onClick={e => e.stopPropagation()}
          >
            {invoiceDetailLoading ? (
              <div className="flex flex-col items-center gap-3 py-20">
                <div className="h-6 w-6 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin" />
                <p className={textMuted}>Lade Details…</p>
              </div>
            ) : selectedInvoice && (
              <ModalContent inv={selectedInvoice} onClose={() => setSelectedInvoice(null)} styleCtx={styleCtx} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- Structured Modal Content ---------- */

function ModalContent({
  inv, onClose, styleCtx,
}: { inv: Record<string, unknown>; onClose: () => void; styleCtx: StyleCtx }) {
  const { textPrimary, textSecondary, textMuted } = styleCtx;
  const vendor = (inv.vendor ?? inv.Kreditor ?? '–') as string;
  const category = (inv.category ?? inv.Kategorie ?? '') as string;
  const docType = (inv.Dokumenttyp ?? inv.doc_type ?? 'RECHNUNG') as string;
  const pdfPath = (inv.pdf_path ?? inv.Dateipfad ?? '') as string;
  const date = (inv.date ?? inv.Rechnungsdatum ?? '') as string;
  const amount = (inv.amount_chf ?? inv.Betrag_CHF ?? inv.Betrag) as number | undefined;
  const currency = (inv.currency ?? inv.Währung ?? 'CHF') as string;
  const product = (inv.product ?? inv['Produkt/Dienstleistung'] ?? inv.Produkt ?? '') as string;
  const country = (inv.Land ?? inv.country ?? '') as string;
  const mwst = (inv.MwSt ?? inv.mwst ?? '') as string;
  const payMethod = (inv.Zahlungsart ?? '') as string;
  const confScore = inv.Confidence_Score;

  const headerFields: [string, string][] = [
    ['Datum', date || '–'],
    ['Betrag', amount != null ? formatCHF(amount) : '–'],
    ['Währung', currency],
    ['Land', country || '–'],
    ['MwSt', mwst || '–'],
    ['Zahlungsart', payMethod || '–'],
  ];

  const SKIP = new Set([
    'vendor', 'Kreditor', 'category', 'Kategorie', 'pdf_path', 'Dateipfad',
    'index', 'invoice_id', 'date', 'Rechnungsdatum', 'amount_chf', 'Betrag_CHF',
    'Betrag', 'amount', 'currency', 'Währung', 'product', 'Produkt/Dienstleistung',
    'Produkt', 'Land', 'country', 'MwSt', 'mwst', 'Zahlungsart', 'Dokumenttyp',
    'doc_type', 'Confidence_Score', 'filename', 'Dateiname',
  ]);
  const extraFields = Object.entries(inv).filter(([k, v]) => !SKIP.has(k) && v != null && v !== '');

  return (
    <div className="p-5 sm:p-6">
      {/* Header */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <h2 className={`text-xl font-bold ${textPrimary}`}>{vendor}</h2>
          <div className="mt-1.5 flex flex-wrap gap-2">
            <span className="rounded-full bg-indigo-100 dark:bg-indigo-900/40 px-2.5 py-0.5 text-xs font-medium text-indigo-700 dark:text-indigo-300">
              {docType}
            </span>
            {category && (
              <span className="rounded-full bg-purple-100 dark:bg-purple-900/40 px-2.5 py-0.5 text-xs font-medium text-purple-700 dark:text-purple-300">
                {category}
              </span>
            )}
            {confScore != null && (
              <span className="flex items-center gap-1 rounded-full bg-gray-100 dark:bg-gray-800 px-2.5 py-0.5 text-xs font-medium text-gray-600 dark:text-gray-300">
                <span className={`h-1.5 w-1.5 rounded-full ${confidenceDisplay(confScore).color}`} />
                Confidence {String(confScore)}%
              </span>
            )}
          </div>
        </div>
        <button onClick={onClose} className={`rounded-lg p-1.5 hover:bg-gray-100 dark:hover:bg-gray-800 ${textMuted}`}>
          <X className="h-5 w-5" />
        </button>
      </div>

      <div className="grid gap-6 lg:grid-cols-5">
        {/* Left: structured data (3 cols) */}
        <div className="lg:col-span-3 space-y-5">
          {/* Rechnungskopf */}
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-3">
            {headerFields.map(([label, val]) => (
              <div key={label}>
                <p className={`text-[11px] uppercase tracking-wide ${textMuted}`}>{label}</p>
                <p className={`text-sm font-medium ${textPrimary}`}>{val}</p>
              </div>
            ))}
          </div>

          {/* Leistungen */}
          {product && (
            <div>
              <p className={`text-[11px] uppercase tracking-wide mb-1 ${textMuted}`}>Produkt / Dienstleistung</p>
              <p className={`text-sm ${textPrimary}`}>{product}</p>
            </div>
          )}

          {/* Zahlungszusammenfassung */}
          {amount != null && (
            <div className="rounded-lg bg-indigo-50 dark:bg-indigo-950/30 p-4">
              <div className="flex items-baseline justify-between">
                <span className={`text-sm font-medium ${textSecondary}`}>Gesamtbetrag</span>
                <span className={`text-2xl font-bold ${textPrimary}`}>{formatCHF(amount)}</span>
              </div>
              <p className={`text-xs mt-1 ${textMuted}`}>{currency}{mwst ? ` · ${mwst}` : ''}</p>
            </div>
          )}

          {/* Zusätzliche Metadaten */}
          {extraFields.length > 0 && (
            <div>
              <p className={`text-[11px] uppercase tracking-wide mb-2 ${textMuted}`}>Weitere Details</p>
              <div className="grid grid-cols-2 gap-x-4 gap-y-2">
                {extraFields.map(([key, val]) => (
                  <div key={key}>
                    <p className={`text-[10px] ${textMuted}`}>{key.replace(/_/g, ' ')}</p>
                    <p className={`text-xs ${textPrimary}`}>
                      {typeof val === 'number' ? val.toLocaleString('de-CH') : String(val)}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right: PDF area (2 cols) */}
        <div className="lg:col-span-2">
          <p className={`text-[11px] uppercase tracking-wide mb-2 ${textMuted}`}>Dokument</p>
          {pdfPath ? (
            <div className="flex flex-col items-center justify-center rounded-xl border-2 border-dashed border-gray-300 dark:border-gray-700 p-8 gap-3">
              <FileText className={`h-16 w-16 ${textMuted}`} />
              <p className={`text-xs text-center break-all ${textMuted}`}>{pdfPath.split('/').pop()}</p>
              <a
                href="http://invoice.innosmith.ai"
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-xs font-medium text-white hover:bg-indigo-700 transition-colors"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                In InvoiceInsight öffnen
              </a>
            </div>
          ) : (
            <div className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed border-gray-300 dark:border-gray-700 p-8 gap-2 ${textMuted}`}>
              <FileText className="h-12 w-12 opacity-30" />
              <p className="text-xs">Kein PDF verfügbar</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
