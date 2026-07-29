import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine, Cell, ComposedChart,
  LineChart, Line, LabelList, Legend,
} from 'recharts';
import { api } from '../api/client';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { parseExcludeVendors, isExcludedVendor } from './creditors/creditors-helpers';

// ── Types ────────────────────────────────────────────

interface KpiOverview {
  bank_balance: number | null;
  bank_account_name: string | null;
  open_invoices_total: number;
  open_invoices_count: number;
  current_month_revenue: number;
  current_month_hours: number;
  forecast_year_revenue: number;
  forecast_year_revenue_runrate: number;
  forecast_year_end_cashflow: number;
  forecast_year_end_runrate: number;
  revenue_gap_to_goal: number;
  annual_revenue_goal: number;
  min_liquidity: number;
  burn_rate: number;
  runway_months: number | null;
  runway_months_incl_debtors: number | null;
  profit_margin_ytd: number | null;
  revenue_ytd: number;
  revenue_ytd_live: number;
  revenue_ytd_closed: number;
  revenue_ytd_net: number;
  revenue_ytd_net_closed: number;
  expenses_ytd: number;
  expenses_ytd_closed: number;
  ebitda_ytd: number | null;
  personalquote_ytd: number | null;
  dso_days: number | null;
  liquiditaet_2: number | null;
  ek_quote: number | null;
  revenue_ytd_prior: number;
  prior_year_revenue: number;
  expenses_ytd_prior: number;
  ebitda_ytd_prior: number | null;
  personalquote_ytd_prior: number | null;
  profit_margin_ytd_prior: number | null;
  closed_until_month: number;
  closed_until_label: string;
  journal_data_from: string | null;
  journal_data_to: string | null;
  as_of_date: string;
  currency: string;
}

interface CashflowSpecialItem {
  label: string;
  amount: number;
}

interface CashflowMonth {
  month: string;
  revenue: number;
  expenses: number;
  personnel_outflow?: number;
  social_outflow?: number;
  pension_outflow?: number;
  tax_outflow?: number;
  fin_outflow: number;
  invest_outflow: number;
  delta: number;
  cumulative: number;
  cumulative_expected?: number;
  is_forecast: boolean;
  special_items: CashflowSpecialItem[];
  forecast_committed?: number;
  forecast_pipeline?: number;
  forecast_fill?: number;
}

interface CashflowResponse {
  months: CashflowMonth[];
  forecast_revenue_monthly: number;
  forecast_expenses_monthly: number;
  start_balance: number;
  annual_revenue_goal: number;
  monthly_revenue_goal: number;
  min_liquidity: number;
}

interface TogglProject {
  project_name: string;
  client_name: string;
  hours: number;
  rate_per_hour: number;
  amount: number;
  currency: string;
}

interface YoyMonth {
  month_label: string;
  month_num: number;
  revenue_current: number;
  revenue_prior: number;
  expenses_current: number;
  expenses_prior: number;
}

interface YoyResponse {
  current_year: number;
  prior_year: number;
  months: YoyMonth[];
  revenue_current_ytd: number;
  revenue_prior_ytd: number;
  growth_pct: number | null;
}

interface WaterfallStep {
  label: string;
  value: number;
  step_type: string;
}

interface WaterfallResponse {
  steps: WaterfallStep[];
  period_label: string;
  revenue_total: number;
  expenses_total: number;
  result: number;
}

interface MarginMonth {
  month: string;
  label: string;
  ytd_margin: number | null;
  rolling_12m_margin: number | null;
  ytd_margin_prior: number | null;
}

interface MarginTrendResponse {
  months: MarginMonth[];
  current_year: number;
  prior_year: number;
}

interface ExpenseMonthRow {
  month: string;
  year: number;
  categories: Record<string, number>;
  total: number;
}

interface ExpenseMonthlyBreakdown {
  current_year: number;
  prior_year: number;
  months_current: ExpenseMonthRow[];
  months_prior: ExpenseMonthRow[];
  category_labels: Record<string, string>;
}

// ── Formatierung ────────────────────────────────────

function formatCHF(value: number | null | undefined): string {
  if (value == null) return '–';
  return new Intl.NumberFormat('de-CH', { style: 'currency', currency: 'CHF' }).format(value);
}

