import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  LayoutDashboard, FileText, CalendarClock, Building2,
  TrendingUp, AlertTriangle, Microscope,
  RefreshCw, Image, ExternalLink, SlidersHorizontal,
} from 'lucide-react';
import { api } from '../api/client';
import { BackgroundPicker } from '../components/BackgroundPicker';
import type { CreditorsTab, CreditorsFilter, StyleCtx, DashboardData } from './creditors/creditors-types';
import { activeFilterCount } from './creditors/creditors-helpers';
import { CreditorsFilterPanel } from './creditors/CreditorsFilterPanel';
import { CreditorsOverview } from './creditors/CreditorsOverview';
import { CreditorsInvoices } from './creditors/CreditorsInvoices';
import { CreditorsRenewals } from './creditors/CreditorsRenewals';
import { CreditorsVendors } from './creditors/CreditorsVendors';
import { CreditorsAnalysis } from './creditors/CreditorsAnalysis';
import { CreditorsAnomalies } from './creditors/CreditorsAnomalies';
import { CreditorsResearch } from './creditors/CreditorsResearch';

const TABS: { id: CreditorsTab; label: string; shortLabel: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: 'uebersicht', label: 'Übersicht', shortLabel: 'Übersicht', icon: LayoutDashboard },
  { id: 'rechnungen', label: 'Rechnungen', shortLabel: 'Rechn.', icon: FileText },
  { id: 'erneuerungen', label: 'Erneuerungen', shortLabel: 'Erneu.', icon: CalendarClock },
  { id: 'anbieter', label: 'Anbieter', shortLabel: 'Anb.', icon: Building2 },
  { id: 'trends', label: 'Trends & Analyse', shortLabel: 'Trends', icon: TrendingUp },
  { id: 'anomalien', label: 'Anomalien', shortLabel: 'Anom.', icon: AlertTriangle },
  { id: 'research', label: 'Deep Research', shortLabel: 'Research', icon: Microscope },
];

class CreditorsErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-4 p-8">
          <div className="rounded-2xl border border-red-200 bg-red-50 p-8 text-center dark:border-red-800 dark:bg-red-950/30">
            <h2 className="mb-2 text-lg font-bold text-red-700 dark:text-red-400">Rendering-Fehler</h2>
            <p className="mb-4 text-sm text-red-600 dark:text-red-300">{this.state.error?.message}</p>
            <button
              onClick={() => { this.setState({ hasError: false, error: null }); window.location.reload(); }}
              className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
            >
              Seite neu laden
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function CreditorsPage() {
  return (
    <CreditorsErrorBoundary>
      <CreditorsPageInner />
    </CreditorsErrorBoundary>
  );
}

