import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Cell, PieChart, Pie,
  LineChart, Line, Legend,
} from 'recharts';
import { api } from '../api/client';
import { BackgroundPicker } from '../components/BackgroundPicker';

interface TogglProjectRow {
  project_id: number;
  project_name: string;
  client_id: number | null;
  client_name: string;
  hours: number;
  billable_hours: number;
  is_billable: boolean;
  pct_of_total: number;
  rate_per_hour: number;
  amount: number;
  budget_hours: number | null;
  budget_pct: number | null;
}

interface DailyHours {
  date: string;
  billable: number;
  non_billable: number;
}

interface TogglMonthSummary {
  total_hours: number;
  billable_hours: number;
  non_billable_hours: number;
  billable_ratio: number;
  total_amount: number;
  avg_daily_hours: number;
  forecast_month_amount: number;
  working_days_total: number;
  working_days_elapsed: number;
  projects: TogglProjectRow[];
  daily_hours: DailyHours[];
}

interface DebtorSummary {
  contact_id: number;
  contact_name: string;
  revenue_ytd: number;
  revenue_prior_year: number;
  revenue_delta_pct: number | null;
  open_invoices_count: number;
  open_invoices_total: number;
  avg_payment_days: number | null;
  aging_0_30: number;
  aging_31_60: number;
  aging_61_90: number;
  aging_over_90: number;
  project_count: number;
}

interface RevenueByMonth {
  contact_id: number;
  contact_name: string;
  months: Record<string, number>;
}

interface DebtorsResponse {
  toggl_month: TogglMonthSummary;
  debtors: DebtorSummary[];
  revenue_trend: RevenueByMonth[];
  total_open: number;
  total_revenue_ytd: number;
  dso_days: number | null;
  currency: string;
}

function formatCHF(value: number | null | undefined): string {
  if (value == null) return '–';
  return new Intl.NumberFormat('de-CH', { style: 'currency', currency: 'CHF' }).format(value);
}

function formatHours(h: number): string {
  return `${h.toFixed(1)}h`;
}

function formatHoursHM(h: number): string {
  const totalMinutes = Math.round(h * 60);
  const hours = Math.floor(totalMinutes / 60);
  const mins = totalMinutes % 60;
  return `${hours}:${String(mins).padStart(2, '0')}:00`;
}

function formatPct(v: number | null | undefined): string {
  if (v == null) return '–';
  return `${v.toFixed(1)}%`;
}