function formatK(value: number): string {
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(0)}k`;
  return value.toFixed(0);
}

// Differenz in Prozentpunkten (fuer Quoten: Marge, Personalquote). null = kein Vergleich.
function ppDelta(current: number | null | undefined, prior: number | null | undefined): number | null {
  if (current == null || prior == null) return null;
  return current - prior;
}

// Relativer %-Trend mit Schutz gegen Mini-Basis-Artefakte (z. B. Vorjahres-EBITDA nahe 0,
// das sonst absurde Werte wie "+1268 %" erzeugt). Liefert null, wenn die Vorjahresbasis
// unwesentlich klein oder das Resultat irrefuehrend gross waere -- dann zeigt die Karte
// stattdessen den absoluten Vorjahreswert im Sublabel.
function relTrend(current: number | null | undefined, prior: number | null | undefined): number | null {
  if (current == null || prior == null || prior === 0) return null;
  if (Math.abs(prior) < 0.15 * Math.abs(current)) return null;  // Basis zu klein -> % irrefuehrend
  const pct = ((current - prior) / Math.abs(prior)) * 100;
  if (Math.abs(pct) > 200) return null;                         // unplausibel gross -> VJ-Wert genuegt
  return pct;
}

// Baut ein KpiTrend: bevorzugt %-Trend, faellt bei Mini-Basis/Extremwert auf das
// absolute CHF-Delta zurueck -- statt gar nichts anzuzeigen. Liefert null nur,
// wenn kein Vergleich moeglich ist (ein Wert fehlt).
function buildRelTrend(
  current: number | null | undefined,
  prior: number | null | undefined,
  goodWhen: 'up' | 'down',
  title: string,
): KpiTrend | null {
  if (current == null || prior == null) return null;
  const pct = relTrend(current, prior);
  if (pct != null) return { value: pct, unit: '%', goodWhen, title };
  return { value: current - prior, unit: 'chf', goodWhen, title };
}

// Leitet eine Kartenfarbe aus der Trendrichtung ab (positiv->gruen, negativ->rot).
function trendStatus(trend: KpiTrend | null): 'green' | 'red' | 'neutral' {
  if (!trend || Math.round(trend.value) === 0) return 'neutral';
  const isUp = trend.value > 0;
  const isGood = (trend.goodWhen ?? 'up') === 'up' ? isUp : !isUp;
  return isGood ? 'green' : 'red';
}

function formatMonthLabel(month: string): string {
  const [y, m] = month.split('-');
  const names = ['', 'Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
  return `${names[parseInt(m)]} ${y.slice(2)}`;
}

const CATEGORY_COLORS = [
  '#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6',
  '#14b8a6', '#f97316', '#ec4899', '#06b6d4', '#84cc16',
  '#d946ef', '#0ea5e9', '#a3e635',
];

const TOOLTIP_STYLE: React.CSSProperties = {
  borderRadius: '0.5rem',
  fontSize: '0.8rem',
  backgroundColor: '#1f2937',
  color: '#f3f4f6',
  border: '1px solid #374151',
  boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
};

const CURSOR_STYLE = { fill: 'rgba(99,102,241,0.06)' };

// ── Hauptkomponente ─────────────────────────────────

export function FinancePage() {
  const [overview, setOverview] = useState<KpiOverview | null>(null);
  const [cashflow, setCashflow] = useState<CashflowResponse | null>(null);
  const [togglProjects, setTogglProjects] = useState<TogglProject[]>([]);
  const [yoy, setYoy] = useState<YoyResponse | null>(null);
  const [waterfall, setWaterfall] = useState<WaterfallResponse | null>(null);
  const [expenseBreakdown, setExpenseBreakdown] = useState<ExpenseMonthlyBreakdown | null>(null);
  const [marginTrend, setMarginTrend] = useState<MarginTrendResponse | null>(null);
  const [pnlPeriod, setPnlPeriod] = useState('ytd');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [creditorsExcludeVendors, setCreditorsExcludeVendors] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const isFinanceMobile = useMediaQuery('(max-width: 1023px)');

  const loadData = useCallback(async (wfPeriod = 'ytd') => {
    setLoading(true);
    setError(null);
    try {
      const [ov, cf, tp, yoyR, wf, eb, mt] = await Promise.allSettled([
        api.get<KpiOverview>('/api/finance/overview'),
        api.get<CashflowResponse>('/api/finance/cashflow?months_back=6&months_forward=12'),
        api.get<TogglProject[]>('/api/finance/toggl-summary'),
        api.get<YoyResponse>('/api/finance/yoy'),
        api.get<WaterfallResponse>(`/api/finance/pnl-waterfall?period=${wfPeriod}`),
        api.get<ExpenseMonthlyBreakdown>('/api/finance/expense-monthly-breakdown'),
        api.get<MarginTrendResponse>('/api/finance/margin-trend'),
      ]);
      if (ov.status === 'fulfilled') setOverview(ov.value);
      if (cf.status === 'fulfilled') setCashflow(cf.value);
      if (tp.status === 'fulfilled') setTogglProjects(tp.value);
      if (yoyR.status === 'fulfilled') setYoy(yoyR.value);
      if (wf.status === 'fulfilled') setWaterfall(wf.value);
      if (eb.status === 'fulfilled') setExpenseBreakdown(eb.value);
      if (mt.status === 'fulfilled') setMarginTrend(mt.value);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Laden fehlgeschlagen');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
    api.get<{ finance_background_url: string | null; creditors_overview_exclude_vendors: string | null }>('/api/settings')
      .then(s => {
        setBgUrl(s.finance_background_url);
        setCreditorsExcludeVendors(s.creditors_overview_exclude_vendors ?? null);
      })
      .catch(() => {});
  }, [loadData]);

  const handleRefresh = async () => {
    try {
      await api.post('/api/finance/cache/clear', {});
      await api.post('/api/bexio/cache/clear', {});
      await api.post('/api/toggl/cache/clear', {});
    } catch { /* ignore */ }
    loadData();
  };

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { finance_background_url: url });
    setBgUrl(url);
  };

  const hasBg = !!bgUrl;
  const isGradient = bgUrl?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient
    ? { background: bgUrl!.slice('gradient:'.length) }
    : hasBg
      ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover' as const, backgroundPosition: 'center' }
      : undefined;

  const currentMonth = new Date().toISOString().slice(0, 7);
  const nextYearJan = `${new Date().getFullYear() + 1}-01`;
  const cardClass = hasBg
    ? 'bg-black/30 backdrop-blur-xl ring-1 ring-white/10'
    : 'border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800/50';
  const sectionClass = `rounded-2xl p-4 sm:p-6 ${cardClass}`;

  // Cashflow-Chart-Daten: Ist-Einnahmen + gesicherte Prognose-Balken
  // (Gebucht / gewichtete Pipeline). Run-Rate, Monatsziel, kumulierter Saldo und
  // Mindest-Liquiditaet werden als Referenzlinien gezeigt, nicht als Balken.
  const cashflowChartData = useMemo(() => {
    if (!cashflow) return [];
    const runRate = cashflow.forecast_revenue_monthly || 0;
    const monthlyGoal = cashflow.monthly_revenue_goal || 0;
    const minLiq = cashflow.min_liquidity || 0;
    return cashflow.months.map(m => {
      const isCurrent = m.month === currentMonth;
      // Laufender Monat: bereits erfasst (revActual) + erwarteter Rest (fcFill)
      return {
        ...m,
        label: formatMonthLabel(m.month),
        revActual: m.is_forecast ? 0 : (isCurrent ? (m.forecast_committed ?? m.revenue) : m.revenue),
        committed: m.is_forecast ? (m.forecast_committed || 0) : 0,
        pipeline: m.is_forecast ? (m.forecast_pipeline || 0) : 0,
        fcFill: m.is_forecast ? 0 : (isCurrent ? (m.forecast_fill || 0) : 0),
        // Run-Rate-Referenz nur ueber laufenden Monat + Prognosehorizont
        runRate: (m.is_forecast || isCurrent) ? runRate : null,
        goalLine: monthlyGoal > 0 ? monthlyGoal : null,
        cumulative: m.cumulative,
        // Erwarteter Saldo (Run-Rate): primaere Liquiditaetslinie; faellt nicht
        // kuenstlich ab, weil Prognosemonate auf die Run-Rate aufgefuellt werden.
        cumulativeExpected: m.cumulative_expected ?? m.cumulative,
        minLiq: minLiq > 0 ? minLiq : null,
        // Auszahlungs-Buckets (negativ gestapelt): Liquiditaets-Timing.
        personnelNeg: -(m.personnel_outflow || 0),
        socialNeg: -(m.social_outflow || 0),
        pensionNeg: -(m.pension_outflow || 0),
        taxNeg: -(m.tax_outflow || 0),
        opNeg: -m.expenses,
        finNeg: -(m.fin_outflow || 0),
        invNeg: -(m.invest_outflow || 0),
        hasSpecial: (m.special_items?.length ?? 0) > 0,
        // Unsichtbarer Anker, damit das reine Linien-Panel (Banksaldo) dieselbe
        // Band-Skala wie das Balken-Panel oben nutzt -> X-Achse deckungsgleich.
        __axisAnchor: 0,
      };
    });
  }, [cashflow, currentMonth]);

  // Auszahlungs-Buckets (Liquiditaets-Timing): Ist solide, Prognose transparent + gestrichelt.
  const renderCostCells = (
    dataKey: 'personnelNeg' | 'socialNeg' | 'pensionNeg' | 'taxNeg' | 'opNeg' | 'finNeg' | 'invNeg',
    color: string,
  ) =>
    cashflowChartData.map((row, i) => {
      const val = (row as unknown as Record<string, number>)[dataKey] ?? 0;
      const visible = val !== 0;
      return (
        <Cell
          key={i}
          fill={visible ? color : 'transparent'}
          fillOpacity={row.is_forecast ? 0.42 : 1}
          stroke={visible && row.is_forecast ? color : undefined}
          strokeDasharray={row.is_forecast ? '3 2' : undefined}
        />
      );
    });

  // Stapel-Segmente in Zeichenreihenfolge: Einnahmen unten -> oben, Kosten 0 -> abwaerts.
  // Das Total-Label haengt jeweils am letzten nicht-leeren Segment (= oberste bzw.
  // unterste sichtbare Kante), damit es fuer Vergangenheit, laufenden Monat und
  // Prognose zuverlaessig an der richtigen Position rendert (0-Hoehe-Segmente liefern
  // in recharts keine brauchbaren Koordinaten).
  const REVENUE_STACK_KEYS = ['revActual', 'committed', 'pipeline', 'fcFill'] as const;
  const COST_STACK_KEYS = ['personnelNeg', 'socialNeg', 'pensionNeg', 'taxNeg', 'opNeg', 'finNeg', 'invNeg'] as const;

  const lastNonZeroKey = (row: Record<string, number>, keys: readonly string[]): string | null => {
    let found: string | null = null;
    for (const k of keys) {
      if ((row[k] ?? 0) !== 0) found = k;
    }
    return found;
  };

  // Gesamt-Umsatz-Label oberhalb des obersten sichtbaren Einnahmen-Segments.
  // recharts liefert bei positiven Balken y = obere Kante; zur Sicherheit nehmen wir
  // die kleinste y-Koordinate (oberste Kante) unabhaengig vom Vorzeichen der height.
  const renderRevenueTotalLabel = (dataKey: typeof REVENUE_STACK_KEYS[number]) => (
    <LabelList
      content={(props: { x?: string | number; y?: string | number; width?: string | number; height?: string | number; index?: number }) => {
        const index = props.index ?? 0;
        const row = cashflowChartData[index] as unknown as Record<string, number>;
        if (!row || !row.revenue || row.revenue <= 0) return null;
        if (lastNonZeroKey(row, REVENUE_STACK_KEYS) !== dataKey) return null;
        const x = Number(props.x ?? 0);
        const y = Number(props.y ?? 0);
        const width = Number(props.width ?? 0);
        const height = Number(props.height ?? 0);
        const topY = Math.min(y, y + height);
        return (
          <text x={x + width / 2} y={topY - 4} textAnchor="middle" fontSize={9} fill="#16a34a">
            {formatK(row.revenue)}
          </text>
        );
      }}
    />
  );

  // Gesamt-Kosten-Label unterhalb des untersten sichtbaren Auszahlungs-Segments.
  // Wichtig: bei negativen Balken ist y bereits die UNTERE Kante und height negativ,
  // darum die groesste y-Koordinate verwenden (sonst landet das Label im Balken).
  const renderCostTotalLabel = (dataKey: typeof COST_STACK_KEYS[number]) => (
    <LabelList
      content={(props: { x?: string | number; y?: string | number; width?: string | number; height?: string | number; index?: number }) => {
        const index = props.index ?? 0;
        const row = cashflowChartData[index] as unknown as Record<string, number>;
        if (!row || lastNonZeroKey(row, COST_STACK_KEYS) !== dataKey) return null;
        const total = (row.personnel_outflow ?? 0) + (row.social_outflow ?? 0) + (row.pension_outflow ?? 0)
          + (row.tax_outflow ?? 0) + (row.expenses ?? 0) + (row.fin_outflow ?? 0) + (row.invest_outflow ?? 0);
        if (total <= 0) return null;
        const x = Number(props.x ?? 0);
        const y = Number(props.y ?? 0);
        const width = Number(props.width ?? 0);
        const height = Number(props.height ?? 0);
        const bottomY = Math.max(y, y + height);
        return (
          <text x={x + width / 2} y={bottomY + 12} textAnchor="middle" fontSize={9} fill="#dc2626">
            -{formatK(total)}
          </text>
        );
      }}
    />
  );

  // marginTrend kommt direkt vom Backend

  // Stacked-Bar-Daten für Kostenstruktur
  const costBarData = useMemo(() => {
    if (!expenseBreakdown) return { data: [], keys: [] as string[], labels: {} as Record<string, string> };
    const labels = expenseBreakdown.category_labels;
    const allKeys = new Set<string>();
    const combined: Record<string, number | string>[] = [];
    for (let m = 0; m < 12; m++) {
      const cur = expenseBreakdown.months_current[m];
      const prior = expenseBreakdown.months_prior[m];
      if (!cur && !prior) continue;
      const curCats = cur?.categories || {};
      const priorCats = prior?.categories || {};
      Object.keys(curCats).forEach(k => allKeys.add(k));
      Object.keys(priorCats).forEach(k => allKeys.add(k));

      const row: Record<string, number | string> = {
        month: m,
        _label_cur: cur?.month ? `${cur.month} ${String(expenseBreakdown.current_year).slice(2)}` : '',
        _label_prior: prior?.month ? `${prior.month} ${String(expenseBreakdown.prior_year).slice(2)}` : '',
        _total_cur: cur?.total || 0,
        _total_prior: prior?.total || 0,
      };
      Object.keys(curCats).forEach(k => { row[`cur_${k}`] = curCats[k]; });
      Object.keys(priorCats).forEach(k => { row[`prior_${k}`] = priorCats[k]; });
      combined.push(row);
    }
    const sortedKeys = [...allKeys].sort((a, b) => {
      const sumA = expenseBreakdown.months_current.reduce((s, m) => s + (m.categories[a] || 0), 0);
      const sumB = expenseBreakdown.months_current.reduce((s, m) => s + (m.categories[b] || 0), 0);
      return sumB - sumA;
    });
    return { data: combined, keys: sortedKeys, labels };
  }, [expenseBreakdown]);

  // Waterfall-Chart-Daten aufbereiten
  const waterfallChartData = useMemo(() => {
    if (!waterfall) return [];
    let running = 0;
    return waterfall.steps.map(step => {
      if (step.step_type === 'income') {
        const d = { name: step.label, base: 0, bar: step.value, value: step.value, type: step.step_type };
        running = step.value;
        return d;
      } else if (step.step_type === 'expense') {
        const absVal = Math.abs(step.value);
        running -= absVal;
        return { name: step.label, base: running, bar: absVal, value: step.value, type: step.step_type };
      } else {
        return { name: step.label, base: 0, bar: running, value: running, type: step.step_type };
      }
    });
  }, [waterfall]);

  return (
    <div className="relative flex h-full flex-col" style={hasBg ? bgStyle : undefined}>
      {!hasBg && <div className="absolute inset-0 bg-gradient-to-br from-slate-50 via-white to-indigo-50/30 dark:from-gray-950 dark:via-gray-900 dark:to-indigo-950/20" />}
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/25 dark:bg-black/40" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/25" />}

      <div className="relative z-10 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
        {/* Header */}
        <div className="mb-6 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between lg:gap-2">
          <div className="min-w-0">
            {/* Titel liefert mobil der MobileHeader — hier würde er nur doppelt erscheinen. */}
            <h1 className={`hidden text-xl font-bold lg:block lg:text-2xl ${hasBg ? 'text-white drop-shadow-sm' : 'text-gray-900 dark:text-white'}`}>Finanz-Controlling</h1>
            {overview && (
              <p className={`mt-1 text-xs ${hasBg ? 'text-white/60' : 'text-gray-400 dark:text-gray-500'}`}>
                Datenstand: Journal{' '}
                {overview.journal_data_from && overview.journal_data_to
                  ? `${formatMonthLabel(overview.journal_data_from.slice(0, 7))} – ${formatMonthLabel(overview.journal_data_to.slice(0, 7))}`
                  : '–'}
                {` · Toggl: live · Aktualisiert: ${new Date().toLocaleString('de-CH')}`}
              </p>
            )}
            {!overview && (
              <p className={`mt-1 text-sm ${hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'}`}>
                Cashflow-Übersicht, Prognosen und Kostenanalyse
              </p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2 lg:flex-nowrap">
            <Link
              to="/finanzen/analysen"
              className={`flex min-h-11 items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 ${hasBg ? 'bg-indigo-500/80 text-white hover:bg-indigo-500 backdrop-blur-sm' : 'border border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100 dark:border-indigo-800 dark:bg-indigo-950/40 dark:text-indigo-300 dark:hover:bg-indigo-900/40'}`}
              title="KI-gestützte Finanz- und Treuhandanalysen"
            >
              <Sparkles className="h-4 w-4" />
              KI-Analysen
            </Link>
            <a
              href="https://office.bexio.com"
              target="_blank"
              rel="noopener noreferrer"
              className={`flex min-h-11 items-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20 backdrop-blur-sm' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'}`}
              title="Bexio Buchhaltung öffnen"
            >
              Bexio öffnen
              <ExternalLinkIcon className="h-3.5 w-3.5" />
            </a>
            <button
              onClick={() => setBgPickerOpen(true)}
              className={`flex min-h-11 min-w-11 items-center justify-center rounded-lg p-2 transition-colors lg:min-h-0 lg:min-w-0 ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`}
              title="Hintergrund ändern"
            >
              <BgImageIcon className="h-5 w-5" />
            </button>
            <button
              onClick={handleRefresh}
              disabled={loading}
              className={`flex min-h-11 items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20 backdrop-blur-sm' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'}`}
            >
              <RefreshIcon className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              Aktualisieren
            </button>
          </div>
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
            {error}
          </div>
        )}

        {/* KPI-Leiste: 12 Karten in 3 beschrifteten Gruppen (je 4 Kacheln)
            Gruppe 1: Heute / Bestand
            Gruppe 2: Prognose Jahresende (Umsatz- und Cashflow-Paar, je gesichert → erwartet)
            Gruppe 3: Ergebnis YTD */}
        <div className="mb-6 space-y-4">
          {/* Gruppe 1 — Heute / Bestand */}
          <div>
            <p className={`mb-2 text-[11px] font-semibold uppercase tracking-wider ${hasBg ? 'text-white/60' : 'text-gray-400 dark:text-gray-500'}`}>Heute / Bestand</p>
            <div className="grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-3 lg:grid-cols-4">
              <KpiCard
                label="Banksaldo"
                value={formatCHF(overview?.bank_balance)}
                sublabel={overview?.bank_account_name || ''}
                icon={<BankIcon />}
                status={overview?.runway_months != null
                  ? (overview.runway_months > 6 ? 'green' : overview.runway_months > 2 ? 'yellow' : 'red')
                  : 'neutral'}
              />
              <KpiCard
                label="Offene Debitoren"
                value={formatCHF(overview?.open_invoices_total)}
                sublabel={overview?.dso_days != null ? `DSO: ${overview.dso_days} Tage` : `${overview?.open_invoices_count ?? 0} Rechnungen`}
                icon={<InvoiceIcon />}
                info="Summe der noch nicht bezahlten Kundenrechnungen. DSO (Days Sales Outstanding) ist die durchschnittliche Zahlungsdauer in Tagen – je tiefer, desto schneller fliesst das Geld."
                status={overview?.dso_days != null
                  ? (overview.dso_days <= 30 ? 'green' : overview.dso_days <= 60 ? 'yellow' : 'red')
                  : 'neutral'}
              />
              <KpiCard
                label="Runway"
                value={overview?.runway_months != null ? `${overview.runway_months} Mt.` : '–'}
                sublabel={overview?.runway_months_incl_debtors != null
                  ? `inkl. Debitoren: ${overview.runway_months_incl_debtors} Mt.`
                  : `Burn Rate: ${formatCHF(overview?.burn_rate)}/Mt.`}
                icon={<RunwayIcon />}
                info="Wie viele Monate die liquiden Mittel beim aktuellen monatlichen Mittelabfluss (Burn Rate) noch reichen, falls kein neuer Umsatz dazukommt."
                status={overview?.runway_months != null
                  ? (overview.runway_months > 6 ? 'green' : overview.runway_months > 2 ? 'yellow' : 'red')
                  : 'neutral'}
              />
              <KpiCard
                label="Lfd. Monat (Toggl)"
                value={formatCHF(overview?.current_month_revenue)}
                sublabel={`${overview?.current_month_hours ?? 0}h erfasst`}
                icon={<ClockIcon />}
                status="neutral"
              />
            </div>
          </div>

          {/* Gruppe 2 — Prognose Jahresende (erwartet/Run-Rate als Hauptwert → Worst Case als Floor) */}
          <div>
            <p className={`mb-2 text-[11px] font-semibold uppercase tracking-wider ${hasBg ? 'text-white/60' : 'text-gray-400 dark:text-gray-500'}`}>Prognose Jahresende</p>
            <div className="grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-3 lg:grid-cols-4">
              <KpiCard
                label="Prog. Jahresumsatz (erwartet)"
                value={formatCHF(overview?.forecast_year_revenue_runrate)}
                trend={buildRelTrend(overview?.forecast_year_revenue_runrate, overview?.prior_year_revenue, 'up', `Prognose vs. Vorjahres-Gesamtumsatz (${formatCHF(overview?.prior_year_revenue)})`)}
                sublabel={(overview?.annual_revenue_goal ?? 0) > 0
                  ? (((overview!.annual_revenue_goal) - (overview!.forecast_year_revenue_runrate)) > 0
                    ? `Ziel ${formatK(overview!.annual_revenue_goal)} · Lücke ${formatCHF((overview!.annual_revenue_goal) - (overview!.forecast_year_revenue_runrate))}`
                    : `Ziel ${formatK(overview!.annual_revenue_goal)} · erreicht ✓`)
                  : ((overview?.prior_year_revenue ?? 0) > 0
                    ? `realistisch · VJ-Total ${formatCHF(overview!.prior_year_revenue)}`
                    : 'realistisch · inkl. erwarteter Auffüllung (Ø 3/12 Mt.)')}
                icon={<TrendIcon />}
                info="Realistisches Szenario: bereits verbuchter Umsatz, gebuchte Kapazität plus erwartete Auffüllung noch freier Kapazität auf Basis der Run-Rate (gewichteter Ø der letzten 3 und 12 Monate). Bei einem Geschäftsverlauf wie in den Vorjahren der massgebliche Erwartungswert. Trend: Prognose vs. Vorjahres-Gesamtumsatz."
                status={(overview?.annual_revenue_goal ?? 0) > 0
                  ? (((overview!.annual_revenue_goal) - (overview!.forecast_year_revenue_runrate)) <= 0 ? 'green' : ((overview!.annual_revenue_goal) - (overview!.forecast_year_revenue_runrate)) < overview!.annual_revenue_goal * 0.25 ? 'yellow' : 'red')
                  : (overview?.forecast_year_revenue_runrate && overview.forecast_year_revenue_runrate > 0 ? 'green' : 'neutral')}
              />
              <KpiCard
                label="Prog. Jahresumsatz (Worst Case)"
                value={formatCHF(overview?.forecast_year_revenue)}
                sublabel="Floor: nur gebuchte Kapazität + Pipeline, keine neue Akquisition"
                icon={<TrendIcon />}
                info="Unterer Grenzwert (Worst Case): nur bereits verbuchter Umsatz plus bestätigte und gewichtete Kapazitäts-Buchungen. Unterstellt KEINE weitere Akquisition bis Jahresende – seit 5 Jahren nie eingetreten, dient als konservativer Floor."
                status="neutral"
              />
              <KpiCard
                label="Cashflow Ende Jahr (erwartet)"
                value={formatCHF(overview?.forecast_year_end_runrate)}
                sublabel="realistisch · Banksaldo per 31.12. bei gehaltener Run-Rate"
                icon={<TrendIcon />}
                info="Voraussichtlicher Banksaldo per 31.12. im realistischen Szenario: heutiger Saldo plus erwartete Einnahmen (Run-Rate, Ø 3/12 Mt.) abzüglich der erwarteten Ausgaben. Massgeblicher Erwartungswert bei normalem Geschäftsverlauf."
                status={overview?.forecast_year_end_runrate != null
                  ? ((overview.min_liquidity > 0 && overview.forecast_year_end_runrate < overview.min_liquidity)
                    ? 'red'
                    : (overview.forecast_year_end_runrate > 0 ? 'green' : 'red'))
                  : 'neutral'}
              />
              <KpiCard
                label="Cashflow Ende Jahr (Worst Case)"
                value={formatCHF(overview?.forecast_year_end_cashflow)}
                sublabel="Floor: keine neue Akquisition, voller Aufwand inkl. Inhaberlohn"
                icon={<TrendIcon />}
                info="Unterer Grenzwert (Worst Case): heutiger Saldo plus NUR gesicherte Einnahmen, abzüglich voller Ausgaben – ohne jede weitere Akquisition. Der grösste Aufwandsblock ist der frei steuerbare Inhaberlohn, der bei Auftragseinbruch sofort reduzierbar wäre. Daher kein Insolvenz-, sondern ein Steuerungssignal."
                status={overview?.forecast_year_end_cashflow != null
                  ? (overview.forecast_year_end_cashflow < 0 ? 'yellow' : 'neutral')
                  : 'neutral'}
              />
            </div>
          </div>

          {/* Gruppe 3 — Ergebnis YTD */}
          <div>
            <p className={`mb-2 text-[11px] font-semibold uppercase tracking-wider ${hasBg ? 'text-white/60' : 'text-gray-400 dark:text-gray-500'}`}>Ergebnis YTD</p>
            <div className="grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-3 lg:grid-cols-4">
              <KpiCard
                label="Umsatz YTD"
                value={formatCHF(overview?.revenue_ytd_live)}
                trend={buildRelTrend(overview?.revenue_ytd_closed, overview?.revenue_ytd_prior, 'up', `vs. Vorjahr, stichtagsgleich${overview?.closed_until_label ? ` (per Ende ${overview.closed_until_label})` : ''}`)}
                sublabel={`netto ${formatCHF(overview?.revenue_ytd_net)}${overview?.closed_until_label ? ` · VJ per Ende ${overview.closed_until_label}` : ''}`}
                icon={<InvoiceIcon />}
                info={`Live-Sicht: abgeschlossene Monate plus geschätzter laufender Monat (früh aus Kapazitätsplanung, im Verlauf aus Toggl). Der Vorjahresvergleich (Trend) wird stichtagsgleich nur über abgeschlossene Monate gerechnet${overview?.closed_until_label ? ` (per Ende ${overview.closed_until_label})` : ''}, da der laufende Monat wegen Monatsend-Fakturierung unvollständig ist. Brutto inkl., netto exkl. MWST.`}
                status={(overview?.revenue_ytd_live ?? 0) > 0
                  ? (trendStatus(buildRelTrend(overview?.revenue_ytd_closed, overview?.revenue_ytd_prior, 'up', '')) === 'red' ? 'yellow' : 'green')
                  : 'neutral'}
              />
              <KpiCard
                label="EBITDA YTD"
                value={formatCHF(overview?.ebitda_ytd)}
                trend={buildRelTrend(overview?.ebitda_ytd, overview?.ebitda_ytd_prior, 'up', 'vs. Vorjahr, stichtagsgleich')}
                sublabel={overview?.ebitda_ytd_prior != null
                  ? `VJ: ${formatCHF(overview.ebitda_ytd_prior)}`
                  : overview?.revenue_ytd_net_closed
                    ? `${((overview.ebitda_ytd ?? 0) / overview.revenue_ytd_net_closed * 100).toFixed(1)}% Marge`
                    : '–'}
                icon={<CashflowIcon />}
                info={`Betriebsergebnis vor Zinsen, Steuern und Abschreibungen – zeigt die operative Ertragskraft ohne Finanzierungs- und Buchungseffekte. Basis: abgeschlossene Monate${overview?.closed_until_label ? ` (per Ende ${overview.closed_until_label})` : ''}.`}
                status={overview?.ebitda_ytd != null
                  ? (overview.ebitda_ytd > 0 ? 'green' : 'red')
                  : 'neutral'}
              />
              <KpiCard
                label="Gewinnmarge YTD"
                value={overview?.profit_margin_ytd != null ? `${overview.profit_margin_ytd}%` : '–'}
                trend={overview?.profit_margin_ytd_prior != null
                  ? { value: ppDelta(overview?.profit_margin_ytd, overview.profit_margin_ytd_prior) ?? 0, unit: 'pp', goodWhen: 'up', title: 'vs. Vorjahr (Prozentpunkte), stichtagsgleich' }
                  : null}
                sublabel={overview?.profit_margin_ytd_prior != null
                  ? `VJ: ${overview.profit_margin_ytd_prior}%`
                  : `Aufwand YTD: ${formatCHF(overview?.expenses_ytd_closed)}`}
                icon={<CashflowIcon />}
                info={`Anteil des Nettoumsatzes, der nach Abzug aller Aufwände als Gewinn übrig bleibt. Höher ist besser. Trend in Prozentpunkten (%-Pkt.) vs. Vorjahr. Basis: abgeschlossene Monate${overview?.closed_until_label ? ` (per Ende ${overview.closed_until_label})` : ''}.`}
                status={overview?.profit_margin_ytd != null
                  ? (overview.profit_margin_ytd >= 10 ? 'green' : overview.profit_margin_ytd >= 0 ? 'yellow' : 'red')
                  : 'neutral'}
              />
              <KpiCard
                label="Personalquote"
                value={overview?.personalquote_ytd != null ? `${overview.personalquote_ytd}%` : '–'}
                trend={overview?.personalquote_ytd_prior != null
                  ? { value: ppDelta(overview?.personalquote_ytd, overview.personalquote_ytd_prior) ?? 0, unit: 'pp', goodWhen: 'down', title: 'vs. Vorjahr (Prozentpunkte); tiefer ist besser' }
                  : null}
                sublabel={overview?.personalquote_ytd_prior != null
                  ? `VJ: ${overview.personalquote_ytd_prior}%`
                  : 'Personalaufwand / Netto-Ertrag'}
                icon={<ClockIcon />}
                info={`Personalaufwand im Verhältnis zum Nettoumsatz. Zeigt, wie viel vom Umsatz für Löhne und Sozialleistungen aufgeht – ein tieferer Wert bedeutet höhere Effizienz. Trend in Prozentpunkten (%-Pkt.); tiefer ist besser. Basis: abgeschlossene Monate${overview?.closed_until_label ? ` (per Ende ${overview.closed_until_label})` : ''}.`}
                status={overview?.personalquote_ytd != null
                  ? (overview.personalquote_ytd <= 70 ? 'green' : overview.personalquote_ytd <= 85 ? 'yellow' : 'red')
                  : 'neutral'}
              />
            </div>
          </div>
        </div>

        {/* Cashflow: Zwei-Panel (oben Einnahmen/Auszahlungen, unten Banksaldo) */}
        {cashflow && cashflow.months.length > 0 && (
          <div className={`mb-6 ${sectionClass}`}>
            <div className="mb-1 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                Liquiditätsplanung &amp; Cashflow
              </h2>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                Zahlungs-Timing (cash-basis) – wann Geld das Konto verlässt
              </span>
            </div>
            {/* ── Panel A: Einnahmen vs. Auszahlungen nach Liquiditäts-Bucket ── */}
            <div className="rounded-xl bg-white/95 p-2 dark:bg-gray-900/90">
            <ResponsiveContainer width="100%" height={isFinanceMobile ? 280 : 360}>
              <ComposedChart data={cashflowChartData} margin={{ top: 16, right: 16, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#cbd5e1" className="dark:opacity-20" />
                <XAxis dataKey="label" tick={{ fontSize: isFinanceMobile ? 9 : 11, fill: '#475569' }} interval={isFinanceMobile ? 1 : 0} angle={isFinanceMobile ? -45 : 0} textAnchor={isFinanceMobile ? 'end' : 'middle'} height={isFinanceMobile ? 50 : 30} />
                <YAxis yAxisId="left" width={52} tick={{ fontSize: 11, fill: '#475569' }} tickFormatter={(v: number) => `${formatK(v)}`} />
                <Tooltip
                  cursor={CURSOR_STYLE}
                  content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null;
                    const d = payload[0]?.payload;
                    if (!d) return null;
                    const items = d.special_items || [];
                    const fc = d.is_forecast;
                    return (
                      <div className="rounded-lg border border-gray-700 bg-gray-800 p-3 text-xs text-gray-100 shadow-lg">
                        <p className="mb-2 font-semibold text-white">{label}</p>
                        <div className="space-y-1">
                          <div className="flex justify-between gap-4">
                            <span className="text-green-400">{fc ? 'Gesicherter Umsatz (brutto)' : 'Einnahmen (brutto)'}</span>
                            <span className="font-medium">{formatCHF(d.revenue)}</span>
                          </div>
                          {fc && (
                            <div className="space-y-0.5 pl-2 text-[10px] text-gray-400">
                              <div className="flex justify-between gap-4">
                                <span>· Gebucht (Kapazität)</span>
                                <span>{formatCHF(d.forecast_committed || 0)}</span>
                              </div>
                              <div className="flex justify-between gap-4">
                                <span>· Pipeline (gewichtet)</span>
                                <span>{formatCHF(d.forecast_pipeline || 0)}</span>
                              </div>
                              {d.runRate != null && (
                                <div className="flex justify-between gap-4 text-emerald-300/80">
                                  <span>· Run-Rate (Ø, Referenz)</span>
                                  <span>{formatCHF(d.runRate)}</span>
                                </div>
                              )}
                            </div>
                          )}
                          {!fc && d.month === currentMonth && (d.forecast_fill || 0) > 0 && (
                            <div className="space-y-0.5 pl-2 text-[10px] text-gray-400">
                              <div className="flex justify-between gap-4">
                                <span>· Bereits erfasst</span>
                                <span>{formatCHF(d.forecast_committed || 0)}</span>
                              </div>
                              <div className="flex justify-between gap-4">
                                <span>· Erwarteter Rest (Kapazität)</span>
                                <span>{formatCHF(d.forecast_fill || 0)}</span>
                              </div>
                            </div>
                          )}
                          <hr className="my-1 border-gray-600" />
                          <p className="text-[10px] font-medium text-gray-400">Auszahlungen{fc ? ' (Prognose)' : ''}:</p>
                          {(d.personnel_outflow || 0) > 0 && (
                            <div className="flex justify-between gap-4">
                              <span style={{ color: '#dc2626' }}>Personal</span>
                              <span className="font-medium">-{formatCHF(d.personnel_outflow)}</span>
                            </div>
                          )}
                          {(d.social_outflow || 0) > 0 && (
                            <div className="flex justify-between gap-4">
                              <span style={{ color: '#ea580c' }}>Sozialversicherungen</span>
                              <span className="font-medium">-{formatCHF(d.social_outflow)}</span>
                            </div>
                          )}
                          {(d.pension_outflow || 0) > 0 && (
                            <div className="flex justify-between gap-4">
                              <span style={{ color: '#0d9488' }}>Pensionskasse (BVG)</span>
                              <span className="font-medium">-{formatCHF(d.pension_outflow)}</span>
                            </div>
                          )}
                          {(d.tax_outflow || 0) > 0 && (
                            <div className="flex justify-between gap-4">
                              <span style={{ color: '#d97706' }}>MWST/Steuern</span>
                              <span className="font-medium">-{formatCHF(d.tax_outflow)}</span>
                            </div>
                          )}
                          {(d.expenses || 0) > 0 && (
                            <div className="flex justify-between gap-4">
                              <span style={{ color: '#f87171' }}>Übrige operativ</span>
                              <span className="font-medium">-{formatCHF(d.expenses)}</span>
                            </div>
                          )}
                          {d.fin_outflow > 0 && (
                            <div className="flex justify-between gap-4">
                              <span style={{ color: '#9333ea' }}>Finanzierung{fc ? ' (Ø 12 Mt.)' : ''}</span>
                              <span className="font-medium">-{formatCHF(d.fin_outflow)}</span>
                            </div>
                          )}
                          {d.invest_outflow > 0 && (
                            <div className="flex justify-between gap-4">
                              <span style={{ color: '#64748b' }}>Investitionen{fc ? ' (Ø 12 Mt.)' : ''}</span>
                              <span className="font-medium">-{formatCHF(d.invest_outflow)}</span>
                            </div>
                          )}
                          {items.length > 0 && (
                            <>
                              <hr className="my-1 border-gray-600" />
                              <p className="text-[10px] font-medium text-gray-400">Sonderposten:</p>
                              {items.map((si: CashflowSpecialItem, idx: number) => (
                                <div key={idx} className="flex justify-between gap-4 text-[11px]">
                                  <span className="text-gray-400">{si.label}</span>
                                  <span className="font-medium text-orange-400">{formatCHF(si.amount)}</span>
                                </div>
                              ))}
                            </>
                          )}
                          <hr className="my-1 border-gray-600" />
                          <div className="flex justify-between gap-4 font-semibold">
                            <span className={d.delta >= 0 ? 'text-green-400' : 'text-red-400'}>Delta</span>
                            <span>{d.delta >= 0 ? '+' : ''}{formatCHF(d.delta)}</span>
                          </div>
                        </div>
                      </div>
                    );
                  }}
                />
                <ReferenceLine yAxisId="left" y={0} stroke="#94a3b8" />
                <ReferenceLine
                  yAxisId="left"
                  x={formatMonthLabel(currentMonth)}
                  stroke="#6366f1"
                  strokeWidth={2}
                  strokeDasharray="6 3"
                  label={{ value: '▼ Heute', fontSize: 11, fill: '#6366f1', position: 'top' }}
                />
                {cashflow.months.some(m => m.month === nextYearJan) && (
                  <ReferenceLine
                    yAxisId="left"
                    x={formatMonthLabel(nextYearJan)}
                    stroke="#94a3b8"
                    strokeWidth={2}
                    strokeDasharray="6 3"
                    label={{ value: '▼ Jahreswechsel', fontSize: 11, fill: '#94a3b8', position: 'top' }}
                  />
                )}
                {/* Einnahmen: Ist (solide) + gesicherte Prognose-Schichten (gestapelt).
                    Das Umsatz-Total-Label haengt am jeweils obersten nicht-leeren Segment,
                    damit Vergangenheit, laufender Monat UND Prognose den Wert zeigen. */}
                <Bar yAxisId="left" dataKey="revActual" name="revActual" stackId="in" fill="#22c55e" radius={[4, 4, 0, 0]} isAnimationActive={false}>{renderRevenueTotalLabel('revActual')}</Bar>
                <Bar yAxisId="left" dataKey="committed" name="committed" stackId="in" fill="#16a34a" isAnimationActive={false}>{renderRevenueTotalLabel('committed')}</Bar>
                <Bar yAxisId="left" dataKey="pipeline" name="pipeline" stackId="in" fill="#4ade80" radius={[4, 4, 0, 0]} isAnimationActive={false}>{renderRevenueTotalLabel('pipeline')}</Bar>
                <Bar yAxisId="left" dataKey="fcFill" name="fcFill" stackId="in" fill="#bbf7d0" stroke="#86efac" strokeDasharray="4 2" radius={[4, 4, 0, 0]} isAnimationActive={false}>{renderRevenueTotalLabel('fcFill')}</Bar>
                {/* Auszahlungen nach Liquiditäts-Bucket (negativ gestapelt). Das Kosten-Total-Label
                    haengt am jeweils untersten nicht-leeren Segment (Vergangenheit + Prognose). */}
                <Bar yAxisId="left" dataKey="personnelNeg" name="personnelNeg" stackId="out">{renderCostCells('personnelNeg', '#dc2626')}{renderCostTotalLabel('personnelNeg')}</Bar>
                <Bar yAxisId="left" dataKey="socialNeg" name="socialNeg" stackId="out">{renderCostCells('socialNeg', '#ea580c')}{renderCostTotalLabel('socialNeg')}</Bar>
                <Bar yAxisId="left" dataKey="pensionNeg" name="pensionNeg" stackId="out">{renderCostCells('pensionNeg', '#0d9488')}{renderCostTotalLabel('pensionNeg')}</Bar>
                <Bar yAxisId="left" dataKey="taxNeg" name="taxNeg" stackId="out">{renderCostCells('taxNeg', '#d97706')}{renderCostTotalLabel('taxNeg')}</Bar>
                <Bar yAxisId="left" dataKey="opNeg" name="opNeg" stackId="out">{renderCostCells('opNeg', '#f87171')}{renderCostTotalLabel('opNeg')}</Bar>
                <Bar yAxisId="left" dataKey="finNeg" name="finNeg" stackId="out">{renderCostCells('finNeg', '#9333ea')}{renderCostTotalLabel('finNeg')}</Bar>
                <Bar yAxisId="left" dataKey="invNeg" name="invNeg" stackId="out">{renderCostCells('invNeg', '#64748b')}{renderCostTotalLabel('invNeg')}</Bar>
                {/* Run-Rate (Ø Monatsumsatz) als Orientierungslinie */}
                <Line
                  yAxisId="left"
                  dataKey="runRate"
                  name="Run-Rate (Ø)"
                  stroke="#15803d"
                  strokeWidth={2}
                  strokeDasharray="8 4"
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
                {(cashflow.monthly_revenue_goal || 0) > 0 && (
                  <Line
                    yAxisId="left"
                    dataKey="goalLine"
                    name="Monatsziel"
                    stroke="#7c3aed"
                    strokeWidth={2}
                    strokeDasharray="3 3"
                    dot={false}
                    connectNulls
                    isAnimationActive={false}
                  />
                )}
              </ComposedChart>
            </ResponsiveContainer>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-gray-600 dark:text-gray-300">
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded bg-green-500" /> Einnahmen (Ist)</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#16a34a' }} /> Gebucht</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#4ade80' }} /> Pipeline</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#dc2626' }} /> Personal</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#ea580c' }} /> Sozialversicherungen</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#0d9488' }} /> Pensionskasse (BVG)</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#d97706' }} /> MWST/Steuern</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#f87171' }} /> Übrige operativ</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#9333ea' }} /> Finanzierung</span>
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded" style={{ background: '#64748b' }} /> Investitionen</span>
              <span className="flex items-center gap-1"><span className="inline-block h-0.5 w-4 border-t-2 border-dashed" style={{ borderColor: '#15803d' }} /> Run-Rate (Ø)</span>
              {(cashflow.monthly_revenue_goal || 0) > 0 && (
                <span className="flex items-center gap-1"><span className="inline-block h-0.5 w-4 border-t-2 border-dotted" style={{ borderColor: '#7c3aed' }} /> Monatsziel</span>
              )}
              <span className="flex items-center gap-1"><span className="inline-block h-3 w-3 rounded border border-dashed border-gray-400 opacity-50" /> Transparent = Prognose</span>
            </div>

            {/* ── Panel B: Banksaldo-Verlauf (erwartet vs. gesichert) ── */}
            <div className="mt-4 mb-1 flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-200">Banksaldo-Verlauf</h3>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                Liquiditätsverlauf: erwartet vs. gesichert (Worst Case)
              </span>
            </div>
            <div className="rounded-xl bg-white/95 p-2 dark:bg-gray-900/90">
            <ResponsiveContainer width="100%" height={isFinanceMobile ? 160 : 200}>
              <ComposedChart data={cashflowChartData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#cbd5e1" className="dark:opacity-20" />
                <XAxis dataKey="label" tick={{ fontSize: isFinanceMobile ? 9 : 11, fill: '#475569' }} interval={isFinanceMobile ? 1 : 0} angle={isFinanceMobile ? -45 : 0} textAnchor={isFinanceMobile ? 'end' : 'middle'} height={isFinanceMobile ? 50 : 30} />
                <YAxis width={52} tick={{ fontSize: 11, fill: '#475569' }} tickFormatter={(v: number) => `${formatK(v)}`} />
                <Tooltip
                  cursor={CURSOR_STYLE}
                  content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null;
                    const d = payload[0]?.payload;
                    if (!d) return null;
                    return (
                      <div className="rounded-lg border border-gray-700 bg-gray-800 p-3 text-xs text-gray-100 shadow-lg">
                        <p className="mb-2 font-semibold text-white">{label}</p>
                        <div className="space-y-1">
                          <div className="flex justify-between gap-4">
                            <span style={{ color: '#0ea5e9' }}>Erwarteter Saldo</span>
                            <span className="font-medium">{formatCHF(d.cumulative_expected ?? d.cumulative)}</span>
                          </div>
                          {d.is_forecast && d.cumulative_expected != null && d.cumulative_expected !== d.cumulative && (
                            <div className="flex justify-between gap-4 text-gray-400">
                              <span>Gesichert (Worst Case)</span>
                              <span>{formatCHF(d.cumulative)}</span>
                            </div>
                          )}
                          {d.minLiq != null && (
                            <div className="flex justify-between gap-4 text-[10px] text-gray-400">
                              <span>Mindest-Liquidität</span>
                              <span>{formatCHF(d.minLiq)}</span>
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  }}
                />
                {cashflow.min_liquidity > 0 && (
                  <ReferenceLine
                    y={cashflow.min_liquidity}
                    stroke="#dc2626"
                    strokeWidth={2}
                    strokeDasharray="2 3"
                    label={{ value: `Mindest-Liquidität ${formatK(cashflow.min_liquidity)}`, position: 'insideTopLeft', fontSize: 10, fill: '#dc2626' }}
                  />
                )}
                <ReferenceLine
                  x={formatMonthLabel(currentMonth)}
                  stroke="#6366f1"
                  strokeWidth={2}
                  strokeDasharray="6 3"
                />
                {/* Unsichtbarer Anker-Balken: erzwingt dieselbe Band-Skala wie das obere
                    Panel, damit Monate und der Heute-Strich exakt deckungsgleich sind. */}
                <Bar dataKey="__axisAnchor" fill="transparent" isAnimationActive={false} legendType="none" />
                <Line dataKey="cumulative" name="Gesichert" stroke="#94a3b8" strokeWidth={2} strokeDasharray="5 3" dot={false} isAnimationActive={false} />
                <Line dataKey="cumulativeExpected" name="Erwarteter Saldo" stroke="#0ea5e9" strokeWidth={3} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-gray-600 dark:text-gray-300">
              <span className="flex items-center gap-1"><span className="inline-block h-0.5 w-4" style={{ background: '#0ea5e9' }} /> Erwarteter Saldo</span>
              <span className="flex items-center gap-1"><span className="inline-block h-0.5 w-4 border-t-2 border-dashed" style={{ borderColor: '#94a3b8' }} /> Gesichert (Worst Case)</span>
              {cashflow.min_liquidity > 0 && (
                <span className="flex items-center gap-1"><span className="inline-block h-0.5 w-4 border-t-2 border-dashed" style={{ borderColor: '#dc2626' }} /> Mindest-Liquidität</span>
              )}
            </div>
          </div>
        )}

        {/* 2-Spalten: Vorjahresvergleich + Kostenstruktur */}
        <div className="mb-6 grid gap-4 lg:grid-cols-2">
          {/* Vorjahresvergleich */}
          {yoy && (
            <div className={sectionClass}>
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Vorjahresvergleich (brutto)</h2>
                {yoy.growth_pct != null && (
                  <span className={`rounded-full px-2.5 py-1 text-xs font-bold ${
                    yoy.growth_pct >= 0
                      ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                      : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                  }`}>
                    {yoy.growth_pct >= 0 ? '+' : ''}{yoy.growth_pct}% YTD
                  </span>
                )}
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={yoy.months.filter(m => m.revenue_current > 0 || m.revenue_prior > 0)}>
                  <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
                  <XAxis dataKey="month_label" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} tickFormatter={(v: number) => formatK(v)} />
                  <Tooltip
                    formatter={(value: unknown, name: unknown) => {
                      const n = String(name);
                      const labels: Record<string, string> = {
                        revenue_current: `Umsatz ${yoy.current_year} (brutto)`,
                        revenue_prior: `Umsatz ${yoy.prior_year} (brutto)`,
                      };
                      return [formatCHF(Number(value)), labels[n] || n];
                    }}
                    contentStyle={TOOLTIP_STYLE} cursor={CURSOR_STYLE}
                  />
                  <Legend
                    formatter={(value: string) => {
                      if (value === 'revenue_current') return `${yoy.current_year}`;
                      if (value === 'revenue_prior') return `${yoy.prior_year}`;
                      return value;
                    }}
                    wrapperStyle={{ fontSize: '0.75rem' }}
                  />
                  <Bar dataKey="revenue_prior" name="revenue_prior" fill="#c7d2fe" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="revenue_current" name="revenue_current" fill="#6366f1" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Kostenstruktur: Stacked Bar mit VJ-Referenzlinie */}
          {costBarData.data.length > 0 && expenseBreakdown && (() => {
            const filteredData = costBarData.data.filter(d => (d._total_cur as number) > 0);
            const priorMonthsWithData = costBarData.data.filter(d => (d._total_prior as number) > 0);
            const avgPrior = priorMonthsWithData.length > 0
              ? priorMonthsWithData.reduce((s, d) => s + (d._total_prior as number), 0) / priorMonthsWithData.length
              : 0;
            return (
              <div className={sectionClass}>
                <div className="mb-1 flex items-center justify-between">
                  <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                    Kostenstruktur {expenseBreakdown.current_year}
                  </h2>
                  {avgPrior > 0 && (
                    <span className="text-xs text-gray-400 dark:text-gray-500">
                      Ø {expenseBreakdown.prior_year}: {formatCHF(avgPrior)}/Mt.
                    </span>
                  )}
                </div>
                <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
                  Aufwand nach Kategorie (periodengerecht) – nicht nach Zahlungszeitpunkt
                </p>
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart
                    data={filteredData}
                    barCategoryGap="15%"
                    barGap={2}
                  >
                    <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
                    <XAxis dataKey="_label_cur" tick={{ fontSize: 10 }} />
                    <YAxis tick={{ fontSize: 11 }} tickFormatter={(v: number) => formatK(v)} />
                    <Tooltip
                      cursor={CURSOR_STYLE}
                      content={({ active, payload, label }) => {
                        if (!active || !payload?.length) return null;
                        const d = payload[0]?.payload;
                        if (!d) return null;
                        const totalCur = (d._total_cur as number) || 0;
                        const totalPrior = (d._total_prior as number) || 0;
                        const deltaPct = totalPrior > 0 ? ((totalCur - totalPrior) / totalPrior * 100) : null;
                        return (
                          <div className="rounded-lg border border-gray-700 bg-gray-800 p-3 text-xs text-gray-100 shadow-lg">
                            <p className="mb-2 font-semibold text-white">{label}</p>
                            <div className="space-y-0.5">
                              {costBarData.keys.filter(k => (d[`cur_${k}`] || 0) > 0).map((k) => (
                                <div key={k} className="flex justify-between gap-4">
                                  <span className="text-gray-400">{costBarData.labels[k] || k}</span>
                                  <span className="font-medium">{formatCHF(d[`cur_${k}`])}</span>
                                </div>
                              ))}
                              <hr className="my-1 border-gray-600" />
                              <div className="flex justify-between gap-4 font-semibold">
                                <span>Total {expenseBreakdown.current_year}</span>
                                <span>{formatCHF(totalCur)}</span>
                              </div>
                              {totalPrior > 0 && (
                                <>
                                  <div className="flex justify-between gap-4 text-gray-400">
                                    <span>{d._label_prior || `${expenseBreakdown.prior_year}`}</span>
                                    <span>{formatCHF(totalPrior)}</span>
                                  </div>
                                  {deltaPct != null && (
                                    <div className={`flex justify-between gap-4 font-medium ${deltaPct <= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                      <span>Delta</span>
                                      <span>{deltaPct >= 0 ? '+' : ''}{deltaPct.toFixed(0)}%</span>
                                    </div>
                                  )}
                                </>
                              )}
                            </div>
                          </div>
                        );
                      }}
                    />
                    {avgPrior > 0 && (
                      <ReferenceLine
                        y={avgPrior}
                        stroke="#94a3b8"
                        strokeDasharray="6 4"
                        label={{ value: `Ø ${expenseBreakdown.prior_year}`, position: 'insideTopRight', fontSize: 10, fill: '#94a3b8' }}
                      />
                    )}
                    {costBarData.keys.slice(0, 8).map((k, i) => (
                      <Bar key={`cur_${k}`} dataKey={`cur_${k}`} stackId="cur" name={costBarData.labels[k] || k}
                        fill={CATEGORY_COLORS[i % CATEGORY_COLORS.length]}
                        radius={i === 0 ? [4, 4, 0, 0] : [0, 0, 0, 0]}
                      />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
                <div className="mt-3 flex flex-wrap gap-3 text-[11px] text-gray-500 dark:text-gray-400">
                  {costBarData.keys.slice(0, 8).map((k, i) => (
                    <span key={k} className="flex items-center gap-1">
                      <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: CATEGORY_COLORS[i % CATEGORY_COLORS.length] }} />
                      {costBarData.labels[k] || k}
                    </span>
                  ))}
                  {avgPrior > 0 && (
                    <span className="flex items-center gap-1">
                      <span className="inline-block h-0 w-4 border-t-2 border-dashed border-gray-400" />
                      Ø {expenseBreakdown.prior_year}
                    </span>
                  )}
                </div>
              </div>
            );
          })()}
        </div>

        {/* 2-Spalten: P&L Wasserfall + Gewinnmarge-Trend */}
        <div className="mb-6 grid gap-4 lg:grid-cols-2">
          {/* P&L Wasserfall */}
          {waterfall && waterfallChartData.length > 0 && (
            <div className={sectionClass}>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                  Erfolgsrechnung {waterfall.period_label} (netto)
                </h2>
                <div className="flex items-center gap-2">
                  <select
                    value={pnlPeriod}
                    onChange={async (e) => {
                      const p = e.target.value;
                      setPnlPeriod(p);
                      try {
                        const wf = await api.get<WaterfallResponse>(`/api/finance/pnl-waterfall?period=${p}`);
                        setWaterfall(wf);
                      } catch { /* ignore */ }
                    }}
                    className="rounded-lg border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300"
                  >
                    <option value="ytd">YTD {new Date().getFullYear()}</option>
                    {(() => {
                      const y = new Date().getFullYear();
                      const m = new Date().getMonth() + 1;
                      const names = ['', 'Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
                      const opts = [];
                      for (let i = m; i >= 1; i--) {
                        opts.push(<option key={`${y}-${i}`} value={`${y}-${String(i).padStart(2, '0')}`}>{names[i]} {y}</option>);
                      }
                      for (let i = 12; i >= Math.max(1, 12 - 3); i--) {
                        opts.push(<option key={`${y - 1}-${i}`} value={`${y - 1}-${String(i).padStart(2, '0')}`}>{names[i]} {y - 1}</option>);
                      }
                      return opts;
                    })()}
                  </select>
                  <span className={`rounded-full px-2.5 py-1 text-xs font-bold ${
                    waterfall.result >= 0
                      ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                      : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                  }`}>
                    {formatCHF(waterfall.result)}
                  </span>
                </div>
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={waterfallChartData} barCategoryGap="20%">
                  <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
                  <XAxis dataKey="name" tick={{ fontSize: 9 }} angle={-30} textAnchor="end" height={60} />
                  <YAxis tick={{ fontSize: 11 }} tickFormatter={(v: number) => formatK(v)} />
                  <Tooltip
                    formatter={(value: unknown, name: unknown) => {
                      if (String(name) === 'base') return [null, null];
                      return [formatCHF(Number(value)), 'Betrag'];
                    }}
                    contentStyle={TOOLTIP_STYLE} cursor={CURSOR_STYLE}
                  />
                  <Bar dataKey="base" stackId="stack" fill="transparent" />
                  <Bar dataKey="bar" stackId="stack" radius={[4, 4, 0, 0]}>
                    {waterfallChartData.map((entry, i) => (
                      <Cell
                        key={i}
                        fill={entry.type === 'income' ? '#22c55e' : entry.type === 'total' ? '#6366f1' : '#ef4444'}
                      />
                    ))}
                    <LabelList
                      dataKey="value"
                      position="top"
                      fontSize={9}
                      formatter={(v: unknown) => formatK(Number(v))}
                      fill="#6b7280"
                    />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Gewinnmarge-Trend (YTD-kumuliert + VJ-Benchmark) */}
          {marginTrend && marginTrend.months.length > 1 && (
            <div className={sectionClass}>
              <div className="mb-4 flex items-center justify-between">
                <h2 className={`text-lg font-semibold ${hasBg ? 'text-white' : 'text-gray-900 dark:text-white'}`}>
                  Gewinnmarge YTD
                </h2>
                {overview?.profit_margin_ytd != null && (
                  <span className={`rounded-full px-2.5 py-1 text-xs font-bold ${
                    overview.profit_margin_ytd >= 0
                      ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                      : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                  }`}>
                    {overview.profit_margin_ytd}%
                  </span>
                )}
              </div>
              <ResponsiveContainer width="100%" height={260}>
                <LineChart data={marginTrend.months}>
                  <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} tickFormatter={(v: number) => `${v}%`} domain={['auto', 'auto']} />
                  <Tooltip
                    formatter={(value: unknown, name: unknown) => {
                      if (value == null) return ['–', ''];
                      const n = String(name);
                      const labels: Record<string, string> = {
                        ytd_margin: `YTD ${marginTrend.current_year}`,
                        ytd_margin_prior: `YTD ${marginTrend.prior_year}`,
                        rolling_12m_margin: '12-Mt. Rolling',
                      };
                      return [`${Number(value)}%`, labels[n] || n];
                    }}
                    contentStyle={TOOLTIP_STYLE} cursor={CURSOR_STYLE}
                  />
                  <Legend
                    formatter={(value: string) => {
                      const labels: Record<string, string> = {
                        ytd_margin: `YTD ${marginTrend.current_year}`,
                        ytd_margin_prior: `VJ ${marginTrend.prior_year}`,
                        rolling_12m_margin: '12-Mt. Rolling',
                      };
                      return labels[value] || value;
                    }}
                    wrapperStyle={{ fontSize: '0.7rem' }}
                  />
                  <ReferenceLine y={0} stroke="#94a3b8" strokeDasharray="4 4" label={{ value: 'Break-Even', fontSize: 10, fill: '#94a3b8' }} />
                  <Line
                    type="monotone"
                    dataKey="ytd_margin"
                    stroke="#6366f1"
                    strokeWidth={2.5}
                    dot={{ r: 4, fill: '#6366f1', stroke: '#fff', strokeWidth: 2 }}
                    activeDot={{ r: 6 }}
                    connectNulls
                  />
                  <Line
                    type="monotone"
                    dataKey="ytd_margin_prior"
                    stroke="#94a3b8"
                    strokeWidth={1.5}
                    strokeDasharray="6 4"
                    dot={{ r: 3, fill: '#94a3b8', stroke: '#fff', strokeWidth: 1 }}
                    connectNulls
                  />
                  <Line
                    type="monotone"
                    dataKey="rolling_12m_margin"
                    stroke="#22c55e"
                    strokeWidth={1.5}
                    dot={false}
                    connectNulls
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        {/* Quell-Karten */}
        <div className="mb-6 grid gap-4 md:grid-cols-2">
          <SourceCard title="Toggl Track" subtitle="Leistungserfassung" color="violet" linkUrl="https://track.toggl.com" linkLabel="Toggl öffnen">
            {togglProjects.length === 0 ? (
              <p className="text-sm text-gray-400">Keine Daten für diesen Monat</p>
            ) : (
              <div className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-2 gap-y-1 text-sm">
                {togglProjects.slice(0, 5).map((p, i) => (
                  <React.Fragment key={i}>
                    <span className="truncate text-gray-600 dark:text-gray-300">
                      {p.client_name ? `${p.client_name} – ` : ''}{p.project_name}
                    </span>
                    <span className="whitespace-nowrap text-right text-xs text-gray-400 tabular-nums">{p.hours}h</span>
                    <span className="whitespace-nowrap text-right font-medium text-gray-900 tabular-nums dark:text-white">{formatCHF(p.amount)}</span>
                  </React.Fragment>
                ))}
              </div>
            )}
          </SourceCard>

          <InvoiceInsightPreview excludeVendors={creditorsExcludeVendors} />
        </div>

        {/* Detailtabelle */}
        {cashflow && (
          <div className={`rounded-2xl ${cardClass}`}>
            <div className="border-b border-gray-200 px-4 py-3 dark:border-gray-700 sm:px-6">
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Monatsübersicht</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:border-gray-700 dark:text-gray-400">
                    <th className="px-4 py-3 sm:px-6">Monat</th>
                    <th className="px-4 py-3 text-right sm:px-6">Einnahmen (brutto)</th>
                    <th className="px-4 py-3 text-right sm:px-6">Operativ</th>
                    <th className="hidden px-4 py-3 text-right sm:table-cell sm:px-6">Finanz.</th>
                    <th className="px-4 py-3 text-right sm:px-6">Delta</th>
                    <th className="px-4 py-3 text-right sm:px-6" title="Banksaldo: bis heute Ist-Stand, ab dem laufenden Monat prognostiziert (am heutigen Saldo verankert)">Kum. Saldo</th>
                    <th className="px-4 py-3 sm:px-6"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
                  {cashflow.months.map((m) => {
                    const hasSpecials = (m.special_items?.length ?? 0) > 0;
                    return (
                      <React.Fragment key={m.month}>
                        <tr
                          className={`transition-colors hover:bg-gray-50 dark:hover:bg-gray-800/40 ${
                            m.month === currentMonth ? 'bg-indigo-50/50 dark:bg-indigo-950/20'
                              : m.is_forecast ? 'bg-amber-50/30 dark:bg-amber-950/10' : ''
                          }`}
                        >
                          <td className="whitespace-nowrap px-4 py-2.5 font-medium text-gray-900 dark:text-white sm:px-6">
                            {formatMonthLabel(m.month)}
                            {m.is_forecast && (
                              <span className="ml-2 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 dark:bg-amber-900/30 dark:text-amber-400">
                                Schätzung
                              </span>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-4 py-2.5 text-right text-green-600 dark:text-green-400 sm:px-6">
                            {formatCHF(m.revenue)}
                            {m.is_forecast && (m.revenue || 0) > 0 && (
                              <div className="text-[10px] font-normal text-gray-400" title="Gebucht / Pipeline / Auffüllung">
                                {formatK(m.forecast_committed || 0)} · {formatK(m.forecast_pipeline || 0)} · {formatK(m.forecast_fill || 0)}
                              </div>
                            )}
                            {!m.is_forecast && m.month === currentMonth && (m.forecast_fill || 0) > 0 && (
                              <div className="text-[10px] font-normal text-gray-400" title="Bereits erfasst / erwarteter Rest">
                                {formatK(m.forecast_committed || 0)} erfasst · +{formatK(m.forecast_fill || 0)} erwartet
                              </div>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-4 py-2.5 text-right text-red-500 dark:text-red-400 sm:px-6">
                            {formatCHF(m.expenses)}
                          </td>
                          <td className="hidden whitespace-nowrap px-4 py-2.5 text-right sm:table-cell sm:px-6" style={{ color: '#f59e0b' }}>
                            {(m.fin_outflow || 0) > 0 ? formatCHF(m.fin_outflow) : '–'}
                          </td>
                          <td className={`whitespace-nowrap px-4 py-2.5 text-right font-medium sm:px-6 ${
                            m.delta >= 0 ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'
                          }`}>
                            {m.delta >= 0 ? '+' : ''}{formatCHF(m.delta)}
                          </td>
                          <td className="whitespace-nowrap px-4 py-2.5 text-right font-semibold text-gray-900 dark:text-white sm:px-6">
                            {formatCHF(m.cumulative)}
                          </td>
                          <td className="px-4 py-2.5 sm:px-6">
                            {m.month === currentMonth && (
                              <span className="inline-block h-2 w-2 rounded-full bg-indigo-500" title="Aktueller Monat" />
                            )}
                          </td>
                        </tr>
                        {hasSpecials && (
                          <tr className="bg-orange-50/40 dark:bg-orange-950/10">
                            <td colSpan={7} className="px-8 py-1.5 sm:px-10">
                              <div className="flex flex-wrap gap-x-5 gap-y-0.5 text-xs text-orange-600 dark:text-orange-400">
                                {m.special_items.map((si, idx) => (
                                  <span key={idx}>
                                    {si.label}: {formatCHF(si.amount)}
                                  </span>
                                ))}
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {loading && !cashflow && (
          <div className="flex items-center justify-center py-20">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-indigo-200 border-t-indigo-600" />
          </div>
        )}
      </div>

      <BackgroundPicker
        isOpen={bgPickerOpen}
        onClose={() => setBgPickerOpen(false)}
        currentUrl={bgUrl}
        onSelect={(url) => { handleBgSelect(url); setBgPickerOpen(false); }}
      />
    </div>
  );
}

// ── Sub-Komponenten ─────────────────────────────────

function InfoTooltip({ text }: { text: string }) {
  return (
    <span className="group/tip relative inline-flex shrink-0">
      {/* Negatives Margin: mobil 44px Trefferfläche, ohne das Layout zu verändern. */}
      <button
        type="button"
        aria-label="Erklärung anzeigen"
        className="-m-[15px] p-[15px] text-gray-300 transition-colors hover:text-gray-500 focus:outline-none lg:m-0 lg:p-0 dark:text-gray-600 dark:hover:text-gray-300"
      >
        <InfoIcon className="h-3.5 w-3.5" />
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute right-0 top-5 z-30 w-56 max-w-[75vw] rounded-lg bg-gray-900 p-2 text-[11px] font-normal leading-snug text-white opacity-0 shadow-lg transition-opacity duration-150 group-hover/tip:opacity-100 group-focus-within/tip:opacity-100 dark:bg-gray-700"
      >
        {text}
      </span>
    </span>
  );
}

interface KpiTrend {
  value: number;            // % (unit '%'), Prozentpunkte (unit 'pp' -> '%-Pkt.') oder CHF-Delta (unit 'chf')
  unit?: '%' | 'pp' | 'chf';
  goodWhen?: 'up' | 'down'; // bestimmt Farbe; default 'up'
  title?: string;           // Tooltip/aria
}

function TrendPill({ trend }: { trend: KpiTrend }) {
  const { value, unit = '%', goodWhen = 'up', title } = trend;
  const isFlat = unit === 'chf' ? Math.round(value) === 0 : Math.round(value) === 0;
  const isUp = value > 0;
  // "gut" haengt von der Richtung der Kennzahl ab (z. B. Personalquote: runter = gut)
  const isGood = isFlat ? null : (goodWhen === 'up' ? isUp : !isUp);
  const tone = isGood == null
    ? 'text-gray-500 bg-gray-100 dark:text-gray-400 dark:bg-gray-700/60'
    : isGood
      ? 'text-green-700 bg-green-50 dark:text-green-400 dark:bg-green-500/10'
      : 'text-red-700 bg-red-50 dark:text-red-400 dark:bg-red-500/10';
  const arrow = isFlat ? '→' : isUp ? '↑' : '↓';
  const sign = value > 0 ? '+' : '';
  const display = unit === 'pp'
    ? `${sign}${Math.round(value)} %-Pkt.`
    : unit === 'chf'
      ? `${sign}${formatCHF(value)}`
      : `${sign}${Math.round(value)}%`;
  return (
    <span
      title={title}
      className={`inline-flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] font-semibold leading-none ${tone}`}
    >
      <span aria-hidden>{arrow}</span>{display}
    </span>
  );
}

function KpiCard({
  label, value, sublabel, icon, status = 'neutral', info, trend,
}: {
  label: string; value: string; sublabel: string; icon: React.ReactNode;
  status?: 'green' | 'yellow' | 'red' | 'neutral';
  info?: string;
  trend?: KpiTrend | null;
}) {
  const statusColors: Record<string, string> = {
    green: 'border-l-green-500',
    yellow: 'border-l-amber-500',
    red: 'border-l-red-500',
    neutral: 'border-l-gray-200 dark:border-l-gray-700',
  };
  return (
    <div className={`rounded-xl border border-gray-200 border-l-4 ${statusColors[status]} bg-white p-2.5 shadow-sm sm:p-3 lg:p-4 dark:border-gray-700 dark:bg-gray-800/50`}>
      <div className="flex items-start gap-2 lg:items-center lg:gap-3">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-gray-50 text-gray-500 sm:h-8 sm:w-8 lg:h-10 lg:w-10 dark:bg-gray-800 dark:text-gray-400">
          {icon}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-1">
            <p className="min-w-0 text-[10px] font-medium leading-snug text-gray-500 sm:text-[11px] lg:truncate lg:text-xs dark:text-gray-400" title={label}>{label}</p>
            {info && <InfoTooltip text={info} />}
          </div>
          <div className="mt-0.5 flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
            <p className="min-w-0 break-words text-sm font-bold leading-tight text-gray-900 sm:text-base lg:text-xl dark:text-white">{value}</p>
            {trend && <TrendPill trend={trend} />}
          </div>
          <p className="mt-0.5 text-[10px] leading-snug text-gray-500 sm:text-[11px] lg:truncate lg:text-xs dark:text-gray-400" title={sublabel}>{sublabel}</p>
        </div>
      </div>
    </div>
  );
}

function SourceCard({
  title, subtitle, color, linkUrl, linkLabel, comingSoon, children,
}: {
  title: string; subtitle: string; color: string;
  linkUrl?: string; linkLabel?: string; comingSoon?: boolean;
  children: React.ReactNode;
}) {
  const borderColors: Record<string, string> = {
    blue: 'border-t-blue-500',
    violet: 'border-t-violet-500',
    rose: 'border-t-rose-400',
  };
  return (
    // `min-w-0`: als Grid-Item darf die Karte sonst nicht unter ihre Inhaltsbreite
    // schrumpfen und schob den Öffnen-Link mobil ~60px aus dem Viewport.
    <div className={`min-w-0 rounded-2xl border border-gray-200 border-t-4 ${borderColors[color] || 'border-t-gray-400'} bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800/50`}>
      <div className="mb-3 flex min-w-0 items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-gray-900 dark:text-white">{title}</h3>
          <p className="truncate text-xs text-gray-500 dark:text-gray-400">{subtitle}</p>
        </div>
        {comingSoon && (
          <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-400">
            Coming Soon
          </span>
        )}
      </div>
      {children}
      {linkUrl && (
        <a
          href={linkUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-3 flex min-h-11 items-center gap-1 text-xs font-medium text-indigo-600 transition-colors hover:text-indigo-500 lg:min-h-0 dark:text-indigo-400"
        >
          {linkLabel || 'Öffnen'}
          <ExternalLinkIcon className="h-3 w-3" />
        </a>
      )}
    </div>
  );
}

// ── Icons ────────────────────────────────────────────

function RefreshIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182" />
    </svg>
  );
}

function InfoIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.8} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z" />
    </svg>
  );
}

function BankIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18.75a60.07 60.07 0 0 1 15.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 0 1 3 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 0 0-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 0 1-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 0 0 3 15h-.75M15 10.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm3 0h.008v.008H18V10.5Zm-12 0h.008v.008H6V10.5Z" />
    </svg>
  );
}

function InvoiceIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
    </svg>
  );
}

function ClockIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
    </svg>
  );
}

function TrendIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18 9 11.25l4.306 4.306a11.95 11.95 0 0 1 5.814-5.518l2.74-1.22m0 0-5.94-2.281m5.94 2.28-2.28 5.941" />
    </svg>
  );
}

function CashflowIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 0 1 3 19.875v-6.75ZM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V8.625ZM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V4.125Z" />
    </svg>
  );
}

function RunwayIcon() {
  return (
    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.91 11.672a.375.375 0 0 1 0 .656l-5.603 3.113a.375.375 0 0 1-.557-.328V8.887c0-.286.307-.466.557-.327l5.603 3.112Z" />
    </svg>
  );
}

function BgImageIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5a2.25 2.25 0 0 0 2.25-2.25V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Zm6-13.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z" />
    </svg>
  );
}

function ExternalLinkIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 0 0 3 8.25v10.5A2.25 2.25 0 0 0 5.25 21h10.5A2.25 2.25 0 0 0 18 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
    </svg>
  );
}

interface UpcomingPaymentRow {
  vendor: string;
  next_date?: string;
  amount_chf?: number;
  invoice_id?: number;
  days_until?: number;
}

function formatDateCH(iso: string | undefined): string {
  if (!iso) return '–';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}.${mm}.${d.getFullYear()}`;
}

function InvoiceInsightPreview({ excludeVendors }: { excludeVendors: string | null }) {
  const [upcoming, setUpcoming] = useState<UpcomingPaymentRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [pdfModalUrl, setPdfModalUrl] = useState<string | null>(null);

  useEffect(() => {
    const terms = parseExcludeVendors(excludeVendors);
    api.get<unknown>('/api/creditors/upcoming?n=20')
      .then(data => {
        const raw = Array.isArray(data) ? data : (data as Record<string, unknown>)?.payments as Record<string, unknown>[] || [];
        const filtered = raw.filter((p) => !isExcludedVendor(
          (p.vendor ?? p.Kreditor) as string | undefined,
          (p.product ?? p.Produkt) as string | undefined,
          terms,
        ));
        setUpcoming(filtered.map((p: Record<string, unknown>) => ({
          vendor: (p.vendor ?? p.Kreditor ?? '–') as string,
          next_date: (p.next_date ?? p.Renewal_Date_Parsed ?? p.Renewal_Date) as string | undefined,
          amount_chf: (p.amount_chf ?? p.Betrag_CHF) as number | undefined,
          invoice_id: (p.invoice_id ?? p.index) as number | undefined,
          days_until: (p.days_until ?? p.Tage_bis_Renewal) as number | undefined,
        })));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [excludeVendors]);

  const handleRowClick = (p: UpcomingPaymentRow) => {
    if (p.invoice_id != null) {
      setPdfModalUrl(`/api/creditors/invoice/${p.invoice_id}/pdf/view`);
    }
  };

  return (
    <>
      <SourceCard title="InvoiceInsight" subtitle="Nächste Zahlungen (30 Tage)" color="rose" linkUrl="/kreditoren" linkLabel="Kreditoren öffnen">
        {loading ? (
          <p className="py-2 text-center text-sm text-gray-400">Laden...</p>
        ) : upcoming.length === 0 ? (
          <p className="py-2 text-center text-sm text-gray-400">Keine anstehenden Zahlungen</p>
        ) : (
          <div className="max-h-[200px] space-y-1 overflow-y-auto">
            {upcoming.map((p, i) => (
              <div
                key={i}
                className={`flex items-center justify-between rounded-md px-1 py-1 text-xs transition-colors ${p.invoice_id != null ? 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/40' : ''}`}
                onClick={() => handleRowClick(p)}
              >
                <div className="flex items-center gap-1.5 min-w-0">
                  {(p.days_until ?? 999) < 7 && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-red-500" />}
                  {(p.days_until ?? 999) >= 7 && (p.days_until ?? 999) < 30 && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />}
                  {(p.days_until ?? 999) >= 30 && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-green-500" />}
                  <span className="truncate text-gray-600 dark:text-gray-300">{p.vendor}</span>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span className="w-[78px] text-right tabular-nums text-gray-400">{formatDateCH(p.next_date)}</span>
                  <span className="w-[90px] text-right font-medium tabular-nums text-gray-900 dark:text-white">
                    {p.amount_chf != null ? formatCHF(p.amount_chf) : ''}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </SourceCard>
      {pdfModalUrl && (
        <InvoicePdfModal url={pdfModalUrl} onClose={() => setPdfModalUrl(null)} />
      )}
    </>
  );
}

function InvoicePdfModal({ url, onClose }: { url: string; onClose: () => void }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  useEffect(() => {
    let revoke = '';
    const token = localStorage.getItem('taskpilot_token');
    fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.blob();
      })
      .then(blob => {
        const u = URL.createObjectURL(blob);
        revoke = u;
        setBlobUrl(u);
      })
      .catch(e => setError(e.message));
    return () => { if (revoke) URL.revokeObjectURL(revoke); };
  }, [url]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm modal-safe" onClick={onClose}>
      <div className="relative mx-2 h-[90dvh] w-full rounded-2xl bg-white shadow-2xl lg:mx-4 lg:h-[85vh] lg:max-w-6xl dark:bg-gray-900" onClick={e => e.stopPropagation()}>
        <button
          onClick={onClose}
          className="absolute right-2 top-2 z-10 flex h-9 w-9 items-center justify-center rounded-full bg-gray-800 text-white shadow-lg hover:bg-gray-700 lg:-right-3 lg:-top-3 lg:h-8 lg:w-8"
        >
          ✕
        </button>
        {error ? (
          <div className="flex h-full items-center justify-center text-red-500">{error}</div>
        ) : blobUrl ? (
          isMobile ? (
            <div className="flex h-full flex-col items-center justify-center gap-4 p-6 text-center">
              <svg className="h-16 w-16 text-gray-300 dark:text-gray-600" fill="none" viewBox="0 0 24 24" strokeWidth={1} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
              </svg>
              <p className="text-sm text-gray-500 dark:text-gray-400">PDF-Vorschau ist auf Mobilgeräten nicht verfügbar.</p>
              <a
                href={blobUrl}
                download="rechnung.pdf"
                className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
                </svg>
                PDF herunterladen
              </a>
            </div>
          ) : (
            <iframe src={blobUrl} className="h-full w-full rounded-2xl" title="PDF-Vorschau" />
          )
        ) : (
          <div className="flex h-full items-center justify-center text-gray-400">Laden…</div>
        )}
      </div>
    </div>
  );
}