function CreditorsPageInner() {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = (searchParams.get('tab') as CreditorsTab) || 'uebersicht';

  const setTab = useCallback((tab: CreditorsTab) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev);
      next.set('tab', tab);
      return next;
    });
  }, [setSearchParams]);

  const [filter, setFilter] = useState<CreditorsFilter>({
    yearFrom: new Date().getFullYear() - 1,
    yearTo: new Date().getFullYear(),
  });
  const [filterOpen, setFilterOpen] = useState(false);

  const [meta, setMeta] = useState<{ categories: string[]; vendors: string[]; yearRange: { min: number; max: number } }>({
    categories: [],
    vendors: [],
    yearRange: { min: 2019, max: new Date().getFullYear() },
  });

  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    api.get<DashboardData>('/api/creditors/dashboard')
      .then(d => {
        const m = d.metadata || {};
        const yr = (m as Record<string, unknown>).year_range as Record<string, unknown> | undefined;
        setMeta({
          categories: ((m as Record<string, unknown>).categories ?? []) as string[],
          vendors: ((m as Record<string, unknown>).vendors ?? []) as string[],
          yearRange: {
            min: (yr?.min ?? 2019) as number,
            max: (yr?.max ?? new Date().getFullYear()) as number,
          },
        });
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  useEffect(() => {
    api.get<{ creditors_background_url: string | null }>('/api/settings')
      .then(s => { if (s.creditors_background_url) setBgUrl(s.creditors_background_url); })
      .catch(() => {});
  }, []);

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { creditors_background_url: url });
    setBgUrl(url);
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    try { await api.post('/api/creditors/cache/clear', {}); } catch { /* ignore */ }
    window.location.reload();
  };

  const hasBg = !!bgUrl;
  const isGradient = bgUrl?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient
    ? { background: bgUrl!.slice('gradient:'.length) }
    : hasBg ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover' as const, backgroundPosition: 'center' } : undefined;

  const styleCtx: StyleCtx = useMemo(() => {
    const cardClass = hasBg
      ? 'bg-black/30 backdrop-blur-xl ring-1 ring-white/10'
      : 'border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800/50';
    return {
      hasBg,
      cardClass,
      sectionClass: `rounded-2xl p-4 sm:p-6 ${cardClass}`,
      textPrimary: hasBg ? 'text-white' : 'text-gray-900 dark:text-white',
      textSecondary: hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400',
      textMuted: hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500',
    };
  }, [hasBg]);

  const years = useMemo(() => {
    const arr: number[] = [];
    for (let y = meta.yearRange.max; y >= meta.yearRange.min; y--) arr.push(y);
    return arr;
  }, [meta.yearRange]);

  const filterCount = activeFilterCount(filter);

  const renderTab = () => {
    switch (activeTab) {
      case 'uebersicht':
        return <CreditorsOverview filter={filter} styleCtx={styleCtx} />;
      case 'rechnungen':
        return <CreditorsInvoices filter={filter} styleCtx={styleCtx} categories={meta.categories} years={years} />;
      case 'erneuerungen':
        return <CreditorsRenewals filter={filter} styleCtx={styleCtx} />;
      case 'anbieter':
        return <CreditorsVendors filter={filter} styleCtx={styleCtx} />;
      case 'trends':
        return <CreditorsAnalysis filter={filter} styleCtx={styleCtx} />;
      case 'anomalien':
        return <CreditorsAnomalies filter={filter} styleCtx={styleCtx} />;
      case 'research':
        return <CreditorsResearch filter={filter} styleCtx={styleCtx} />;
      default:
        return <CreditorsOverview filter={filter} styleCtx={styleCtx} />;
    }
  };

  const btnBase = hasBg
    ? 'bg-white/10 text-white/90 hover:bg-white/20 backdrop-blur-sm'
    : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700';

  return (
    <div className="relative flex h-full flex-col" style={hasBg ? bgStyle : undefined}>
      {!hasBg && <div className="absolute inset-0 bg-gradient-to-br from-slate-50 via-white to-amber-50/20 dark:from-gray-950 dark:via-gray-900 dark:to-amber-950/10" />}
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/25 dark:bg-black/40" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/25" />}

      <div className="relative z-10 flex h-full flex-col overflow-hidden">
        {/* Header */}
        <div className="flex-shrink-0 px-4 pt-4 sm:px-6 sm:pt-6">
          <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              {/* Titel liefert mobil der MobileHeader. */}
              <h1 className={`hidden text-xl font-bold lg:block lg:text-2xl ${hasBg ? 'text-white drop-shadow-sm' : 'text-gray-900 dark:text-white'}`}>
                Kreditoren
              </h1>
              <p className={`mt-1 text-xs ${styleCtx.textMuted}`}>
                InvoiceInsight · Powered by MCP
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setFilterOpen(f => !f)}
                className={`relative flex min-h-11 min-w-11 items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 lg:min-w-0 ${btnBase}`}
              >
                <SlidersHorizontal className="h-4 w-4" />
                <span className="hidden sm:inline">Filter</span>
                {filterCount > 0 && (
                  <span className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full bg-indigo-500 text-[10px] font-bold text-white shadow">
                    {filterCount}
                  </span>
                )}
              </button>
              <a
                href="http://invoice.innosmith.ai"
                target="_blank"
                rel="noopener noreferrer"
                className={`flex min-h-11 min-w-11 items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 lg:min-w-0 ${btnBase}`}
              >
                <ExternalLink className="h-4 w-4" />
                <span className="hidden sm:inline">InvoiceInsight</span>
              </a>
              <button
                onClick={() => setBgPickerOpen(true)}
                className={`flex min-h-11 min-w-11 items-center justify-center rounded-lg p-2 transition-colors lg:min-h-0 lg:min-w-0 ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`}
                title="Hintergrund ändern"
              >
                <Image className="h-5 w-5" />
              </button>
              <button
                onClick={handleRefresh}
                disabled={refreshing}
                className={`flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 lg:min-w-0 ${btnBase}`}
              >
                <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
                <span className="hidden sm:inline">Aktualisieren</span>
              </button>
            </div>
          </div>

          {/* Tab Bar */}
          <div className="-mx-4 overflow-x-auto px-4 sm:-mx-6 sm:px-6">
            <div className={`flex gap-0.5 border-b ${hasBg ? 'border-white/10' : 'border-gray-200 dark:border-gray-700'}`}>
              {TABS.map(t => {
                const isActive = activeTab === t.id;
                const Icon = t.icon;
                return (
                  <button
                    key={t.id}
                    onClick={() => setTab(t.id)}
                    className={`relative flex min-h-11 items-center gap-1.5 whitespace-nowrap px-3 py-2.5 text-[11px] font-medium transition-colors sm:text-sm lg:min-h-0 ${
                      isActive
                        ? hasBg
                          ? 'text-white'
                          : 'text-indigo-600 dark:text-indigo-400'
                        : hasBg
                          ? 'text-white/50 hover:text-white/80'
                          : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
                    }`}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="hidden sm:inline">{t.label}</span>
                    <span className="sm:hidden">{t.shortLabel}</span>
                    {isActive && (
                      <span className={`absolute bottom-0 left-1 right-1 h-0.5 rounded-full ${hasBg ? 'bg-white' : 'bg-indigo-500 dark:bg-indigo-400'}`} />
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* Content Area with optional filter sidebar */}
        <div className="flex min-h-0 flex-1">
          {/* Filter Panel -- desktop sidebar */}
          {filterOpen && (
            <>
              {/* Mobile overlay backdrop */}
              <div
                className="fixed inset-0 z-30 bg-black/40 backdrop-blur-sm lg:hidden"
                onClick={() => setFilterOpen(false)}
              />
              <div className={`
                fixed inset-x-0 bottom-0 z-40 max-h-[70vh] overflow-y-auto rounded-t-2xl shadow-2xl
                lg:relative lg:inset-auto lg:z-auto lg:max-h-none lg:rounded-none lg:shadow-none
                lg:flex-shrink-0 lg:border-r lg:overflow-y-auto
                ${hasBg ? 'bg-gray-900/95 lg:border-white/10' : 'bg-white dark:bg-gray-900 lg:border-gray-200 lg:dark:border-gray-700'}
              `}>
                {/* Mobile drag indicator */}
                <div className="flex justify-center py-2 lg:hidden">
                  <div className="h-1 w-10 rounded-full bg-gray-300 dark:bg-gray-600" />
                </div>
                <CreditorsFilterPanel
                  filter={filter}
                  onChange={setFilter}
                  onClose={() => setFilterOpen(false)}
                  categories={meta.categories}
                  vendors={meta.vendors}
                  yearRange={meta.yearRange}
                  styleCtx={styleCtx}
                />
              </div>
            </>
          )}

          {/* Tab Content */}
          <div className="flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
            {loading ? (
              <div className="flex flex-col items-center justify-center gap-3 py-20">
                <RefreshCw className="h-8 w-8 animate-spin text-indigo-400" />
                <p className={`text-sm ${styleCtx.textMuted}`}>Daten werden geladen…</p>
              </div>
            ) : (
              renderTab()
            )}
          </div>
        </div>
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
