import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api } from '../api/client';
import { BackgroundPicker } from '../components/BackgroundPicker';

// ── ErrorBoundary ────────────────────────────────────

class SignaleErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('SignalePage Crash:', error, info.componentStack);
  }
  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-screen items-center justify-center bg-gray-950 p-8">
          <div className="max-w-2xl rounded-2xl border border-red-500/30 bg-red-950/40 p-8 text-white shadow-xl">
            <h2 className="text-lg font-bold text-red-400">Rendering-Fehler</h2>
            <pre className="mt-4 whitespace-pre-wrap rounded-lg bg-black/40 p-4 text-sm text-red-300">{this.state.error.message}</pre>
            <pre className="mt-2 max-h-60 overflow-y-auto whitespace-pre-wrap rounded-lg bg-black/30 p-4 text-xs text-gray-400">{this.state.error.stack}</pre>
            <button onClick={() => this.setState({ error: null })} className="mt-6 rounded-lg bg-indigo-600 px-5 py-2.5 text-sm font-medium transition-colors hover:bg-indigo-500">
              Erneut versuchen
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

// ── Types ────────────────────────────────────────────

interface Signal {
  id: number;
  title: string;
  source_name: string | null;
  url: string | null;
  type: string | null;
  status: string | null;
  description: string | null;
  thumbnail_url: string | null;
  published_at: string | null;
  total_score: number | null;
  relevant_role: string | null;
  ai_reason: string | null;
  topic_name: string | null;
  category: string | null;
  has_full_content?: boolean;
  full_content?: string | null;
}

interface SignalListResponse {
  signals: Signal[];
  total: number;
  limit: number;
  offset: number;
}

interface Briefing {
  id: number;
  briefing_date: string | null;
  signal_count: number | null;
  high_score_count: number | null;
  avg_score: number | null;
  signal_density: string | null;
  top_keywords: string[] | string | null;
  createdAt: string | null;
  briefing_text?: string | null;
  briefing_html?: string | null;
  audio_url?: string | null;
  duration_seconds?: number | null;
}

interface DeepDiveListItem {
  id: number;
  persona_name: string | null;
  createdAt: string | null;
}

interface BriefingItem {
  id: number;
  type: 'daily' | 'deep-dive';
  date: string;
  signal_count?: number;
  avg_score?: number;
  persona_name?: string;
}

interface BriefingDetail {
  briefing_text?: string | null;
  briefing_html?: string | null;
  audio_url?: string | null;
  persona_name?: string | null;
  type: 'daily' | 'deep-dive';
}

type BriefingFilter = 'all' | 'daily' | 'deep-dive';

interface Persona { id: number; persona_name: string; description: string | null; }
interface Topic { id: number; topic_name: string; relevance_weight: number | null; category: string | null; keywords: string | null; strategic_why: string | null; }
interface Stats { total: number; high_score: number; medium_score: number; low_score: number; avg_score: number; today: number; this_week: number; sources: number; rss_count: number; youtube_count: number; web_count: number; }

type TabId = 'signals' | 'briefings';
type TypeFilter = '' | 'rss' | 'youtube';
type TimeRange = '' | 'today' | 'week' | '2weeks';
type ScoreFilter = 0 | 7 | 8 | 9;

interface SavedFilters { typeFilter: TypeFilter; timeRange: TimeRange; minScore: ScoreFilter; topicFilter: string; personaFilter: string; }

const FILTER_STORAGE_KEY = 'signale_filters';
const LIMIT = 20;

function loadSavedFilters(): SavedFilters {
  try { const raw = localStorage.getItem(FILTER_STORAGE_KEY); if (raw) return JSON.parse(raw); } catch { /* ignore */ }
  return { typeFilter: '', timeRange: '', minScore: 0, topicFilter: '', personaFilter: '' };
}
function saveFilters(f: SavedFilters) { localStorage.setItem(FILTER_STORAGE_KEY, JSON.stringify(f)); }

function relativeTime(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'gerade eben';
  if (mins < 60) return `vor ${mins} Min.`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `vor ${hours} Std.`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `vor ${days} ${days === 1 ? 'Tag' : 'Tagen'}`;
  return new Date(isoDate).toLocaleDateString('de-CH', { day: 'numeric', month: 'short', year: 'numeric' });
}

function getYouTubeEmbedUrl(url: string): string {
  const match = url.match(/(?:v=|youtu\.be\/|embed\/)([a-zA-Z0-9_-]{11})/);
  return match ? `https://www.youtube.com/embed/${match[1]}?autoplay=1` : url;
}

function getYouTubeVideoId(url: string): string | null {
  const match = url.match(/(?:v=|youtu\.be\/|embed\/)([a-zA-Z0-9_-]{11})/);
  return match ? match[1] : null;
}

function getHighResThumbnail(url: string | null): string | null {
  if (!url) return null;
  const id = getYouTubeVideoId(url);
  if (!id) return null;
  return `https://img.youtube.com/vi/${id}/maxresdefault.jpg`;
}

const STRIP_CSS_PROPS = new Set([
  'background', 'background-color', 'background-image',
  'font-family', 'box-shadow', 'color',
]);

function stripInlineStyles(html: string): string {
  return html
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/\s*bgcolor="[^"]*"/gi, '')
    .replace(/\s*color="[^"]*"/gi, '')
    .replace(/style="([^"]*)"/gi, (_, css: string) => {
      const kept = css
        .split(';')
        .filter(p => {
          const name = p.split(':')[0]?.trim().toLowerCase() ?? '';
          return name && !STRIP_CSS_PROPS.has(name);
        })
        .join(';')
        .trim();
      return kept ? `style="${kept}"` : '';
    });
}

// ── Component ────────────────────────────────────────