function formatMonthLabel(month: string): string {
  const [y, m] = month.split('-');
  const names = ['', 'Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
  return `${names[parseInt(m)]} ${y.slice(2)}`;
}

function currentMonthStr(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function formatMonthLong(month: string): string {
  const [y, m] = month.split('-').map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString('de-CH', { month: 'long', year: 'numeric' });
}

const PROJECT_COLORS = [
  '#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6',
  '#14b8a6', '#f97316', '#ec4899', '#06b6d4', '#84cc16',
  '#d946ef', '#0ea5e9', '#a3e635', '#fb7185', '#38bdf8',
];

const TREND_COLORS = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6'];

const CURSOR_STYLE = { fill: 'rgba(107,114,128,0.08)' };

export default function DebtorsPage() {
  const [data, setData] = useState<DebtorsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [debtorSort, setDebtorSort] = useState<'revenue' | 'open' | 'name'>('revenue');
  const [expandedDebtor, setExpandedDebtor] = useState<number | null>(null);

  const currentMonth = useMemo(() => currentMonthStr(), []);
  const [selectedMonth, setSelectedMonth] = useState<string>(currentMonth);
  const [monthData, setMonthData] = useState<TogglMonthSummary | null>(null);
  const [monthLoading, setMonthLoading] = useState(false);
  const isCurrentMonth = selectedMonth === currentMonth;

  const [pipedriveLeads, setPipedriveLeads] = useState<{ id: string; title: string; person_name: string | null; org_name: string | null; value: number | null; currency: string | null }[]>([]);
  const [pipedriveDeals, setPipedriveDeals] = useState<{ id: number; title: string; value: number | null; currency: string | null; person_name: string | null; org_name: string | null }[]>([]);
  const [pipedriveActivities, setPipedriveActivities] = useState<{ id: number; subject: string; type: string | null; due_date: string | null; person_name: string | null }[]>([]);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.get<DebtorsResponse>('/api/debtors');
      setData(resp);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Daten konnten nicht geladen werden');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
    api.get<{ debtors_background_url: string | null }>('/api/settings')
      .then(s => { if (s.debtors_background_url) setBgUrl(s.debtors_background_url); })
      .catch(() => {});
    // Pipedrive-Daten laden
    Promise.allSettled([
      api.get<{ id: number; title: string; value: number | null; currency: string | null; person_name: string | null; org_name: string | null }[]>('/api/pipedrive/deals?status=open&limit=20'),
      api.get<{ id: string; title: string; person_name: string | null; org_name: string | null; value: number | null; currency: string | null }[]>('/api/pipedrive/leads?limit=100'),
      api.get<{ id: number; subject: string; type: string | null; due_date: string | null; person_name: string | null }[]>('/api/pipedrive/activities?done=false&limit=15'),
    ]).then(([deals, leads, acts]) => {
      if (deals.status === 'fulfilled') setPipedriveDeals(deals.value);
      if (leads.status === 'fulfilled') setPipedriveLeads(leads.value);
      if (acts.status === 'fulfilled') setPipedriveActivities(acts.value);
    });
  }, [loadData]);

  useEffect(() => {
    if (selectedMonth === currentMonth) {
      setMonthData(null);
      return;
    }
    let cancelled = false;
    setMonthLoading(true);
    api.get<TogglMonthSummary>(`/api/debtors/toggl-month?month=${selectedMonth}`)
      .then(res => { if (!cancelled) setMonthData(res); })
      .catch(() => { if (!cancelled) setMonthData(null); })
      .finally(() => { if (!cancelled) setMonthLoading(false); });
    return () => { cancelled = true; };
  }, [selectedMonth, currentMonth]);

  const handleRefresh = async () => {
    try { await api.post('/api/debtors/cache/clear', {}); } catch { /* ignore */ }
    loadData();
    if (selectedMonth !== currentMonth) {
      setMonthLoading(true);
      try {
        const res = await api.get<TogglMonthSummary>(`/api/debtors/toggl-month?month=${selectedMonth}`);
        setMonthData(res);
      } catch { /* ignore */ } finally {
        setMonthLoading(false);
      }
    }
  };

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { debtors_background_url: url });
    setBgUrl(url);
  };

  const hasBg = !!bgUrl;
  const isGradient = bgUrl?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient
    ? { background: bgUrl!.slice('gradient:'.length) }
    : hasBg ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover' as const, backgroundPosition: 'center' } : undefined;

  const cardClass = hasBg
    ? 'bg-black/30 backdrop-blur-xl ring-1 ring-white/10'
    : 'border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800/50';
  const sectionClass = `rounded-2xl p-4 sm:p-6 ${cardClass}`;
  const textPrimary = hasBg ? 'text-white' : 'text-gray-900 dark:text-white';
  const textSecondary = hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400';
  const textMuted = hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500';

  const toggl = isCurrentMonth ? data?.toggl_month : (monthData ?? undefined);

  const sortedDebtors = useMemo(() => {
    if (!data?.debtors) return [];
    const d = [...data.debtors];
    switch (debtorSort) {
      case 'open': return d.sort((a, b) => b.open_invoices_total - a.open_invoices_total);
      case 'name': return d.sort((a, b) => a.contact_name.localeCompare(b.contact_name));
      default: return d.sort((a, b) => b.revenue_ytd - a.revenue_ytd);
    }
  }, [data?.debtors, debtorSort]);


  const agingData = useMemo(() => {
    if (!data?.debtors) return [];
    let a030 = 0, a3160 = 0, a6190 = 0, a90 = 0;
    for (const d of data.debtors) {
      a030 += d.aging_0_30; a3160 += d.aging_31_60; a6190 += d.aging_61_90; a90 += d.aging_over_90;
    }
    return [
      { range: '0–30', amount: a030, fill: '#22c55e' },
      { range: '31–60', amount: a3160, fill: '#f59e0b' },
      { range: '61–90', amount: a6190, fill: '#f97316' },
      { range: '90+', amount: a90, fill: '#ef4444' },
    ].filter(d => d.amount > 0);
  }, [data?.debtors]);

  const trendChartData = useMemo(() => {
    if (!data?.revenue_trend?.length) return [];
    const allMonths = new Set<string>();
    for (const t of data.revenue_trend) Object.keys(t.months).forEach(m => allMonths.add(m));
    const sorted = [...allMonths].sort();
    return sorted.map(m => {
      const row: Record<string, unknown> = { month: formatMonthLabel(m) };
      for (const t of data.revenue_trend) row[t.contact_name] = t.months[m] || 0;
      return row;
    });
  }, [data?.revenue_trend]);

  const monthProgress = toggl
    ? Math.round((toggl.working_days_elapsed / Math.max(toggl.working_days_total, 1)) * 100) : 0;

  return (
    <div className="relative flex h-full flex-col" style={hasBg ? bgStyle : undefined}>
      {!hasBg && <div className="absolute inset-0 bg-gradient-to-br from-slate-50 via-white to-emerald-50/20 dark:from-gray-950 dark:via-gray-900 dark:to-emerald-950/10" />}
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/25 dark:bg-black/40" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/25" />}

      <div className="relative z-10 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
        <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            {/* Titel liefert mobil der MobileHeader. */}
            <h1 className={`hidden text-xl font-bold lg:block lg:text-2xl ${hasBg ? 'text-white drop-shadow-sm' : 'text-gray-900 dark:text-white'}`}>Debitorensicht</h1>
            <p className={`mt-1 text-xs ${textMuted}`}>Toggl: live &middot; Bexio: letzte 2 Jahre &middot; Stand: {new Date().toLocaleString('de-CH')}</p>
          </div>
          <div className="flex items-center gap-2">
            <div className={`flex items-center gap-0.5 rounded-lg p-0.5 ${hasBg ? 'bg-white/10 backdrop-blur-sm' : 'border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800'}`}>
              <button
                onClick={() => setSelectedMonth(m => shiftMonth(m, -1))}
                disabled={monthLoading}
                title="Vorheriger Monat"
                className={`flex min-h-10 min-w-10 items-center justify-center rounded-md p-1.5 transition-colors disabled:opacity-40 lg:min-h-0 lg:min-w-0 ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700'}`}
              >
                <ChevronLeftIcon className="h-4 w-4" />
              </button>
              <span className={`min-w-[7.5rem] text-center text-sm font-medium tabular-nums ${hasBg ? 'text-white' : 'text-gray-700 dark:text-gray-200'}`}>
                {formatMonthLong(selectedMonth)}
              </span>
              <button
                onClick={() => setSelectedMonth(m => shiftMonth(m, 1))}
                disabled={isCurrentMonth || monthLoading}
                title="Nächster Monat"
                className={`flex min-h-10 min-w-10 items-center justify-center rounded-md p-1.5 transition-colors disabled:opacity-40 lg:min-h-0 lg:min-w-0 ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700'}`}
              >
                <ChevronRightIcon className="h-4 w-4" />
              </button>
            </div>
            <button onClick={() => setBgPickerOpen(true)} className={`flex min-h-11 min-w-11 items-center justify-center rounded-lg p-2 transition-colors lg:min-h-0 lg:min-w-0 ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`} title="Hintergrund ändern">
              <BgImageIcon className="h-5 w-5" />
            </button>
            <button onClick={handleRefresh} disabled={loading} className={`flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 lg:min-w-0 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20 backdrop-blur-sm' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'}`}>
              <RefreshIcon className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              <span className="hidden sm:inline">Aktualisieren</span>
            </button>
          </div>
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">{error}</div>
        )}

        {toggl && <KpiStrip toggl={toggl} data={data!} hasBg={hasBg} textMuted={textMuted} />}

        {monthLoading && !toggl && (
          <div className={`mb-6 flex items-center justify-center py-12 ${sectionClass}`}>
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-indigo-200 border-t-indigo-600" />
          </div>
        )}

        {toggl && toggl.projects.length > 0 && (
          <MonthCockpit toggl={toggl} selectedMonth={selectedMonth} monthProgress={monthProgress} hasBg={hasBg}
            sectionClass={sectionClass} textPrimary={textPrimary} textSecondary={textSecondary} textMuted={textMuted} />
        )}

        {!isCurrentMonth && !monthLoading && toggl && toggl.projects.length === 0 && (
          <div className={`mb-6 rounded-2xl p-8 text-center ${sectionClass}`}>
            <p className={`text-sm ${textSecondary}`}>Keine Toggl-Daten für {formatMonthLong(selectedMonth)}.</p>
          </div>
        )}

        {/* ── Pipedrive: Leads, Deals & Aufgaben ── */}
        {(pipedriveLeads.length > 0 || pipedriveDeals.length > 0 || pipedriveActivities.length > 0) && (
          <div className={`mb-6 ${sectionClass}`}>
            <div className="mb-3 flex items-center justify-between">
              <h3 className={`text-sm font-semibold ${textPrimary}`}>Pipedrive CRM</h3>
              <a
                href="https://innosmith.pipedrive.com"
                target="_blank"
                rel="noopener noreferrer"
                className={`text-xs font-medium ${textSecondary} hover:underline`}
              >
                Pipedrive öffnen →
              </a>
            </div>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
              {/* Leads */}
              <div>
                <div className="mb-2 flex items-center justify-between">
                  <span className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Leads</span>
                  <span className={`text-[11px] ${textMuted}`}>{pipedriveLeads.length}</span>
                </div>
                {pipedriveLeads.length === 0 ? (
                  <p className={`text-xs ${textMuted}`}>Keine offenen Leads</p>
                ) : (
                  <div className="max-h-56 space-y-1 overflow-y-auto">
                    {pipedriveLeads.map(lead => (
                      <a key={lead.id} href={`https://innosmith.pipedrive.com/leads/inbox/${lead.id}`} target="_blank" rel="noopener noreferrer"
                        className={`flex items-center gap-2 rounded-lg p-2 transition-colors ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}>
                        <div className="h-1.5 w-1.5 rounded-full shrink-0 bg-amber-400" />
                        <div className="min-w-0 flex-1">
                          <div className={`text-sm font-medium truncate ${textPrimary}`}>{lead.title}</div>
                          <div className={`text-[11px] ${textMuted}`}>{lead.person_name || lead.org_name || ''}</div>
                        </div>
                        {lead.value != null && lead.value > 0 && (
                          <span className={`shrink-0 text-[11px] font-medium ${textMuted}`}>{lead.currency || 'CHF'} {lead.value.toLocaleString('de-CH')}</span>
                        )}
                      </a>
                    ))}
                  </div>
                )}
              </div>
              {/* Deals */}
              <div>
                <div className="mb-2 flex items-center justify-between">
                  <span className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Deals</span>
                  <span className={`text-[11px] ${textMuted}`}>{pipedriveDeals.length}</span>
                </div>
                {pipedriveDeals.length === 0 ? (
                  <p className={`text-xs ${textMuted}`}>Keine offenen Deals</p>
                ) : (
                  <div className="max-h-56 space-y-1 overflow-y-auto">
                    {pipedriveDeals.map(deal => (
                      <a key={deal.id} href={`https://innosmith.pipedrive.com/deal/${deal.id}`} target="_blank" rel="noopener noreferrer"
                        className={`flex items-center gap-2 rounded-lg p-2 transition-colors ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}>
                        <div className="h-1.5 w-1.5 rounded-full shrink-0 bg-green-500" />
                        <div className="min-w-0 flex-1">
                          <div className={`text-sm font-medium truncate ${textPrimary}`}>{deal.title}</div>
                          <div className={`text-[11px] ${textMuted}`}>{deal.person_name || deal.org_name || ''}</div>
                        </div>
                        {deal.value != null && deal.value > 0 && (
                          <span className={`shrink-0 text-[11px] font-semibold ${hasBg ? 'text-green-300' : 'text-green-600 dark:text-green-400'}`}>{deal.currency || 'CHF'} {deal.value.toLocaleString('de-CH')}</span>
                        )}
                      </a>
                    ))}
                  </div>
                )}
              </div>
              {/* Aktivitäten */}
              <div>
                <div className="mb-2 flex items-center justify-between">
                  <span className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Aufgaben</span>
                  <span className={`text-[11px] ${textMuted}`}>{pipedriveActivities.length}</span>
                </div>
                {pipedriveActivities.length === 0 ? (
                  <p className={`text-xs ${textMuted}`}>Keine offenen CRM-Aufgaben</p>
                ) : (
                  <div className="max-h-56 space-y-1 overflow-y-auto">
                    {pipedriveActivities.map(act => {
                      const isOverdue = act.due_date && new Date(act.due_date) < new Date();
                      return (
                        <a key={act.id} href={`https://innosmith.pipedrive.com/activities/${act.id}`} target="_blank" rel="noopener noreferrer"
                          className={`flex items-center gap-2 rounded-lg p-2 transition-colors ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}>
                          <div className={`h-1.5 w-1.5 rounded-full shrink-0 ${isOverdue ? 'bg-red-500' : 'bg-orange-400'}`} />
                          <div className="min-w-0 flex-1">
                            <div className={`text-sm font-medium truncate ${textPrimary}`}>{act.subject}</div>
                            <div className={`text-[11px] ${isOverdue ? 'text-red-500 font-medium' : textMuted}`}>
                              {act.type || 'Aufgabe'}
                              {act.due_date && ` · ${isOverdue ? 'Überfällig' : new Date(act.due_date).toLocaleDateString('de-CH')}`}
                              {act.person_name && ` · ${act.person_name}`}
                            </div>
                          </div>
                        </a>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {toggl && agingData.length > 0 && (
          <div className={`mb-6 ${sectionClass}`}>
            <h3 className={`mb-3 text-sm font-semibold ${textPrimary}`}>Fälligkeitsstruktur</h3>
            <ResponsiveContainer width="100%" height={120}>
              <BarChart data={agingData} layout="vertical" barSize={16}>
                <XAxis type="number" tick={{ fontSize: 10 }} tickFormatter={(v: number) => formatCHF(v).replace('CHF', '').trim()} />
                <YAxis type="category" dataKey="range" tick={{ fontSize: 11 }} width={40} />
                <Tooltip cursor={CURSOR_STYLE} contentStyle={{ borderRadius: '0.5rem', fontSize: 12, background: '#1f2937', color: '#f9fafb', border: 'none' }} formatter={(v: unknown) => [formatCHF(Number(v)), 'Betrag']} />
                <Bar dataKey="amount" radius={[0, 4, 4, 0]}>{agingData.map((entry, idx) => <Cell key={idx} fill={entry.fill} />)}</Bar>
              </BarChart>
            </ResponsiveContainer>
            <p className={`mt-2 text-center text-[11px] ${textMuted}`}>Tage seit Rechnungsdatum</p>
          </div>
        )}

        {data && data.debtors.length > 0 && (
          <DebtorTable debtors={sortedDebtors} debtorSort={debtorSort} setDebtorSort={setDebtorSort}
            expandedDebtor={expandedDebtor} setExpandedDebtor={setExpandedDebtor}
            hasBg={hasBg} sectionClass={sectionClass} textPrimary={textPrimary} textSecondary={textSecondary} textMuted={textMuted} />
        )}

        {trendChartData.length > 0 && data?.revenue_trend && (
          <div className={`mb-6 ${sectionClass}`}>
            <h2 className={`mb-4 text-lg font-semibold ${textPrimary}`}>Umsatz-Trend (Top 5 Kunden)</h2>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={trendChartData}>
                <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
                <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} tickFormatter={(v: number) => Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}k` : v.toFixed(0)} />
                <Tooltip cursor={CURSOR_STYLE} contentStyle={{ borderRadius: '0.5rem', fontSize: 12, background: '#1f2937', color: '#f9fafb', border: 'none' }} formatter={(v: unknown) => [formatCHF(Number(v)), '']} />
                <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                {data.revenue_trend.map((t, idx) => (
                  <Line key={t.contact_id} type="monotone" dataKey={t.contact_name} stroke={TREND_COLORS[idx % TREND_COLORS.length]} strokeWidth={2} dot={{ r: 3 }} activeDot={{ r: 5 }} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}

        {loading && !data && (
          <div className="flex flex-col items-center justify-center py-20">
            <div className="h-10 w-10 animate-spin rounded-full border-4 border-indigo-200 border-t-indigo-600" />
            <p className={`mt-4 text-sm ${textSecondary}`}>Lade Debitorendaten...</p>
          </div>
        )}

        {!loading && !error && data && data.debtors.length === 0 && !toggl?.projects.length && (
          <div className={`rounded-2xl p-12 text-center ${sectionClass}`}>
            <EmptyIcon className={`mx-auto h-12 w-12 ${textMuted}`} />
            <h3 className={`mt-4 text-lg font-semibold ${textPrimary}`}>Noch keine Daten</h3>
            <p className={`mt-2 text-sm ${textSecondary}`}>Sobald Rechnungen in Bexio und Stunden in Toggl erfasst sind, erscheint hier die Übersicht.</p>
          </div>
        )}
      </div>

      <BackgroundPicker isOpen={bgPickerOpen} onClose={() => setBgPickerOpen(false)} currentUrl={bgUrl} onSelect={(url) => { handleBgSelect(url); setBgPickerOpen(false); }} />
    </div>
  );
}

function KpiStrip({ toggl, data, hasBg, textMuted: _textMuted }: { toggl: TogglMonthSummary; data: DebtorsResponse; hasBg: boolean; textMuted: string }) {
  return (
    <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-5">
      <KpiCard label="Stunden gesamt" value={formatHours(toggl.total_hours)}
        sublabel={`Ø ${toggl.avg_daily_hours}h/Tag · ${toggl.working_days_elapsed}/${toggl.working_days_total} AT`}
        icon={<ClockIcon />} status="neutral" hasBg={hasBg} />
      <KpiCard label="Verrechenbar-Quote" value={formatPct(toggl.billable_ratio)}
        sublabel={`${formatHours(toggl.billable_hours)} von ${formatHours(toggl.total_hours)}`}
        icon={<TargetIcon />} status={toggl.billable_ratio >= 75 ? 'green' : toggl.billable_ratio >= 50 ? 'yellow' : 'red'} hasBg={hasBg} />
      <KpiCard label="Lfd. Umsatz (Toggl)" value={formatCHF(toggl.total_amount)}
        sublabel={`Prognose: ${formatCHF(toggl.forecast_month_amount)}`}
        icon={<TrendIcon />} status="green" hasBg={hasBg} />
      <KpiCard label="Offene Debitoren" value={formatCHF(data.total_open)}
        sublabel={data.dso_days != null ? `DSO: ${data.dso_days} Tage` : `${data.debtors.filter(d => d.open_invoices_count > 0).length} Kunden`}
        icon={<InvoiceIcon />} status={data.dso_days != null ? (data.dso_days <= 30 ? 'green' : data.dso_days <= 60 ? 'yellow' : 'red') : 'neutral'} hasBg={hasBg} />
      <KpiCard label="Umsatz YTD" value={formatCHF(data.total_revenue_ytd)}
        sublabel="Rechnungen (Bexio)" icon={<BankIcon />} status="neutral" hasBg={hasBg} className="col-span-2 lg:col-span-1" />
    </div>
  );
}

function MonthCockpit({ toggl, selectedMonth, monthProgress, hasBg, sectionClass, textPrimary, textSecondary, textMuted }: {
  toggl: TogglMonthSummary; selectedMonth: string; monthProgress: number; hasBg: boolean;
  sectionClass: string; textPrimary: string; textSecondary: string; textMuted: string;
}) {
  const [expandedProject, setExpandedProject] = useState<number | null>(null);

  const dailyChartData = useMemo(() => {
    if (!toggl.daily_hours?.length) return [];
    const dayNames = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];
    const lookup = new Map(toggl.daily_hours.map(d => [d.date, d]));

    const [year, month1] = selectedMonth.split('-').map(Number);
    const month = month1 - 1;
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    const result = [];
    for (let day = 1; day <= daysInMonth; day++) {
      const dt = new Date(year, month, day, 12, 0, 0);
      const iso = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const entry = lookup.get(iso);
      result.push({
        label: `${dayNames[dt.getDay()]} ${String(day).padStart(2, '0')}`,
        billable: entry?.billable ?? 0,
        non_billable: entry?.non_billable ?? 0,
      });
    }
    return result;
  }, [toggl.daily_hours, selectedMonth]);

  const pieData = useMemo(() =>
    toggl.projects.map((p, i) => ({ name: p.project_name, value: p.hours, fill: PROJECT_COLORS[i % PROJECT_COLORS.length] })),
    [toggl.projects]
  );

  return (
    <div className={`mb-6 ${sectionClass}`}>
      {/* Header */}
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className={`text-lg font-semibold ${textPrimary}`}>Monats-Cockpit</h2>
          <p className={`text-xs ${textMuted}`}>{formatMonthLong(selectedMonth)} &middot; {monthProgress}% der Arbeitstage</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5"><div className="h-2.5 w-2.5 rounded-full bg-green-500" /><span className={`text-xs ${textSecondary}`}>Verrechenbar</span></div>
          <div className="flex items-center gap-1.5"><div className="h-2.5 w-2.5 rounded-full bg-rose-400" /><span className={`text-xs ${textSecondary}`}>Nicht verrechenbar</span></div>
        </div>
      </div>

      {/* Progress bar */}
      <div className="mb-5">
        <div className="flex items-center justify-between text-xs">
          <span className={textSecondary}>Monatsfortschritt</span>
          <span className={`font-medium ${textPrimary}`}>{monthProgress}%</span>
        </div>
        <div className={`mt-1.5 h-1.5 overflow-hidden rounded-full ${hasBg ? 'bg-white/10' : 'bg-gray-100 dark:bg-gray-700'}`}>
          <div className="h-full rounded-full bg-gradient-to-r from-indigo-500 to-violet-500 transition-all duration-700" style={{ width: `${monthProgress}%` }} />
        </div>
      </div>

      {/* KPI summary */}
      <div className={`mb-6 grid grid-cols-2 gap-3 rounded-xl p-3 sm:grid-cols-4 ${hasBg ? 'bg-white/5' : 'bg-gray-50 dark:bg-gray-800/50'}`}>
        <SummaryCell label="Stunden gesamt" value={formatHoursHM(toggl.total_hours)} textPrimary={textPrimary} textMuted={textMuted} />
        <SummaryCell label="Verrechenbare Stunden" value={`${formatHoursHM(toggl.billable_hours)} (${formatPct(toggl.billable_ratio)})`} textPrimary="text-green-500" textMuted={textMuted} />
        <SummaryCell label="Betrag" value={formatCHF(toggl.total_amount)} textPrimary={textPrimary} textMuted={textMuted} />
        <SummaryCell label="Ø Stunden/Tag" value={`${toggl.avg_daily_hours.toFixed(2)} Stunden`} textPrimary={textPrimary} textMuted={textMuted} />
      </div>

      {/* Charts: Duration by day + Project distribution (Toggl-Layout) */}
      <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Duration by day — nimmt 2/3 der Breite */}
        <div className="lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <h3 className={`text-sm font-semibold ${textPrimary}`}>Dauer pro Tag</h3>
            <span className={`rounded-md px-2 py-0.5 text-[11px] ${hasBg ? 'bg-white/10 text-white/60' : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'}`}>Gruppiert: Verrechenbar</span>
          </div>
          {dailyChartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={dailyChartData} barCategoryGap="8%">
                <CartesianGrid strokeDasharray="3 3" className="opacity-20" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 8 }}
                  interval={0}
                  angle={-45}
                  textAnchor="end"
                  height={40}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  tick={{ fontSize: 10 }}
                  tickFormatter={(v: number) => {
                    if (v === 0) return '0h';
                    const h = Math.floor(v);
                    const m = Math.round((v - h) * 60);
                    return m > 0 ? `${h}h ${m.toString().padStart(2, '0')}` : `${h}h`;
                  }}
                  width={45}
                  tickLine={false}
                  axisLine={false}
                />
                <Tooltip
                  cursor={CURSOR_STYLE}
                  contentStyle={{ borderRadius: '0.5rem', fontSize: 12, background: '#1f2937', color: '#f9fafb', border: 'none' }}
                  formatter={(v: unknown, name: unknown) => [`${Number(v).toFixed(2)}h`, String(name) === 'billable' ? 'Verrechenbar' : 'Nicht verrechenbar']}
                />
                <Bar dataKey="non_billable" stackId="a" fill="#fb7185" radius={[0, 0, 0, 0]} name="non_billable" />
                <Bar dataKey="billable" stackId="a" fill="#22c55e" radius={[2, 2, 0, 0]} name="billable" />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className={`flex h-[220px] items-center justify-center text-sm ${textMuted}`}>Lade tägliche Daten...</div>
          )}
          <div className="mt-2 flex items-center justify-center gap-4 text-xs">
            <div className="flex items-center gap-1.5"><div className="h-2.5 w-2.5 rounded bg-rose-400" /><span className={textSecondary}>Nicht verrechenbar</span></div>
            <div className="flex items-center gap-1.5"><div className="h-2.5 w-2.5 rounded bg-green-500" /><span className={textSecondary}>Verrechenbar</span></div>
          </div>
        </div>

        {/* Project distribution — nimmt 1/3 der Breite */}
        <div className="lg:col-span-1">
          <div className="mb-3 flex items-center justify-between">
            <h3 className={`text-sm font-semibold ${textPrimary}`}>Projektverteilung</h3>
            <span className={`rounded-md px-2 py-0.5 text-[11px] ${hasBg ? 'bg-white/10 text-white/60' : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'}`}>Aufgeteilt: Projekte</span>
          </div>
          <div className="flex flex-col items-center gap-3">
            <div className="relative">
              <ResponsiveContainer width={160} height={160}>
                <PieChart>
                  <Pie data={pieData} cx="50%" cy="50%" innerRadius={45} outerRadius={72} dataKey="value" strokeWidth={0}>
                    {pieData.map((_, i) => <Cell key={i} fill={PROJECT_COLORS[i % PROJECT_COLORS.length]} />)}
                  </Pie>
                  <Tooltip
                    contentStyle={{ borderRadius: '0.5rem', fontSize: 12, background: '#1f2937', color: '#f9fafb', border: 'none' }}
                    formatter={(v: unknown, name: unknown) => [`${Number(v).toFixed(1)}h`, String(name)]}
                  />
                </PieChart>
              </ResponsiveContainer>
              <div className="absolute inset-0 flex flex-col items-center justify-center">
                <span className={`text-sm font-bold ${textPrimary}`}>{formatHoursHM(toggl.total_hours)}</span>
                <span className={`text-[9px] uppercase tracking-wider ${textMuted}`}>Projekt</span>
              </div>
            </div>
            <div className="w-full space-y-1.5">
              {toggl.projects.map((p, i) => (
                <div key={p.project_id || i} className="flex items-center gap-2 text-xs">
                  <div className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ backgroundColor: PROJECT_COLORS[i % PROJECT_COLORS.length] }} />
                  <span className={`shrink-0 font-semibold tabular-nums ${textPrimary}`}>{p.pct_of_total.toFixed(1)}%</span>
                  <span className={`min-w-0 truncate font-medium ${textSecondary}`}>{p.project_name}</span>
                  {p.client_name && <span className={`shrink-0 ${textMuted}`}>&middot; {p.client_name}</span>}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Project and description breakdown — Desktop */}
      <div className="hidden sm:block">
        <div className="mb-3">
          <h3 className={`text-sm font-semibold ${textPrimary}`}>Projekt-Aufschlüsselung</h3>
        </div>
        <table className="w-full max-w-3xl">
          <thead>
            <tr className={`border-b text-left text-[11px] font-medium uppercase tracking-wider ${hasBg ? 'border-white/10 text-white/40' : 'border-gray-100 text-gray-400 dark:border-gray-700 dark:text-gray-500'}`}>
              <th className="w-8 py-2" />
              <th className="py-2 pr-2">Projekt / Beschreibung</th>
              <th className="w-[90px] px-2 py-2 text-right">Dauer</th>
              <th className="w-[70px] px-2 py-2 text-right">Dauer %</th>
              <th className="w-[100px] py-2 pl-2 text-right">Betrag</th>
            </tr>
          </thead>
          <tbody>
            {toggl.projects.map((p, i) => (
              <React.Fragment key={p.project_id || i}>
                <tr
                  className={`border-b cursor-pointer transition-colors ${hasBg ? 'border-white/5 hover:bg-white/5' : 'border-gray-50 hover:bg-gray-50/50 dark:border-gray-800 dark:hover:bg-gray-800/30'}`}
                  onClick={() => setExpandedProject(expandedProject === p.project_id ? null : p.project_id)}
                >
                  <td className="py-2 pl-2">
                    <svg className={`h-3.5 w-3.5 transition-transform ${expandedProject === p.project_id ? 'rotate-90' : ''} ${textMuted}`} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
                    </svg>
                  </td>
                  <td className="py-2 pr-2">
                    <div className="flex items-center gap-2">
                      <div className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: PROJECT_COLORS[i % PROJECT_COLORS.length] }} />
                      <span className={`text-sm font-medium ${textPrimary}`}>{p.project_name}</span>
                      {p.client_name && <span className={`text-xs ${textMuted}`}>&middot; {p.client_name}</span>}
                    </div>
                  </td>
                  <td className={`px-2 py-2 text-right text-sm font-semibold tabular-nums ${textPrimary}`}>{formatHoursHM(p.hours)}</td>
                  <td className={`px-2 py-2 text-right text-sm tabular-nums ${textSecondary}`}>{p.pct_of_total.toFixed(2)}%</td>
                  <td className={`py-2 pl-2 text-right text-sm font-semibold tabular-nums ${textPrimary}`}>{p.amount > 0 ? formatCHF(p.amount) : '–'}</td>
                </tr>
                {expandedProject === p.project_id && (
                  <tr className={hasBg ? 'bg-white/[0.02]' : 'bg-gray-25 dark:bg-gray-800/20'}>
                    <td />
                    <td colSpan={4} className="py-2 pr-2">
                      <div className={`flex items-center gap-4 text-xs ${textSecondary}`}>
                        <span>Verrechenbar: <strong className="text-green-500">{formatHours(p.billable_hours)}</strong></span>
                        <span>Rate: <strong>{p.rate_per_hour > 0 ? `CHF ${p.rate_per_hour}/h` : '–'}</strong></span>
                        {p.budget_hours != null && (
                          <span>Budget: <strong>{formatHours(p.hours)} / {formatHours(p.budget_hours)}</strong>
                            <span className={`ml-1 ${(p.budget_pct ?? 0) > 100 ? 'text-red-500' : (p.budget_pct ?? 0) > 80 ? 'text-amber-500' : 'text-green-500'}`}>({p.budget_pct?.toFixed(0)}%)</span>
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
            <tr className={`border-t-2 ${hasBg ? 'border-white/20' : 'border-gray-200 dark:border-gray-600'}`}>
              <td className="py-2" />
              <td className={`py-2 pr-2 text-sm font-bold ${textPrimary}`}>TOTAL</td>
              <td className={`px-2 py-2 text-right text-sm font-bold tabular-nums ${textPrimary}`}>{formatHoursHM(toggl.total_hours)}</td>
              <td className={`px-2 py-2 text-right text-sm font-bold tabular-nums ${textPrimary}`}>100%</td>
              <td className={`py-2 pl-2 text-right text-sm font-bold tabular-nums ${textPrimary}`}>{formatCHF(toggl.total_amount)}</td>
            </tr>
          </tbody>
        </table>
      </div>

      {/* Mobile: kompakte Tabelle */}
      <div className="sm:hidden">
        <h3 className={`mb-2 text-sm font-semibold ${textPrimary}`}>Projekt-Aufschlüsselung</h3>
        <table className="w-full">
          <thead>
            <tr className={`border-b text-left text-[10px] font-medium uppercase tracking-wider ${hasBg ? 'border-white/10 text-white/40' : 'border-gray-100 text-gray-400 dark:border-gray-700 dark:text-gray-500'}`}>
              <th className="py-2 pl-1 pr-1">Projekt</th>
              <th className="w-[55px] px-1 py-2 text-right">Zeit</th>
              <th className="w-[35px] px-1 py-2 text-right">%</th>
              <th className="w-[90px] py-2 pl-1 pr-1 text-right">CHF</th>
            </tr>
          </thead>
          <tbody>
            {toggl.projects.map((p, i) => (
              <tr key={p.project_id || i} className={`border-b transition-colors ${hasBg ? 'border-white/5' : 'border-gray-50 dark:border-gray-800'}`}>
                <td className="py-2 pl-1 pr-1">
                  <div className="flex items-center gap-2">
                    <div className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: PROJECT_COLORS[i % PROJECT_COLORS.length] }} />
                    <div className="min-w-0">
                      <p className={`truncate text-[13px] font-medium leading-tight ${textPrimary}`}>{p.project_name}</p>
                      {p.client_name && <p className={`truncate text-[11px] leading-tight ${textMuted}`}>{p.client_name}</p>}
                    </div>
                  </div>
                </td>
                <td className={`px-1 py-2 text-right text-[13px] font-semibold tabular-nums ${textPrimary}`}>{formatHoursHM(p.hours)}</td>
                <td className={`px-1 py-2 text-right text-[12px] tabular-nums ${textSecondary}`}>{p.pct_of_total.toFixed(0)}%</td>
                <td className={`py-2 pl-1 pr-1 text-right text-[13px] font-semibold tabular-nums ${textPrimary}`}>{p.amount > 0 ? formatCHF(p.amount) : '–'}</td>
              </tr>
            ))}
            <tr className={`border-t-2 ${hasBg ? 'border-white/20' : 'border-gray-200 dark:border-gray-600'}`}>
              <td className={`py-2 pl-1 text-[13px] font-bold ${textPrimary}`}>TOTAL</td>
              <td className={`px-1 py-2 text-right text-[13px] font-bold tabular-nums ${textPrimary}`}>{formatHoursHM(toggl.total_hours)}</td>
              <td className={`px-1 py-2 text-right text-[12px] font-bold tabular-nums ${textPrimary}`}>100%</td>
              <td className={`py-2 pl-1 pr-1 text-right text-[13px] font-bold tabular-nums ${textPrimary}`}>{formatCHF(toggl.total_amount)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SummaryCell({ label, value, textPrimary, textMuted }: { label: string; value: string; textPrimary: string; textMuted: string }) {
  return (
    <div>
      <p className={`text-[11px] font-medium uppercase tracking-wider ${textMuted}`}>{label}</p>
      <p className={`text-lg font-bold ${textPrimary}`}>{value}</p>
    </div>
  );
}

function DebtorTable({ debtors, debtorSort, setDebtorSort, expandedDebtor, setExpandedDebtor, hasBg, sectionClass, textPrimary, textSecondary, textMuted }: {
  debtors: DebtorSummary[]; debtorSort: string; setDebtorSort: (s: 'revenue' | 'open' | 'name') => void;
  expandedDebtor: number | null; setExpandedDebtor: (id: number | null) => void;
  hasBg: boolean; sectionClass: string; textPrimary: string; textSecondary: string; textMuted: string;
}) {
  return (
    <div className={`mb-6 ${sectionClass}`}>
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h2 className={`text-lg font-semibold ${textPrimary}`}>Debitoren (Bexio)</h2>
        <div className="flex items-center gap-1.5">
          {(['revenue', 'open', 'name'] as const).map((key) => (
            <button key={key} onClick={() => setDebtorSort(key)}
              className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-colors ${debtorSort === key
                ? hasBg ? 'bg-white/20 text-white' : 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400'
                : hasBg ? 'text-white/50 hover:bg-white/10 hover:text-white/70' : 'text-gray-400 hover:bg-gray-50 hover:text-gray-600 dark:hover:bg-gray-800'}`}>
              {{ revenue: 'Umsatz', open: 'Offen', name: 'Name' }[key]}
            </button>
          ))}
        </div>
      </div>

      {/* Mobile */}
      <div className="space-y-3 sm:hidden">
        {debtors.map((d) => (
          <div key={d.contact_id} className={`rounded-xl p-3 transition-colors ${hasBg ? 'bg-white/5 active:bg-white/10' : 'bg-gray-50 active:bg-gray-100 dark:bg-gray-800/30'}`}
            onClick={() => setExpandedDebtor(expandedDebtor === d.contact_id ? null : d.contact_id)}>
            <div className="flex items-center justify-between">
              <p className={`text-sm font-semibold ${textPrimary}`}>{d.contact_name}</p>
              <p className={`text-sm font-bold tabular-nums ${textPrimary}`}>{formatCHF(d.revenue_ytd)}</p>
            </div>
            <div className="mt-1.5 flex items-center gap-3 text-xs">
              {d.open_invoices_count > 0 && <span className="text-amber-500">{d.open_invoices_count} offen &middot; {formatCHF(d.open_invoices_total)}</span>}
              {d.revenue_delta_pct != null && <span className={d.revenue_delta_pct >= 0 ? 'text-green-500' : 'text-red-500'}>{d.revenue_delta_pct >= 0 ? '+' : ''}{d.revenue_delta_pct}% vs. VJ</span>}
            </div>
            {expandedDebtor === d.contact_id && (
              <div className={`mt-3 grid grid-cols-2 gap-2 border-t pt-3 text-xs ${hasBg ? 'border-white/10' : 'border-gray-200 dark:border-gray-700'}`}>
                <div><p className={textMuted}>Umsatz VJ</p><p className={`font-semibold ${textPrimary}`}>{formatCHF(d.revenue_prior_year)}</p></div>
                <div><p className={textMuted}>Offen total</p><p className="font-semibold text-amber-500">{formatCHF(d.open_invoices_total)}</p></div>
                {d.aging_over_90 > 0 && <div className="col-span-2"><p className="font-semibold text-red-500">{formatCHF(d.aging_over_90)} überfällig (&gt;90 Tage)</p></div>}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Desktop */}
      <div className="hidden sm:block">
        <div className="-mx-6 overflow-x-auto">
          <table className="w-full min-w-[700px]">
            <thead>
              <tr className={`border-b text-left text-[11px] font-medium uppercase tracking-wider ${hasBg ? 'border-white/10 text-white/40' : 'border-gray-100 text-gray-400 dark:border-gray-700 dark:text-gray-500'}`}>
                <th className="py-2 pl-6 pr-2">Kunde</th>
                <th className="px-2 py-2 text-right">Umsatz YTD</th>
                <th className="px-2 py-2 text-right">VJ</th>
                <th className="px-2 py-2 text-right">Δ%</th>
                <th className="px-2 py-2 text-right">Offen</th>
                <th className="py-2 pl-2 pr-6 text-right">Aging 90+</th>
              </tr>
            </thead>
            <tbody>
              {debtors.map((d) => (
                <tr key={d.contact_id} className={`border-b transition-colors ${hasBg ? 'border-white/5 hover:bg-white/5' : 'border-gray-50 hover:bg-gray-50/50 dark:border-gray-800 dark:hover:bg-gray-800/30'}`}>
                  <td className={`py-3 pl-6 pr-2 text-sm font-medium ${textPrimary}`}>{d.contact_name}</td>
                  <td className={`px-2 py-3 text-right text-sm font-semibold tabular-nums ${textPrimary}`}>{formatCHF(d.revenue_ytd)}</td>
                  <td className={`px-2 py-3 text-right text-sm tabular-nums ${textSecondary}`}>{formatCHF(d.revenue_prior_year)}</td>
                  <td className="px-2 py-3 text-right text-sm tabular-nums">
                    {d.revenue_delta_pct != null
                      ? <span className={d.revenue_delta_pct >= 0 ? 'text-green-500' : 'text-red-500'}>{d.revenue_delta_pct >= 0 ? '+' : ''}{d.revenue_delta_pct}%</span>
                      : <span className={textMuted}>{'–'}</span>}
                  </td>
                  <td className="px-2 py-3 text-right text-sm tabular-nums">
                    {d.open_invoices_total > 0 ? <span className="text-amber-500">{formatCHF(d.open_invoices_total)}</span> : <span className={textMuted}>{'–'}</span>}
                  </td>
                  <td className="py-3 pl-2 pr-6 text-right text-sm tabular-nums">
                    {d.aging_over_90 > 0 ? <span className="font-semibold text-red-500">{formatCHF(d.aging_over_90)}</span> : <span className={textMuted}>{'–'}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function KpiCard({ label, value, sublabel, icon, status = 'neutral', hasBg = false, className = '' }: {
  label: string; value: string; sublabel: string; icon: React.ReactNode;
  status?: 'green' | 'yellow' | 'red' | 'neutral'; hasBg?: boolean; className?: string;
}) {
  const statusColors: Record<string, string> = {
    green: 'border-l-green-500', yellow: 'border-l-amber-500', red: 'border-l-red-500',
    neutral: hasBg ? 'border-l-white/20' : 'border-l-gray-200 dark:border-l-gray-700',
  };
  const bgClass = hasBg
    ? 'bg-black/20 backdrop-blur-xl ring-1 ring-white/10 border-transparent'
    : 'border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800/50';
  return (
    <div className={`rounded-xl border border-l-4 ${statusColors[status]} ${bgClass} p-3 sm:p-4 ${className}`}>
      <div className="flex items-center gap-2 lg:gap-3">
        {/* Dekoratives Icon frisst auf 393px die Breite, die der CHF-Betrag braucht. */}
        <div className={`hidden h-8 w-8 shrink-0 items-center justify-center rounded-lg lg:flex lg:h-10 lg:w-10 ${hasBg ? 'bg-white/10 text-white/70' : 'bg-gray-50 text-gray-500 dark:bg-gray-800 dark:text-gray-400'}`}>{icon}</div>
        <div className="min-w-0 flex-1">
          <p className={`text-[11px] font-medium lg:text-xs ${hasBg ? 'text-white/50' : 'text-gray-500 dark:text-gray-400'}`}>{label}</p>
          <p className={`text-sm font-bold leading-tight tabular-nums lg:text-lg ${hasBg ? 'text-white' : 'text-gray-900 dark:text-white'}`}>{value}</p>
          <p className={`text-[10px] lg:truncate lg:text-xs ${hasBg ? 'text-white/40' : 'text-gray-400 dark:text-gray-500'}`}>{sublabel}</p>
        </div>
      </div>
    </div>
  );
}

function RefreshIcon({ className }: { className?: string }) {
  return (<svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182" /></svg>);
}

function ChevronLeftIcon({ className }: { className?: string }) {
  return (<svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>);
}

function ChevronRightIcon({ className }: { className?: string }) {
  return (<svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>);
}

function ClockIcon() {
  return (<svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>);
}

function TargetIcon() {
  return (<svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="6" /><circle cx="12" cy="12" r="2" /></svg>);
}

function TrendIcon() {
  return (<svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18 9 11.25l4.306 4.306a11.95 11.95 0 0 1 5.814-5.518l2.74-1.22m0 0-5.94-2.281m5.94 2.28-2.28 5.941" /></svg>);
}

function InvoiceIcon() {
  return (<svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>);
}

function BankIcon() {
  return (<svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18.75a60.07 60.07 0 0 1 15.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 0 1 3 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 0 0-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 0 1-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 0 0 3 15h-.75M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm3 0h.008v.008H18V10.5Zm-12 0h.008v.008H6V10.5Z" /></svg>);
}

function BgImageIcon({ className }: { className?: string }) {
  return (<svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5a2.25 2.25 0 0 0 2.25-2.25V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Zm6-13.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z" /></svg>);
}

function EmptyIcon({ className }: { className?: string }) {
  return (<svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" /></svg>);
}