export function SignalePage() {
  const [tab, setTab] = useState<TabId>('signals');
  const [stats, setStats] = useState<Stats | null>(null);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [topics, setTopics] = useState<Topic[]>([]);

  const [signals, setSignals] = useState<Signal[]>([]);
  const [signalTotal, setSignalTotal] = useState(0);
  const [loadingSignals, setLoadingSignals] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);
  const loadingRef = useRef(false);

  const saved = useMemo(loadSavedFilters, []);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>(saved.typeFilter);
  const [timeRange, setTimeRange] = useState<TimeRange>(saved.timeRange);
  const [minScore, setMinScore] = useState<ScoreFilter>(saved.minScore);
  const [topicFilter, setTopicFilter] = useState(saved.topicFilter);
  const [personaFilter, setPersonaFilter] = useState(saved.personaFilter);
  const [searchTerm, setSearchTerm] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [expandedSignal, setExpandedSignal] = useState<Signal | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  // YouTube inline player
  const [playingVideoId, setPlayingVideoId] = useState<number | null>(null);

  const [briefingItems, setBriefingItems] = useState<BriefingItem[]>([]);
  const [selectedDetail, setSelectedDetail] = useState<(BriefingDetail & { id: number; date: string }) | null>(null);
  const [loadingBriefings, setLoadingBriefings] = useState(false);
  const [briefingFilter, setBriefingFilter] = useState<BriefingFilter>('all');
  const [ddPersonaFilter, setDdPersonaFilter] = useState('');

  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [mobileFilterOpen, setMobileFilterOpen] = useState(false);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => { saveFilters({ typeFilter, timeRange, minScore, topicFilter, personaFilter }); }, [typeFilter, timeRange, minScore, topicFilter, personaFilter]);

  useEffect(() => {
    Promise.all([
      api.get<Stats>('/api/signa/stats').catch(() => null),
      api.get<Persona[]>('/api/signa/personas').catch(() => []),
      api.get<Topic[]>('/api/signa/topics').catch(() => []),
      api.get<{ signale_background_url: string | null }>('/api/settings').then(s => s.signale_background_url).catch(() => null),
    ]).then(([s, p, t, bg]) => { if (s) setStats(s); setPersonas(p as Persona[]); setTopics(t as Topic[]); if (bg) setBgUrl(bg); });
  }, []);

  useEffect(() => { clearTimeout(searchTimer.current); searchTimer.current = setTimeout(() => setDebouncedSearch(searchTerm), 400); return () => clearTimeout(searchTimer.current); }, [searchTerm]);

  useEffect(() => { setSignals([]); setHasMore(true); setExpandedId(null); setExpandedSignal(null); setPlayingVideoId(null); setInitialLoad(true); }, [typeFilter, timeRange, minScore, topicFilter, personaFilter, debouncedSearch]);

  const fetchSignals = useCallback(async (offset: number, append: boolean) => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoadingSignals(true);
    try {
      const params = new URLSearchParams();
      params.set('limit', String(LIMIT));
      params.set('offset', String(offset));
      params.set('status', 'relevant');
      if (typeFilter) params.set('type', typeFilter);
      if (minScore > 0) params.set('min_score', String(minScore));
      if (topicFilter) params.set('topic', topicFilter);
      if (personaFilter) params.set('persona', personaFilter);
      if (timeRange) params.set('since', timeRange);
      if (debouncedSearch) params.set('search', debouncedSearch);
      const data = await api.get<SignalListResponse>(`/api/signa/signals?${params}`);
      if (append) { setSignals(prev => [...prev, ...data.signals]); } else { setSignals(data.signals); }
      setSignalTotal(data.total);
      setHasMore(offset + data.signals.length < data.total);
    } catch { /* handled */ }
    finally { loadingRef.current = false; setLoadingSignals(false); setInitialLoad(false); }
  }, [typeFilter, minScore, topicFilter, personaFilter, timeRange, debouncedSearch]);

  useEffect(() => { if (tab === 'signals' && initialLoad) fetchSignals(0, false); }, [tab, initialLoad, fetchSignals]);

  useEffect(() => {
    if (tab !== 'signals') return;
    const sentinel = sentinelRef.current;
    if (!sentinel) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && hasMore && !loadingRef.current && !initialLoad) {
        fetchSignals(signals.length, true);
      }
    }, { rootMargin: '300px' });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [tab, hasMore, initialLoad, signals.length, fetchSignals]);

  const toggleExpand = async (id: number) => {
    if (expandedId === id) { setExpandedId(null); setExpandedSignal(null); return; }
    setExpandedId(id);
    setExpandedSignal(null);
    setLoadingDetail(true);
    try {
      const data = await api.get<Signal>(`/api/signa/signals/${id}`);
      setExpandedSignal(data);
    } catch (err) {
      console.error('Signal-Detail laden fehlgeschlagen:', err);
      setExpandedId(null);
    }
    finally { setLoadingDetail(false); }
  };

  const fetchBriefingItems = useCallback(async () => {
    setLoadingBriefings(true);
    try {
      const items: BriefingItem[] = [];
      if (briefingFilter !== 'deep-dive') {
        const dailies = await api.get<Briefing[]>('/api/signa/briefings?limit=50');
        for (const b of dailies) {
          items.push({ id: b.id, type: 'daily', date: b.briefing_date || b.createdAt || '', signal_count: b.signal_count ?? undefined, avg_score: b.avg_score ?? undefined });
        }
      }
      if (briefingFilter !== 'daily') {
        const dives = await api.get<DeepDiveListItem[]>('/api/signa/deep-dives?limit=50');
        for (const dd of dives) {
          items.push({ id: dd.id, type: 'deep-dive', date: dd.createdAt || '', persona_name: dd.persona_name ?? undefined });
        }
      }
      items.sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime());
      setBriefingItems(items);
    } catch { /* handled */ }
    finally { setLoadingBriefings(false); }
  }, [briefingFilter]);
  useEffect(() => { if (tab === 'briefings') fetchBriefingItems(); }, [tab, fetchBriefingItems]);

  const openBriefingDetail = async (item: BriefingItem) => {
    try {
      if (item.type === 'daily') {
        const b = await api.get<Briefing>(`/api/signa/briefings/${item.id}`);
        setSelectedDetail({ id: item.id, date: item.date, type: 'daily', briefing_text: b.briefing_text, briefing_html: b.briefing_html, audio_url: b.audio_url });
      } else {
        const dd = await api.get<{ briefing_html?: string; briefing_text?: string; persona_name?: string }>(`/api/signa/deep-dives/${item.id}`);
        setSelectedDetail({ id: item.id, date: item.date, type: 'deep-dive', briefing_text: dd.briefing_text, briefing_html: dd.briefing_html, persona_name: dd.persona_name });
      }
    } catch { /* handled */ }
  };

  const handleBgSelect = async (url: string | null) => { await api.patch('/api/settings', { signale_background_url: url }); setBgUrl(url); };

  const activeFilterCount = [typeFilter, timeRange, minScore > 0, topicFilter, personaFilter, debouncedSearch].filter(Boolean).length;

  const resetFilters = () => {
    setTypeFilter('');
    setTimeRange('');
    setMinScore(0);
    setTopicFilter('');
    setPersonaFilter('');
    setSearchTerm('');
  };

  const hasBg = !!bgUrl;
  const isGradient = bgUrl?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient ? { background: bgUrl!.slice('gradient:'.length) } : hasBg ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' } : undefined;

  const glass = hasBg ? 'border-white/10 bg-black/35 backdrop-blur-xl' : 'border-gray-200/60 bg-white/70 backdrop-blur-xl dark:border-gray-800 dark:bg-gray-900/60';
  const cardGlass = hasBg ? 'border-white/10 bg-white/[0.06] backdrop-blur-xl' : 'border-gray-200/70 bg-white/80 backdrop-blur-lg dark:border-white/10 dark:bg-white/[0.06]';
  const cardExpandedGlass = hasBg ? 'border-white/15 bg-white/[0.08] backdrop-blur-xl' : 'border-indigo-200 bg-white/90 backdrop-blur-xl dark:border-indigo-500/30 dark:bg-white/[0.08]';
  const textPrimary = hasBg ? 'text-white' : 'text-gray-900 dark:text-white';
  const textSecondary = hasBg ? 'text-white/70' : 'text-gray-600 dark:text-gray-400';
  const textMuted = hasBg ? 'text-white/40' : 'text-gray-400 dark:text-gray-500';

  return (
    <SignaleErrorBoundary>
    <div className="relative flex h-full flex-col" style={!hasBg ? undefined : bgStyle}>
      {!hasBg && <div className="absolute inset-0 bg-gradient-to-br from-slate-50 via-white to-indigo-50/30 dark:from-gray-950 dark:via-gray-900 dark:to-indigo-950/20" />}
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/30 dark:bg-black/45" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/25" />}

      <div className="relative z-10 flex h-full flex-col">
        {/* Header (desktop only) */}
        <div className={`hidden border-b px-4 py-3 sm:px-6 sm:py-4 md:block ${glass}`}>
          <div className="mx-auto flex max-w-3xl items-center justify-between">
            <div>
              <div className="flex items-center gap-3">
                <h1 className={`text-xl font-bold ${textPrimary}`}>Signale</h1>
                {stats && <span className="rounded-full bg-emerald-500/15 px-2.5 py-0.5 text-xs font-semibold text-emerald-600 dark:text-emerald-400">{stats.total.toLocaleString('de-CH')}</span>}
              </div>
              {stats && (
                <div className={`mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs ${textSecondary}`}>
                  <span className="flex items-center gap-1"><span className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />{stats.today} heute</span>
                  <span>{stats.this_week} diese Woche</span>
                  <span>{stats.high_score} Must-Read</span>
                  <span>&#216; {stats.avg_score}</span>
                  <span>{stats.sources} Quellen</span>
                </div>
              )}
            </div>
            <button onClick={() => setBgPickerOpen(true)} className={`rounded-xl p-2.5 transition-colors ${hasBg ? 'text-white/60 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`} title="Hintergrund ändern">
              <ImageIcon className="h-5 w-5" />
            </button>
          </div>
        </div>

        {/* Tabs (desktop only) */}
        <div className={`hidden border-b px-4 sm:px-6 md:block ${glass}`}>
          <div className="mx-auto flex max-w-3xl items-center gap-1">
            {([{ id: 'signals' as TabId, label: 'Signale' }, { id: 'briefings' as TabId, label: 'Briefings' }]).map(t => (
              <button key={t.id} onClick={() => setTab(t.id)} className={`relative px-4 py-3 text-sm font-medium transition-colors ${tab === t.id ? (hasBg ? 'text-white' : 'text-indigo-600 dark:text-indigo-400') : (hasBg ? 'text-white/50 hover:text-white/80' : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300')}`}>
                {t.label}
                {tab === t.id && <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-indigo-500" />}
              </button>
            ))}
          </div>
        </div>

        {/* Mobile: combined tabs + filter in one line */}
        <div className={`flex items-center justify-between border-b px-3 md:hidden ${glass}`}>
          <div className="flex items-center">
            {([{ id: 'signals' as TabId, label: 'Signale' }, { id: 'briefings' as TabId, label: 'Briefings' }]).map(t => (
              <button key={t.id} onClick={() => setTab(t.id)} className={`relative px-3 py-2.5 text-[13px] font-medium transition-colors ${tab === t.id ? (hasBg ? 'text-white' : 'text-indigo-600 dark:text-indigo-400') : (hasBg ? 'text-white/50' : 'text-gray-500 dark:text-gray-400')}`}>
                {t.label}
                {tab === t.id && <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-indigo-500" />}
              </button>
            ))}
          </div>
          {tab === 'signals' && (
            <div className="flex items-center gap-2">
              <button
                onClick={() => setMobileFilterOpen(v => !v)}
                className={`inline-flex min-h-9 items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                  mobileFilterOpen
                    ? 'bg-indigo-600 text-white'
                    : hasBg
                      ? 'bg-white/15 text-white/80 active:bg-white/25'
                      : 'bg-gray-100 text-gray-600 active:bg-gray-200 dark:bg-gray-800 dark:text-gray-300'
                }`}
              >
                <FilterIcon className="h-3 w-3" />
                Filter
                {activeFilterCount > 0 && (
                  <span className="flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-indigo-500 px-0.5 text-[9px] font-bold leading-none text-white">
                    {activeFilterCount}
                  </span>
                )}
              </button>
              {activeFilterCount > 0 && (
                <button onClick={resetFilters} className={`min-h-9 px-1 text-[11px] lg:min-h-0 lg:px-0 ${hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500'}`}>
                  ×
                </button>
              )}
              <span className={`text-[11px] tabular-nums ${textMuted}`}>{signalTotal.toLocaleString('de-CH')}</span>
            </div>
          )}
        </div>

        {/* ── Signals Tab ── */}
        {tab === 'signals' && (
          <div className="relative flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none" onScroll={() => { if (mobileFilterOpen) setMobileFilterOpen(false); }}>
            {/* Desktop filter bar */}
            <div className={`sticky top-0 z-10 hidden border-b px-4 py-3 sm:px-6 md:block ${glass}`}>
              <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-2">
                <PillGroup options={[{ value: '', label: 'Alle' }, { value: 'today', label: 'Heute' }, { value: 'week', label: 'Woche' }, { value: '2weeks', label: '2 Wochen' }]} value={timeRange} onChange={v => setTimeRange(v as TimeRange)} hasBg={hasBg} />
                <Sep hasBg={hasBg} />
                <PillGroup options={[{ value: '', label: 'Alle' }, { value: 'rss', label: 'Artikel' }, { value: 'youtube', label: 'Videos' }]} value={typeFilter} onChange={v => setTypeFilter(v as TypeFilter)} hasBg={hasBg} />
                <Sep hasBg={hasBg} />
                <PillGroup options={[{ value: '0', label: 'Alle' }, { value: '7', label: '\u2265 7' }, { value: '8', label: '\u2265 8' }, { value: '9', label: 'MUST-READ', accent: true }]} value={String(minScore)} onChange={v => setMinScore(Number(v) as ScoreFilter)} hasBg={hasBg} />
                <Sep hasBg={hasBg} />
                <select value={topicFilter} onChange={e => setTopicFilter(e.target.value)} className={`rounded-lg border px-2.5 py-1.5 text-xs ${hasBg ? 'border-white/15 bg-white/10 text-white' : 'border-gray-200 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>
                  <option value="">Topic</option>
                  {topics.map(t => <option key={t.id} value={t.topic_name}>{t.topic_name}</option>)}
                </select>
                <select value={personaFilter} onChange={e => setPersonaFilter(e.target.value)} className={`rounded-lg border px-2.5 py-1.5 text-xs ${hasBg ? 'border-white/15 bg-white/10 text-white' : 'border-gray-200 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>
                  <option value="">Persona</option>
                  {personas.map(p => <option key={p.id} value={p.persona_name}>{p.persona_name}</option>)}
                </select>
                <div className="ml-auto">
                  <input value={searchTerm} onChange={e => setSearchTerm(e.target.value)} placeholder="Suchen..." className={`w-48 rounded-lg border px-3 py-1.5 text-xs outline-none transition-colors ${hasBg ? 'border-white/15 bg-white/10 text-white placeholder:text-white/40 focus:border-white/30' : 'border-gray-200 bg-white text-gray-900 placeholder:text-gray-400 focus:border-indigo-400 dark:border-gray-700 dark:bg-gray-800 dark:text-white'}`} />
                </div>
              </div>
            </div>

            {/* Mobile pull-down filter sheet */}
            {mobileFilterOpen && (
              <>
                <div className="fixed inset-0 z-20 md:hidden" onClick={() => setMobileFilterOpen(false)} />
                <div className={`absolute inset-x-0 top-0 z-20 animate-[slideDown_150ms_ease-out] border-b px-4 py-4 md:hidden ${glass}`}>
                  <div className="space-y-3">
                    <div>
                      <label className={`mb-1.5 block text-[10px] font-semibold uppercase tracking-wider ${textMuted}`}>Zeitraum</label>
                      <PillGroup options={[{ value: '', label: 'Alle' }, { value: 'today', label: 'Heute' }, { value: 'week', label: 'Woche' }, { value: '2weeks', label: '2 Wo.' }]} value={timeRange} onChange={v => setTimeRange(v as TimeRange)} hasBg={hasBg} />
                    </div>
                    <div>
                      <label className={`mb-1.5 block text-[10px] font-semibold uppercase tracking-wider ${textMuted}`}>Typ</label>
                      <PillGroup options={[{ value: '', label: 'Alle' }, { value: 'rss', label: 'Artikel' }, { value: 'youtube', label: 'Videos' }]} value={typeFilter} onChange={v => setTypeFilter(v as TypeFilter)} hasBg={hasBg} />
                    </div>
                    <div>
                      <label className={`mb-1.5 block text-[10px] font-semibold uppercase tracking-wider ${textMuted}`}>Score</label>
                      <PillGroup options={[{ value: '0', label: 'Alle' }, { value: '7', label: '\u2265 7' }, { value: '8', label: '\u2265 8' }, { value: '9', label: 'MUST-READ', accent: true }]} value={String(minScore)} onChange={v => setMinScore(Number(v) as ScoreFilter)} hasBg={hasBg} />
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className={`mb-1.5 block text-[10px] font-semibold uppercase tracking-wider ${textMuted}`}>Topic</label>
                        <select value={topicFilter} onChange={e => setTopicFilter(e.target.value)} className={`w-full rounded-lg border px-2.5 py-2 text-xs ${hasBg ? 'border-white/15 bg-white/10 text-white' : 'border-gray-200 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>
                          <option value="">Alle</option>
                          {topics.map(t => <option key={t.id} value={t.topic_name}>{t.topic_name}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className={`mb-1.5 block text-[10px] font-semibold uppercase tracking-wider ${textMuted}`}>Persona</label>
                        <select value={personaFilter} onChange={e => setPersonaFilter(e.target.value)} className={`w-full rounded-lg border px-2.5 py-2 text-xs ${hasBg ? 'border-white/15 bg-white/10 text-white' : 'border-gray-200 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>
                          <option value="">Alle</option>
                          {personas.map(p => <option key={p.id} value={p.persona_name}>{p.persona_name}</option>)}
                        </select>
                      </div>
                    </div>
                    <div>
                      <label className={`mb-1.5 block text-[10px] font-semibold uppercase tracking-wider ${textMuted}`}>Suche</label>
                      <input value={searchTerm} onChange={e => setSearchTerm(e.target.value)} placeholder="Suchbegriff..." className={`w-full rounded-lg border px-3 py-2 text-xs outline-none transition-colors ${hasBg ? 'border-white/15 bg-white/10 text-white placeholder:text-white/40 focus:border-white/30' : 'border-gray-200 bg-white text-gray-900 placeholder:text-gray-400 focus:border-indigo-400 dark:border-gray-700 dark:bg-gray-800 dark:text-white'}`} />
                    </div>
                  </div>
                </div>
              </>
            )}

            <div className="mx-auto max-w-3xl px-4 py-4 sm:px-6 sm:py-6">
              {initialLoad && loadingSignals ? (
                <div className="grid gap-5">{Array.from({ length: 4 }).map((_, i) => <SkeletonCard key={i} hasBg={hasBg} />)}</div>
              ) : signals.length === 0 && !loadingSignals ? (
                <div className={`py-20 text-center ${textMuted}`}>
                  <SignalIcon className="mx-auto mb-3 h-10 w-10 opacity-40" />
                  <p className="text-sm">Keine Signale gefunden</p>
                  <p className="mt-1 text-xs">Passe die Filter an oder versuche eine andere Suche</p>
                </div>
              ) : (
                <>
                  <div className="grid gap-5">
                    {signals.map(s => {
                      const isYT = s.type === 'youtube';
                      const isExpanded = expandedId === s.id;
                      const isPlaying = playingVideoId === s.id;
                      const summaryText = isYT ? s.ai_reason : (s.ai_reason || s.description);

                      // YouTube signals: no expand, just play inline
                      if (isYT) {
                        return (
                          <div key={s.id} className={`rounded-2xl border shadow-sm transition-all ${cardGlass}`}>
                            {/* Video area */}
                            {isPlaying && s.url ? (
                              <div className="aspect-video w-full overflow-hidden rounded-t-2xl">
                                <iframe src={getYouTubeEmbedUrl(s.url)} className="h-full w-full" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowFullScreen />
                              </div>
                            ) : (s.thumbnail_url || s.url) ? (
                              <div className="relative cursor-pointer overflow-hidden rounded-t-2xl" onClick={() => setPlayingVideoId(s.id)}>
                                <img
                                  src={getHighResThumbnail(s.url) ?? s.thumbnail_url ?? ''}
                                  alt=""
                                  className="aspect-video w-full object-cover"
                                  loading="lazy"
                                  onError={(e) => {
                                    const img = e.currentTarget;
                                    const id = s.url ? getYouTubeVideoId(s.url) : null;
                                    if (id && img.src.includes('maxresdefault')) {
                                      img.src = `https://img.youtube.com/vi/${id}/sddefault.jpg`;
                                    } else if (id && img.src.includes('sddefault')) {
                                      img.src = `https://img.youtube.com/vi/${id}/hqdefault.jpg`;
                                    } else if (s.thumbnail_url && img.src !== s.thumbnail_url) {
                                      img.src = s.thumbnail_url;
                                    }
                                  }}
                                />
                                <div className="absolute inset-0 flex items-center justify-center bg-black/20 transition-colors hover:bg-black/30">
                                  <div className="flex h-14 w-14 items-center justify-center rounded-full bg-red-600/90 shadow-lg transition-transform hover:scale-110">
                                    <PlayIcon className="h-6 w-6 text-white" />
                                  </div>
                                </div>
                              </div>
                            ) : null}

                            <div className="p-4">
                              <div className="mb-2 flex items-start justify-between gap-2">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  {s.relevant_role && <span className="rounded-full bg-purple-500/15 px-2.5 py-0.5 text-[11px] font-medium text-purple-600 dark:text-purple-300">{s.relevant_role}</span>}
                                  {s.source_name && <span className={`text-[11px] ${textMuted}`}>{s.source_name}</span>}
                                </div>
                                <ScoreBadge score={s.total_score} />
                              </div>
                              <h3 className={`text-sm font-semibold leading-snug ${textPrimary}`}>{s.title}</h3>
                              {summaryText && <p className={`mt-2 text-sm leading-relaxed ${textSecondary}`}>{summaryText}</p>}
                              <div className={`mt-3 flex items-center gap-2 text-[11px] ${textMuted}`}>
                                {s.published_at && <span>{relativeTime(s.published_at)}</span>}
                                {s.topic_name && <span className={`rounded-full px-2 py-0.5 ${hasBg ? 'bg-white/8' : 'bg-gray-100 dark:bg-gray-800'}`}>{s.topic_name}</span>}
                                <TypeBadge type={s.type} />
                              </div>
                            </div>
                          </div>
                        );
                      }

                      // RSS / Web signals
                      const canExpand = !!s.has_full_content;
                      return (
                        <div key={s.id} className={`rounded-2xl border shadow-sm transition-all ${isExpanded ? cardExpandedGlass : `${cardGlass}${canExpand ? ' cursor-pointer' : ''}`}`} onClick={() => canExpand && !isExpanded && toggleExpand(s.id)}>
                          <div className="p-5">
                            <div className="mb-2 flex items-start justify-between gap-2">
                              <div className="flex flex-wrap items-center gap-1.5">
                                {s.relevant_role && <span className="rounded-full bg-purple-500/15 px-2.5 py-0.5 text-[11px] font-medium text-purple-600 dark:text-purple-300">{s.relevant_role}</span>}
                                {s.source_name && <span className={`text-[11px] ${textMuted}`}>{s.source_name}</span>}
                              </div>
                              <div className="flex shrink-0 items-center gap-1.5">
                                <ScoreBadge score={s.total_score} />
                                {isExpanded && (
                                  <button onClick={e => { e.stopPropagation(); toggleExpand(s.id); }} className={`rounded-lg p-1 transition-colors ${hasBg ? 'text-white/50 hover:bg-white/10' : 'text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'}`}>
                                    <CloseIcon className="h-4 w-4" />
                                  </button>
                                )}
                              </div>
                            </div>

                            <h3 className={`text-sm font-semibold leading-snug ${textPrimary}`}>{s.title}</h3>
                            {summaryText && <p className={`mt-2 text-sm leading-relaxed ${textSecondary}`}>{summaryText}</p>}

                            {!isExpanded && (
                              <div className={`mt-3 flex flex-wrap items-center gap-2 text-[11px] ${textMuted}`}>
                                {s.published_at && <span>{relativeTime(s.published_at)}</span>}
                                {s.topic_name && <span className={`rounded-full px-2 py-0.5 ${hasBg ? 'bg-white/8' : 'bg-gray-100 dark:bg-gray-800'}`}>{s.topic_name}</span>}
                                <TypeBadge type={s.type} />
                                {s.url && (
                                  <a href={s.url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} className="ml-auto inline-flex items-center gap-1 text-indigo-600 transition-colors hover:text-indigo-500 dark:text-indigo-400 dark:hover:text-indigo-300">
                                    <LinkIcon className="h-3 w-3" />
                                    <span>Artikel lesen</span>
                                  </a>
                                )}
                                {canExpand && (
                                  <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/20 px-2 py-0.5 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400">
                                    <ChevronDownIcon className="h-3 w-3" />
                                    Volltext verfügbar
                                  </span>
                                )}
                              </div>
                            )}
                          </div>

                          {isExpanded && (
                            <div className="border-t border-inherit px-5 pb-5 pt-4">
                              {loadingDetail ? (
                                <div className="flex h-20 items-center justify-center"><div className="h-6 w-6 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" /></div>
                              ) : expandedSignal ? (
                                <ExpandedContent signal={expandedSignal} fallbackType={s.type} hasBg={hasBg} textSecondary={textSecondary} textMuted={textMuted} />
                              ) : (
                                <p className={`text-sm italic ${textSecondary}`}>Fehler beim Laden der Details.</p>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  <div ref={sentinelRef} className="h-1" />
                  {loadingSignals && !initialLoad && (
                    <div className="mt-4 grid gap-5">{Array.from({ length: 2 }).map((_, i) => <SkeletonCard key={`more-${i}`} hasBg={hasBg} />)}</div>
                  )}
                  {!hasMore && signals.length > 0 && <p className={`mt-6 pb-4 text-center text-xs ${textMuted}`}>Alle {signalTotal.toLocaleString('de-CH')} Signale geladen</p>}
                </>
              )}
            </div>
          </div>
        )}

        {/* ── Briefings Tab ── */}
        {tab === 'briefings' && (
          <div className="flex flex-1 overflow-hidden">
            {/* Left: Briefing list -- hidden on mobile when detail is open */}
            <div className={`flex flex-col ${selectedDetail ? 'hidden md:flex md:w-[340px] md:shrink-0' : 'w-full'}`}>
              <div className={`flex items-center gap-2 border-b px-4 py-3 ${glass}`}>
                <PillGroup options={[{ value: 'all', label: 'Alle' }, { value: 'daily', label: 'Daily' }, { value: 'deep-dive', label: 'Deep Dives' }]} value={briefingFilter} onChange={v => { setBriefingFilter(v as BriefingFilter); setDdPersonaFilter(''); setSelectedDetail(null); }} hasBg={hasBg} />
                {briefingFilter !== 'daily' && (
                  <select value={ddPersonaFilter} onChange={e => setDdPersonaFilter(e.target.value)} className={`ml-auto rounded-lg border px-2 py-1 text-[11px] ${hasBg ? 'border-white/15 bg-white/10 text-white' : 'border-gray-200 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>
                    <option value="">Persona</option>
                    {personas.map(p => <option key={p.id} value={p.persona_name}>{p.persona_name}</option>)}
                  </select>
                )}
              </div>
              <div className="flex-1 overflow-y-auto p-4">
                {loadingBriefings ? (
                  <div className="grid gap-2">{Array.from({ length: 4 }).map((_, i) => <SkeletonCard key={i} hasBg={hasBg} />)}</div>
                ) : briefingItems.length === 0 ? (
                  <div className={`py-16 text-center text-sm ${textMuted}`}>Keine Briefings vorhanden</div>
                ) : (
                  <div className="grid gap-2">
                    {briefingItems.filter(item => !ddPersonaFilter || item.type === 'daily' || item.persona_name === ddPersonaFilter).map(item => (
                      <button key={`${item.type}-${item.id}`} onClick={() => openBriefingDetail(item)} className={`w-full rounded-xl border px-3.5 py-3 text-left shadow-sm transition-all ${selectedDetail?.id === item.id && selectedDetail?.type === item.type ? cardExpandedGlass : cardGlass}`}>
                        <div className="flex items-center gap-2">
                          {item.type === 'daily' ? (
                            <span className="shrink-0 rounded-full bg-indigo-500/15 px-2 py-0.5 text-[10px] font-semibold text-indigo-600 dark:text-indigo-400">Daily</span>
                          ) : (
                            <span className="shrink-0 rounded-full bg-purple-500/15 px-2 py-0.5 text-[10px] font-semibold text-purple-600 dark:text-purple-300">Deep Dive</span>
                          )}
                          <h3 className={`text-sm font-semibold ${textPrimary}`}>
                            {new Date(item.date).toLocaleDateString('de-CH', { day: 'numeric', month: 'short', year: 'numeric' })}
                          </h3>
                        </div>
                        <div className={`mt-1 flex items-center gap-2 text-xs ${textSecondary}`}>
                          {item.type === 'daily' ? (
                            <>
                              {item.signal_count != null && <span>{item.signal_count} Signale</span>}
                              {item.avg_score != null && <><span className={textMuted}>&middot;</span><span>&#216; {item.avg_score.toFixed(1)}</span></>}
                            </>
                          ) : (
                            item.persona_name && <span className="truncate text-[11px]">{item.persona_name}</span>
                          )}
                        </div>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
            {/* Right: Briefing detail -- full width on mobile */}
            {selectedDetail && (
              <div className={`flex-1 overflow-y-auto border-l p-4 sm:p-6 ${hasBg ? 'border-white/10 bg-black/60 backdrop-blur-xl' : 'border-gray-200/60 bg-white/70 backdrop-blur-xl dark:border-gray-800 dark:bg-gray-900/60'}`}>
                <div className="flex items-center justify-between">
                  <div className="flex min-w-0 items-center gap-2">
                    <button onClick={() => setSelectedDetail(null)} className={`shrink-0 rounded-lg p-1.5 transition-colors md:hidden ${hasBg ? 'text-white/60 active:bg-white/10' : 'text-gray-500 active:bg-gray-100 dark:active:bg-gray-800'}`} aria-label="Zurück">
                      <BackIcon className="h-5 w-5" />
                    </button>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        {selectedDetail.type === 'daily' ? (
                          <span className="shrink-0 rounded-full bg-indigo-500/15 px-2.5 py-0.5 text-[11px] font-semibold text-indigo-600 dark:text-indigo-400">Daily Report</span>
                        ) : (
                          <span className="shrink-0 rounded-full bg-purple-500/15 px-2.5 py-0.5 text-[11px] font-semibold text-purple-600 dark:text-purple-300">Deep Dive</span>
                        )}
                        <h2 className={`truncate text-base font-bold sm:text-lg ${textPrimary}`}>{new Date(selectedDetail.date).toLocaleDateString('de-CH', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}</h2>
                      </div>
                      {selectedDetail.persona_name && <p className={`mt-1 text-sm ${textSecondary}`}>{selectedDetail.persona_name}</p>}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <CopyButton text={selectedDetail.briefing_text || ''} hasBg={hasBg} />
                    <button onClick={() => setSelectedDetail(null)} className={`hidden rounded-lg p-1.5 transition-colors md:flex ${hasBg ? 'text-white/50 hover:bg-white/10' : 'text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'}`}><CloseIcon className="h-4 w-4" /></button>
                  </div>
                </div>
                {selectedDetail.audio_url && (
                  <div className={`mt-4 rounded-xl border p-3 ${hasBg ? 'border-white/10 bg-white/5' : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800/50'}`}>
                    <audio src={selectedDetail.audio_url} controls className="w-full" />
                  </div>
                )}
                {selectedDetail.briefing_html ? (
                  <div className={`signa-content mt-4 prose prose-sm max-w-none ${hasBg ? 'prose-invert' : 'dark:prose-invert'}`} dangerouslySetInnerHTML={{ __html: stripInlineStyles(selectedDetail.briefing_html) }} />
                ) : selectedDetail.briefing_text ? (
                  <div className={`mt-4 whitespace-pre-wrap text-sm leading-relaxed ${textSecondary}`}>{selectedDetail.briefing_text}</div>
                ) : (
                  <p className={`mt-4 text-sm ${textMuted}`}>Kein Inhalt verfügbar</p>
                )}
              </div>
            )}
          </div>
        )}

      </div>

      <BackgroundPicker isOpen={bgPickerOpen} onClose={() => setBgPickerOpen(false)} currentUrl={bgUrl} onSelect={handleBgSelect} />
    </div>
    </SignaleErrorBoundary>
  );
}

// ── Sub-Components ──────────────────────────────────

function CopyButton({ text, hasBg }: { text: string; hasBg: boolean }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    await navigator.clipboard.writeText(text.replace(/<[^>]*>/g, ''));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <button onClick={handleCopy} title="In Zwischenablage kopieren" className={`rounded-lg p-1.5 transition-colors ${copied ? 'text-emerald-500' : hasBg ? 'text-white/50 hover:bg-white/10' : 'text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'}`}>
      {copied ? <CheckIcon className="h-4 w-4" /> : <ClipboardIcon className="h-4 w-4" />}
    </button>
  );
}

function ExpandedContent({ signal, fallbackType, hasBg, textSecondary, textMuted }: {
  signal: Signal; fallbackType: string | null; hasBg: boolean; textSecondary: string; textMuted: string;
}) {
  let dateStr = '';
  try {
    if (signal.published_at) dateStr = new Date(signal.published_at).toLocaleString('de-CH');
  } catch { /* ungültiges Datum */ }

  return (
    <>
      <div className={`mb-3 flex flex-wrap items-center gap-3 text-xs ${textMuted}`}>
        {dateStr && <span>{dateStr}</span>}
        {signal.topic_name && <span className={`rounded-full px-2 py-0.5 ${hasBg ? 'bg-white/8' : 'bg-gray-100 dark:bg-gray-800'}`}>{signal.topic_name}</span>}
        <TypeBadge type={signal.type ?? fallbackType} />
        {signal.url && (
          <a href={signal.url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} className="ml-auto inline-flex items-center gap-1 text-indigo-600 transition-colors hover:text-indigo-500 dark:text-indigo-400 dark:hover:text-indigo-300">
            <LinkIcon className="h-3 w-3" />
            <span>Artikel lesen</span>
          </a>
        )}
      </div>
      {signal.full_content ? (
        <ContentBlock content={signal.full_content} hasBg={hasBg} />
      ) : (
        <p className={`text-sm italic ${textSecondary}`}>Kein Volltext verfügbar.</p>
      )}
    </>
  );
}

function SafeMarkdown({ content, className }: { content: string; className: string }) {
  try {
    const formatted = content
      .replace(/\r\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .replace(/(?<!\n)\n(?!\n)/g, '\n\n');
    return <div className={className}><Markdown remarkPlugins={[remarkGfm]}>{formatted}</Markdown></div>;
  } catch {
    return <pre className="whitespace-pre-wrap text-sm">{content}</pre>;
  }
}

function ContentBlock({ content, hasBg }: { content: string; hasBg: boolean }) {
  const [copied, setCopied] = useState(false);
  if (!content) return null;
  const handleCopy = async () => {
    try { await navigator.clipboard.writeText(content.replace(/<[^>]*>/g, '')); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch { /* clipboard unavailable */ }
  };
  const isHtml = /<[a-z][\s\S]*>/i.test(content);
  const proseClass = `signa-content prose prose-base max-w-none leading-relaxed ${hasBg ? 'prose-invert' : 'dark:prose-invert'}`;
  return (
    <div className={`relative mb-4 rounded-xl border ${hasBg ? 'border-white/15 bg-white/10' : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800/40'}`}>
      <button onClick={handleCopy} title="In Zwischenablage kopieren" className={`absolute right-3 top-3 z-10 rounded-lg p-1.5 transition-colors ${copied ? 'text-emerald-500' : hasBg ? 'text-white/50 hover:bg-white/10' : 'text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700'}`}>
        {copied ? <CheckIcon className="h-4 w-4" /> : <ClipboardIcon className="h-4 w-4" />}
      </button>
      <div className={`max-h-[40rem] overflow-y-auto px-6 py-5 pr-14 ${hasBg ? 'text-white' : 'text-gray-700 dark:text-gray-300'}`}>
        {isHtml ? (
          <div className={proseClass} dangerouslySetInnerHTML={{ __html: stripInlineStyles(content) }} />
        ) : (
          <SafeMarkdown content={content} className={proseClass} />
        )}
      </div>
    </div>
  );
}

function PillGroup({ options, value, onChange, hasBg }: { options: { value: string; label: string; accent?: boolean }[]; value: string; onChange: (v: string) => void; hasBg: boolean; }) {
  return (
    <div className="flex items-center gap-1">
      {options.map(opt => {
        const active = value === opt.value;
        const accent = opt.accent && active;
        return (
          <button key={opt.value} onClick={() => onChange(opt.value)} className={`rounded-full px-3 py-1 text-[11px] font-medium transition-all ${accent ? 'bg-red-500/90 text-white shadow-sm' : active ? (hasBg ? 'bg-white/20 text-white shadow-sm' : 'bg-indigo-500/15 text-indigo-600 shadow-sm dark:bg-indigo-500/20 dark:text-indigo-400') : (hasBg ? 'text-white/50 hover:bg-white/10' : 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800')}`}>
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function Sep({ hasBg }: { hasBg: boolean }) { return <div className={`h-4 w-px ${hasBg ? 'bg-white/15' : 'bg-gray-200 dark:bg-gray-700'}`} />; }

function SkeletonCard({ hasBg }: { hasBg: boolean }) {
  return (
    <div className={`animate-pulse rounded-2xl border p-5 ${hasBg ? 'border-white/10 bg-white/5' : 'border-gray-200/70 bg-gray-100/50 dark:border-gray-800 dark:bg-gray-800/30'}`}>
      <div className="flex items-center gap-2"><div className={`h-4 w-24 rounded-full ${hasBg ? 'bg-white/10' : 'bg-gray-200 dark:bg-gray-700'}`} /><div className={`h-4 w-16 rounded-full ${hasBg ? 'bg-white/10' : 'bg-gray-200 dark:bg-gray-700'}`} /></div>
      <div className={`mt-3 h-4 w-3/4 rounded ${hasBg ? 'bg-white/10' : 'bg-gray-200 dark:bg-gray-700'}`} />
      <div className={`mt-2 h-3 w-full rounded ${hasBg ? 'bg-white/8' : 'bg-gray-200/70 dark:bg-gray-700/60'}`} />
      <div className={`mt-1.5 h-3 w-5/6 rounded ${hasBg ? 'bg-white/8' : 'bg-gray-200/70 dark:bg-gray-700/60'}`} />
      <div className={`mt-1.5 h-3 w-2/3 rounded ${hasBg ? 'bg-white/8' : 'bg-gray-200/70 dark:bg-gray-700/60'}`} />
    </div>
  );
}

function ScoreBadge({ score }: { score: number | null }) {
  if (score == null) return null;
  const isMustRead = score >= 9;
  const isHigh = score >= 7;
  const color = isMustRead ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' : isHigh ? 'bg-amber-500/15 text-amber-600 dark:text-amber-400' : 'bg-gray-500/10 text-gray-500 dark:text-gray-400';
  return (
    <span className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-bold ${color}`}>
      {typeof score === 'number' ? score.toFixed(1) : score}
      {isMustRead && <span className="text-[9px] font-semibold uppercase tracking-wider">MUST-READ</span>}
    </span>
  );
}

function TypeBadge({ type }: { type: string | null }) {
  if (!type) return null;
  const cfg: Record<string, string> = { rss: 'bg-orange-500/10 text-orange-600 dark:text-orange-400', youtube: 'bg-red-500/10 text-red-600 dark:text-red-400', web: 'bg-blue-500/10 text-blue-600 dark:text-blue-400' };
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ${cfg[type] || cfg.web}`}>{type}</span>;
}

// ── Icons ──

function ImageIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5a2.25 2.25 0 0 0 2.25-2.25V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" /></svg>; }
function CloseIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>; }
function BackIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>; }
function ChevronDownIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" /></svg>; }
function LinkIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M13.19 8.688a4.5 4.5 0 0 1 1.242 7.244l-4.5 4.5a4.5 4.5 0 0 1-6.364-6.364l1.757-1.757m13.35-.622 1.757-1.757a4.5 4.5 0 0 0-6.364-6.364l-4.5 4.5a4.5 4.5 0 0 0 1.242 7.244" /></svg>; }
function PlayIcon({ className }: { className?: string }) { return <svg className={className} fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>; }
function SignalIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9.348 14.652a3.75 3.75 0 0 1 0-5.304m5.304 0a3.75 3.75 0 0 1 0 5.304m-7.425 2.121a6.75 6.75 0 0 1 0-9.546m9.546 0a6.75 6.75 0 0 1 0 9.546M5.106 18.894c-3.808-3.807-3.808-9.98 0-13.788m13.788 0c3.808 3.807 3.808 9.98 0 13.788M12 12h.008v.008H12V12Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" /></svg>; }
function ClipboardIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M15.666 3.888A2.25 2.25 0 0 0 13.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 0 1-.75.75H9.75a.75.75 0 0 1-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 0 1-2.25 2.25H6.75A2.25 2.25 0 0 1 4.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 0 1 1.927-.184" /></svg>; }
function CheckIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>; }
function FilterIcon({ className }: { className?: string }) { return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M10.5 6h9.75M10.5 6a1.5 1.5 0 1 1-3 0m3 0a1.5 1.5 0 1 0-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m-9.75 0h9.75" /></svg>; }
