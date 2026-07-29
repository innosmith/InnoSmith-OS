import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ListChecks, ScrollText, FileText, BrainCircuit, Video } from 'lucide-react';
import { api } from '../api/client';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { FormattedOutput } from '../components/FormattedOutput';
import { ReplayPanel } from '../components/ReplayPanel';
import { TracePanel } from '../components/TracePanel';
import { ApprovalCard } from '../components/agent/ApprovalCard';
import { RulesManager } from '../components/agent/RulesManager';
import { SkillsPanel } from '../components/agent/SkillsPanel';
import { KnowledgePanel } from '../components/agent/KnowledgePanel';
import { MeetingsPanel } from '../components/agent/MeetingsPanel';
import { useSSE } from '../hooks/useSSE';
import type { AgentJob } from '../types';

type FilterStatus = 'all' | 'active' | 'completed' | 'failed';
type FilterType = 'all' | 'chat_agent' | 'chat_triage' | 'email_triage' | 'send_email' | 'briefing' | 'other';
type AgentTab = 'auftraege' | 'meetings' | 'regeln' | 'skills' | 'wissen';

const BRIEFING_TYPES = ['daily_briefing', 'weekly_briefing', 'monthly_briefing'];

const AGENT_TABS: { id: AgentTab; label: string; icon: typeof ListChecks }[] = [
  { id: 'auftraege', label: 'Aufträge', icon: ListChecks },
  { id: 'meetings', label: 'Meetings', icon: Video },
  { id: 'regeln', label: 'Regeln', icon: ScrollText },
  { id: 'skills', label: 'Skills', icon: FileText },
  { id: 'wissen', label: 'Wissen', icon: BrainCircuit },
];

const STATUS_CONFIG: Record<
  AgentJob['status'],
  { label: string; color: string; bg: string }
> = {
  planned: { label: 'Geplant', color: 'text-indigo-700', bg: 'bg-indigo-100 dark:bg-indigo-900/40 dark:text-indigo-300' },
  queued: { label: 'Wartend', color: 'text-amber-700', bg: 'bg-amber-100 dark:bg-amber-900/40 dark:text-amber-300' },
  running: { label: 'Läuft', color: 'text-blue-700', bg: 'bg-blue-100 dark:bg-blue-900/40 dark:text-blue-300' },
  awaiting_approval: { label: 'Prüfung', color: 'text-purple-700', bg: 'bg-purple-100 dark:bg-purple-900/40 dark:text-purple-300' },
  completed: { label: 'Erledigt', color: 'text-green-700', bg: 'bg-green-100 dark:bg-green-900/40 dark:text-green-300' },
  failed: { label: 'Fehler', color: 'text-red-700', bg: 'bg-red-100 dark:bg-red-900/40 dark:text-red-300' },
  blocked: { label: 'Blockiert', color: 'text-gray-600', bg: 'bg-gray-200 dark:bg-gray-700 dark:text-gray-300' },
};

const TYPE_FILTERS: { id: FilterType; label: string }[] = [
  { id: 'all', label: 'Alle Typen' },
  { id: 'chat_agent', label: 'Chat-Agent' },
  { id: 'chat_triage', label: 'Chat-Triage' },
  { id: 'email_triage', label: 'E-Mail-Triage' },
  { id: 'send_email', label: 'E-Mail-Versand' },
  { id: 'briefing', label: 'Briefings' },
  { id: 'other', label: 'Sonstige' },
];

export function AgentQueuePage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get('tab') as AgentTab) || 'auftraege';
  const setTab = (t: AgentTab) =>
    setSearchParams(t === 'auftraege' ? {} : { tab: t }, { replace: true });
  const [jobs, setJobs] = useState<AgentJob[]>([]);
  const [filter, setFilter] = useState<FilterStatus>('all');
  // Deeplink z. B. /agenten?type=briefing (BriefingCard-Archivlink im Cockpit)
  const [typeFilter, setTypeFilter] = useState<FilterType>(() => {
    const t = searchParams.get('type');
    return t && TYPE_FILTERS.some(f => f.id === t) ? (t as FilterType) : 'all';
  });
  const [loading, setLoading] = useState(true);
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const cleanupRef = useRef<HTMLDivElement>(null);
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const fetchJobs = useCallback(async () => {
    try {
      const data = await api.get<AgentJob[]>('/api/agent-jobs');
      setJobs(data);
    } catch { /* handled */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    fetchJobs();
    api.get<{ agents_background_url: string | null }>('/api/settings')
      .then(s => setBgUrl(s.agents_background_url))
      .catch(() => {});
  }, [fetchJobs]);

  useSSE((event) => {
    if (event === 'agent_jobs_changed') fetchJobs();
  });

  const handleDelete = async (jobId: string) => {
    setActionError(null);
    try {
      await api.delete(`/api/agent-jobs/${jobId}`);
      fetchJobs();
    } catch (e) {
      console.error('Agent-Job löschen fehlgeschlagen', e);
      setActionError(
        e instanceof Error ? e.message.slice(0, 200) : 'Löschen fehlgeschlagen',
      );
    }
  };

  const handleCancel = async (jobId: string) => {
    try {
      await api.patch(`/api/agent-jobs/${jobId}`, { status: 'failed', error_message: 'Manuell abgebrochen' });
      fetchJobs();
    } catch { /* silent */ }
  };

  const handleBulkDelete = async (status: string, olderThanDays?: number, forceJobType?: string | null) => {
    setActionError(null);
    const params = new URLSearchParams({ status });
    if (olderThanDays != null) params.set('older_than_days', String(olderThanDays));
    const jt = forceJobType !== undefined ? forceJobType : (typeFilter !== 'all' ? typeFilter : null);
    if (jt && jt !== 'other') params.set('job_type', jt);
    try {
      await api.delete(`/api/agent-jobs/bulk?${params}`);
      setCleanupOpen(false);
      fetchJobs();
    } catch (e) {
      console.error('Bulk-Löschen fehlgeschlagen', e);
      setActionError(
        e instanceof Error ? e.message.slice(0, 200) : 'Aufräumen fehlgeschlagen',
      );
    }
  };

  useEffect(() => {
    if (!cleanupOpen) return;
    const handle = (e: MouseEvent) => {
      if (cleanupRef.current && !cleanupRef.current.contains(e.target as Node)) setCleanupOpen(false);
    };
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, [cleanupOpen]);

  const filtered = jobs.filter((j) => {
    if (filter === 'active' && !['queued', 'running', 'awaiting_approval'].includes(j.status)) return false;
    if (filter === 'completed' && j.status !== 'completed') return false;
    if (filter === 'failed' && j.status !== 'failed') return false;

    if (typeFilter === 'chat_agent' && j.job_type !== 'chat_agent') return false;
    if (typeFilter === 'chat_triage' && j.job_type !== 'chat_triage') return false;
    if (typeFilter === 'email_triage' && j.job_type !== 'email_triage') return false;
    if (typeFilter === 'send_email' && j.job_type !== 'send_email') return false;
    if (typeFilter === 'briefing' && !BRIEFING_TYPES.includes(j.job_type || '')) return false;
    if (
      typeFilter === 'other'
      && ['chat_agent', 'chat_triage', 'email_triage', 'send_email', ...BRIEFING_TYPES].includes(j.job_type || '')
    ) {
      return false;
    }

    return true;
  });

  const activeCount = jobs.filter((j) => ['queued', 'running', 'awaiting_approval'].includes(j.status)).length;
  const runningCount = jobs.filter((j) => j.status === 'running').length;
  const completedCount = jobs.filter((j) => j.status === 'completed').length;
  const failedCount = jobs.filter((j) => j.status === 'failed').length;
  const chatAgentCount = jobs.filter((j) => j.job_type === 'chat_agent').length;
  const chatTriageCount = jobs.filter((j) => j.job_type === 'chat_triage').length;
  const triageCount = jobs.filter((j) => j.job_type === 'email_triage').length;
  const briefingCount = jobs.filter((j) => BRIEFING_TYPES.includes(j.job_type || '')).length;

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { agents_background_url: url });
    setBgUrl(url);
  };

  const hasBg = !!bgUrl;
  const isGradient = bgUrl?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient
    ? { background: bgUrl!.slice('gradient:'.length) }
    : hasBg
      ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' }
      : undefined;

  return (
    <div className="relative flex h-full flex-col" style={!hasBg ? undefined : bgStyle}>
      {!hasBg && <div className="absolute inset-0 bg-gradient-to-br from-slate-50 via-white to-indigo-50/30 dark:from-gray-950 dark:via-gray-900 dark:to-indigo-950/20" />}
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/25 dark:bg-black/40" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/25" />}

      <div className="relative z-10 flex h-full flex-col">
      {/* Mobil trägt die Tab-Leiste Zähler und Hintergrund-Button — eine eigene
          Kopfzeile wäre dort nur verlorene Höhe. */}
      <div className={`hidden border-b px-4 py-2 backdrop-blur-xl sm:px-6 lg:block lg:py-4 ${hasBg ? 'border-white/10 bg-black/35' : 'border-white/40 bg-white/50 dark:border-gray-800 dark:bg-gray-900/50'}`}>
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <h1 className={`hidden text-xl font-bold lg:block ${hasBg ? 'text-white' : 'text-gray-900 dark:text-white'}`}>
                Agenten
              </h1>
              {activeCount > 0 && (
                <span className="flex items-center gap-1.5 rounded-full bg-indigo-100 px-2.5 py-0.5 text-xs font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-indigo-500" />
                  {activeCount} aktiv
                </span>
              )}
            </div>
            <p className={`mt-0.5 hidden text-sm lg:block ${hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'}`}>
              Aufträge, Regeln, Skills und Wissen des Agenten
            </p>
          </div>
          <button
            onClick={() => setBgPickerOpen(true)}
            className={`shrink-0 rounded-lg p-2 transition-colors ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`}
            title="Hintergrund ändern"
          >
            <svg className="h-4.5 w-4.5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5a2.25 2.25 0 0 0 2.25-2.25V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
            </svg>
          </button>
        </div>
      </div>

      {/* Haupt-Tabs */}
      <div className={`relative z-10 flex items-center gap-2 border-b px-4 backdrop-blur-sm sm:px-6 ${hasBg ? 'border-white/10 bg-black/30' : 'border-white/40 bg-white/50 dark:border-gray-800 dark:bg-gray-900/50'}`}>
        <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {AGENT_TABS.map((t) => {
            const active = tab === t.id;
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium transition-colors ${
                  active
                    ? hasBg ? 'border-white text-white' : 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                    : hasBg
                      ? 'border-transparent text-white/70 hover:text-white'
                      : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
                }`}
              >
                <Icon className="h-4 w-4" /> {t.label}
              </button>
            );
          })}
        </div>
        <div className="flex shrink-0 items-center gap-1 lg:hidden">
          {activeCount > 0 && (
            <span className="flex items-center gap-1 rounded-full bg-indigo-100 px-2 py-0.5 text-[11px] font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-500" />
              {activeCount}
            </span>
          )}
          <button
            onClick={() => setBgPickerOpen(true)}
            className={`flex h-11 w-9 items-center justify-center rounded-lg ${hasBg ? 'text-white/70' : 'text-gray-400 dark:text-gray-500'}`}
            aria-label="Hintergrund ändern"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5a2.25 2.25 0 0 0 2.25-2.25V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
            </svg>
          </button>
        </div>
      </div>

      {tab === 'auftraege' && (
      <div className={`relative z-10 border-b px-4 py-2 backdrop-blur-sm sm:px-6 sm:py-3 ${hasBg ? 'border-white/10 bg-black/30' : 'border-white/40 bg-white/50 dark:border-gray-800 dark:bg-gray-900/50'}`}>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          {/* Status- und Typ-Chips mobil in einer Scrollzeile: spart eine Reihe Chrome. */}
          <div className="flex min-w-0 items-center gap-2 overflow-x-auto sm:gap-4 sm:overflow-visible [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            <div className="flex shrink-0 gap-1.5 sm:overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
              {(['all', 'active', 'completed', 'failed'] as FilterStatus[]).map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`min-h-9 shrink-0 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors lg:min-h-0 ${
                    filter === f
                      ? 'bg-indigo-600 text-white'
                      : hasBg
                        ? 'bg-white/10 text-white/80 hover:bg-white/20'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                  }`}
                >
                  {{ all: 'Alle', active: 'Aktiv', completed: 'Erledigt', failed: 'Fehler' }[f]}
                </button>
              ))}
            </div>
            <div className={`h-5 w-px shrink-0 ${hasBg ? 'bg-white/20' : 'bg-gray-200 dark:bg-gray-700'}`} />
            <div className="flex shrink-0 gap-1.5 sm:overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
              {TYPE_FILTERS.map((tf) => {
                const count = tf.id === 'chat_agent' ? chatAgentCount
                  : tf.id === 'chat_triage' ? chatTriageCount
                  : tf.id === 'email_triage' ? triageCount
                  : tf.id === 'briefing' ? briefingCount
                  : undefined;
                return (
                  <button
                    key={tf.id}
                    onClick={() => setTypeFilter(tf.id)}
                    className={`min-h-9 shrink-0 rounded-lg px-2.5 py-1.5 text-[11px] font-medium whitespace-nowrap transition-colors lg:min-h-0 ${
                      typeFilter === tf.id
                        ? 'bg-violet-600 text-white'
                        : hasBg
                          ? 'bg-white/10 text-white/80 hover:bg-white/20'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                    }`}
                  >
                    {tf.label}
                    {count != null && count > 0 && (
                      <span className="ml-1 text-[9px] opacity-70">({count})</span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
          {(completedCount > 0 || failedCount > 0 || runningCount > 0) && (
            <div className="relative" ref={cleanupRef}>
              <button
                onClick={() => setCleanupOpen(!cleanupOpen)}
                className={`flex min-h-9 items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors lg:min-h-0 ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200'}`}
              >
                <TrashIcon className="h-3.5 w-3.5" />
                Aufräumen
              </button>
              {cleanupOpen && (() => {
                const typeLabel = TYPE_FILTERS.find((t) => t.id === typeFilter)?.label ?? '';
                const isFiltered = typeFilter !== 'all';
                const fJobs = isFiltered ? filtered : jobs;
                const fCompleted = fJobs.filter((j) => j.status === 'completed').length;
                const fFailed = fJobs.filter((j) => j.status === 'failed').length;
                const fRunning = fJobs.filter((j) => j.status === 'running').length;
                const prefix = isFiltered ? `${typeLabel}: ` : '';
                return (
                <div className="absolute right-0 top-9 z-50 w-80 rounded-xl border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                  {isFiltered && (
                    <div className="px-4 py-2 text-[10px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">
                      Filter: {typeLabel}
                    </div>
                  )}
                  {fRunning > 0 && (
                    <>
                      <button
                        onClick={() => handleBulkDelete('stale')}
                        className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm text-amber-700 transition-colors hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950/30"
                      >
                        <span>{prefix}Hängende abbrechen</span>
                        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                          {fRunning}
                        </span>
                      </button>
                      <div className="mx-3 my-1 border-t border-gray-100 dark:border-gray-800" />
                    </>
                  )}
                  {fCompleted > 0 && (
                    <button
                      onClick={() => handleBulkDelete('completed')}
                      className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                    >
                      <span>{prefix}Erledigte löschen</span>
                      <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/40 dark:text-green-300">
                        {fCompleted}
                      </span>
                    </button>
                  )}
                  {fFailed > 0 && (
                    <button
                      onClick={() => handleBulkDelete('failed')}
                      className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                    >
                      <span>{prefix}Fehlgeschlagene löschen</span>
                      <span className="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-medium text-red-700 dark:bg-red-900/40 dark:text-red-300">
                        {fFailed}
                      </span>
                    </button>
                  )}
                  {fCompleted > 0 && (
                    <button
                      onClick={() => handleBulkDelete('completed', 7)}
                      className="flex w-full items-center px-4 py-2.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                    >
                      {prefix}Erledigte älter als 7 Tage
                    </button>
                  )}
                  {(fCompleted > 0 || fFailed > 0) && (
                    <>
                      <div className="mx-3 my-1 border-t border-gray-100 dark:border-gray-800" />
                      <button
                        onClick={() => handleBulkDelete('both')}
                        className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/30"
                      >
                        <span>{isFiltered ? `${typeLabel}: Alles aufräumen` : 'Alles aufräumen'}</span>
                        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                          {fCompleted + fFailed}
                        </span>
                      </button>
                    </>
                  )}
                  {isFiltered && (completedCount + failedCount) > (fCompleted + fFailed) && (
                    <>
                      <div className="mx-3 my-1 border-t border-gray-100 dark:border-gray-800" />
                      <button
                        onClick={() => handleBulkDelete('both', undefined, null)}
                        className="flex w-full items-center justify-between px-4 py-2.5 text-left text-sm text-red-400 transition-colors hover:bg-red-50 dark:text-red-500 dark:hover:bg-red-950/30"
                      >
                        <span>Alle Typen aufräumen</span>
                        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                          {completedCount + failedCount}
                        </span>
                      </button>
                    </>
                  )}
                </div>
                );
              })()}
            </div>
          )}
        </div>
        {actionError && (
          <div
            className={`mt-2 rounded-lg border px-3 py-2 text-xs ${
              hasBg
                ? 'border-red-400/50 bg-red-950/40 text-red-100'
                : 'border-red-200 bg-red-50 text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200'
            }`}
            role="alert"
          >
            <span className="font-medium">Aktion fehlgeschlagen: </span>
            {actionError}
            <button
              type="button"
              onClick={() => setActionError(null)}
              className="ml-2 underline"
            >
              Schliessen
            </button>
          </div>
        )}
      </div>
      )}

      {tab === 'auftraege' && (
      <div className="relative z-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
        {loading ? (
          <div className="flex h-48 items-center justify-center">
            <div className="h-10 w-10 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
          </div>
        ) : (
        <>
        {/* Replay-Panel */}
        <div className="mb-4">
          <ReplayPanel onJobCreated={fetchJobs} />
        </div>

        {filtered.length === 0 ? (
          <div className="flex h-48 items-center justify-center text-gray-400 dark:text-gray-600">
            <p>Keine Jobs gefunden</p>
          </div>
        ) : (
          <div className="space-y-3">
            {filtered.map((job) => {
              const cfg = STATUS_CONFIG[job.status];
              const isExpanded = expandedJobId === job.id;
              const meta = (job.metadata_json ?? {}) as Record<string, string>;
              const jobTitle = _jobDisplayTitle(job, meta);
              const jobSubtitle = _jobSubtitle(job, meta);
              const jobTypeBadge = _jobTypeBadge(job);
              return (
                <div
                  key={job.id}
                  className={`rounded-xl p-4 shadow-sm transition-shadow hover:shadow-md ${
                    hasBg
                      ? 'bg-black/30 backdrop-blur-xl ring-1 ring-white/10'
                      : 'border border-white/60 bg-white/70 backdrop-blur-sm dark:border-gray-800 dark:bg-gray-900/70'
                  }`}
                >
                  {/* Mobil Badges als eigene Zeile über dem Titel — nebeneinander
                      blieben dem Titel nur ~130px und jedes Wort brach um. */}
                  <div className="flex flex-col-reverse gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-3">
                    <div className="min-w-0 flex-1">
                      <button
                        onClick={() => setExpandedJobId(isExpanded ? null : job.id)}
                        className="text-left"
                      >
                        <h3 className={`text-sm font-semibold ${hasBg ? 'text-white drop-shadow-sm' : 'text-gray-900 dark:text-white'}`}>
                          {jobTitle}
                        </h3>
                      </button>
                      {jobSubtitle && (
                        <p className={`mt-0.5 text-xs ${hasBg ? 'text-white/80 drop-shadow-sm' : 'text-gray-500 dark:text-gray-400'}`}>
                          {jobSubtitle}
                        </p>
                      )}
                      <p className={`mt-0.5 text-xs ${hasBg ? 'text-white/60 drop-shadow-sm' : 'text-gray-400 dark:text-gray-500'}`}>
                        {new Date(job.created_at).toLocaleString('de-DE')}
                        {job.llm_model && ` · ${job.llm_model}`}
                        {job.tokens_used != null && ` · ${job.tokens_used} Tokens`}
                        {job.cost_usd != null && ` · $${job.cost_usd.toFixed(4)}`}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5 sm:gap-2">
                      {jobTypeBadge && (
                        <span className={`shrink-0 rounded-full px-2.5 py-0.5 text-[10px] font-semibold leading-5 ${jobTypeBadge.classes}`}>
                          {jobTypeBadge.label}
                        </span>
                      )}
                      <span className={`shrink-0 rounded-full px-2.5 py-0.5 text-[10px] font-medium leading-5 ${cfg.bg}`}>
                        {cfg.label}
                      </span>
                      {job.job_type === 'chat_agent' && meta.conversation_id && (
                        <button
                          onClick={(e) => { e.stopPropagation(); navigate(`/chat?conv=${meta.conversation_id}`); }}
                          className={`flex h-10 w-10 items-center justify-center rounded-lg transition-colors sm:h-auto sm:w-auto sm:p-1.5 ${hasBg ? 'text-white/70 hover:bg-white/20 hover:text-white' : 'text-gray-400 hover:bg-indigo-50 hover:text-indigo-600 dark:text-gray-500 dark:hover:bg-indigo-950/30 dark:hover:text-indigo-400'}`}
                          title="Zur Konversation"
                        >
                          <ChatIcon className="h-4 w-4" />
                        </button>
                      )}
                      {(job.status === 'completed' || job.status === 'failed' || job.status === 'awaiting_approval') && (
                          <button
                            onClick={(e) => { e.stopPropagation(); handleDelete(job.id); }}
                            className={`flex h-10 w-10 items-center justify-center rounded-lg transition-colors sm:h-auto sm:w-auto sm:p-1.5 ${hasBg ? 'text-white/70 hover:bg-white/20 hover:text-white' : 'text-gray-400 hover:bg-red-50 hover:text-red-500 dark:text-gray-500 dark:hover:bg-red-950/30 dark:hover:text-red-400'}`}
                            title="Löschen"
                          >
                            <TrashIcon className="h-4 w-4" />
                          </button>
                      )}
                      {(job.status === 'running' || job.status === 'queued') && (
                          <button
                            onClick={(e) => { e.stopPropagation(); handleCancel(job.id); }}
                            className={`flex h-10 w-10 items-center justify-center rounded-lg transition-colors sm:h-auto sm:w-auto sm:p-1.5 ${hasBg ? 'text-white/70 hover:bg-white/20 hover:text-white' : 'text-gray-400 hover:bg-amber-50 hover:text-amber-500 dark:text-gray-500 dark:hover:bg-amber-950/30 dark:hover:text-amber-400'}`}
                            title="Abbrechen"
                          >
                            <StopIcon className="h-4 w-4" />
                          </button>
                      )}
                    </div>
                  </div>

                  {job.status === 'awaiting_approval' && (
                    <div className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-800">
                      <ApprovalCard
                        jobId={job.id}
                        meta={job.metadata_json ?? {}}
                        confidence={typeof meta.confidence === 'number' ? meta.confidence : (meta.confidence ? Number(meta.confidence) : null)}
                        rationale={(meta.rationale as string) || (meta.summary as string) || null}
                        output={job.output}
                        onResolved={fetchJobs}
                      />
                    </div>
                  )}

                  {/* Kompakter Trace-Button + Nochmals triagieren */}
                  {['completed', 'failed'].includes(job.status) && (
                    <div className="mt-1 flex items-center gap-2">
                      <TracePanel jobId={job.id} compact />
                      {job.job_type === 'email_triage' && meta.message_id && (
                        <ReplayButton messageId={meta.message_id} onDone={fetchJobs} />
                      )}
                    </div>
                  )}

                  {isExpanded && (
                    <div className="mt-3 space-y-2 border-t border-gray-100 pt-3 dark:border-gray-800">
                      {job.output && (
                        <div>
                          <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400">Output</h4>
                          <div className="mt-1 max-h-64 overflow-y-auto rounded-lg bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-800 dark:text-gray-200">
                            <FormattedOutput output={job.output} />
                          </div>
                        </div>
                      )}
                      {job.error_message && (
                        <div>
                          <h4 className="text-xs font-medium text-red-500">Fehler</h4>
                          <p className="mt-1 text-sm text-red-600 wrap-anywhere dark:text-red-400">{job.error_message}</p>
                        </div>
                      )}
                      {!job.output && !job.error_message && (
                        <p className="text-xs text-gray-400 italic">Noch kein Output vorhanden</p>
                      )}
                      <TracePanel jobId={job.id} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        </>
        )}
      </div>
      )}

      {tab === 'meetings' && (
        <div className="relative z-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
          <MeetingsPanel />
        </div>
      )}
      {tab === 'regeln' && (
        <div className="relative z-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
          <RulesManager />
        </div>
      )}
      {tab === 'skills' && (
        <div className="relative z-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
          <SkillsPanel />
        </div>
      )}
      {tab === 'wissen' && (
        <div className="relative z-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
          <KnowledgePanel />
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

const BRIEFING_TITLES: Record<string, string> = {
  daily_briefing: 'Tagesbriefing',
  weekly_briefing: 'Wochenbriefing',
  monthly_briefing: 'Monatsbriefing',
};

function _jobDisplayTitle(job: AgentJob, meta: Record<string, string>): string {
  if (BRIEFING_TITLES[job.job_type || '']) {
    return `${BRIEFING_TITLES[job.job_type!]} vom ${new Date(job.created_at).toLocaleDateString('de-CH', { day: 'numeric', month: 'short', year: 'numeric' })}`;
  }
  if (job.job_type === 'meeting_summary') {
    return `Meeting-Protokoll: ${meta.subject || '(ohne Betreff)'}`;
  }
  if (job.job_type === 'chat_agent') {
    const preview = meta.prompt_preview;
    if (preview) return preview.length > 80 ? preview.slice(0, 80) + '…' : preview;
    return 'Chat-Agent-Anfrage';
  }
  if (job.job_type === 'chat_triage') {
    const preview = meta.body_preview;
    if (preview) return preview.length > 80 ? preview.slice(0, 80) + '…' : preview;
    return 'Teams-Chat-Triage';
  }
  if (job.job_type === 'email_triage') {
    const subject = meta.subject;
    if (subject) return subject;
    return 'E-Mail-Triage';
  }
  if (job.job_type === 'send_email') {
    return `E-Mail senden: ${meta.subject || '(kein Betreff)'}`;
  }
  return job.task_title || meta.subject || `Agent-Job: ${job.job_type || 'unbekannt'}`;
}

function _jobSubtitle(job: AgentJob, meta: Record<string, string>): string | null {
  if (job.job_type === 'chat_agent') {
    const tools = meta.tools_used;
    if (tools) {
      const toolList = Array.isArray(tools) ? tools : [];
      return toolList.length > 0 ? `Tools: ${toolList.join(', ')}` : null;
    }
    return null;
  }
  if (job.job_type === 'chat_triage') {
    const parts: string[] = [];
    if (meta.from_name) {
      parts.push(`Von: ${meta.from_name}`);
    }
    if (meta.chat_type) {
      parts.push(meta.chat_type);
    }
    if (meta.body_preview) {
      const preview = meta.body_preview.length > 120 ? meta.body_preview.slice(0, 120) + '…' : meta.body_preview;
      parts.push(preview);
    }
    return parts.join(' · ') || null;
  }
  if (job.job_type === 'email_triage') {
    const parts: string[] = [];
    if (meta.from_name || meta.from_address) {
      parts.push(`Von: ${meta.from_name || ''} ${meta.from_address ? `<${meta.from_address}>` : ''}`.trim());
    }
    if (meta.body_preview) {
      const preview = meta.body_preview.length > 120 ? meta.body_preview.slice(0, 120) + '…' : meta.body_preview;
      parts.push(preview);
    }
    return parts.join(' · ') || null;
  }
  if (job.job_type === 'send_email' && meta.to_recipients) {
    return `An: ${meta.to_recipients}`;
  }
  return null;
}

function _jobTypeBadge(job: AgentJob): { label: string; classes: string } | null {
  switch (job.job_type) {
    case 'chat_agent':
      return { label: 'Chat-Agent', classes: 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300' };
    case 'chat_triage':
      return { label: 'Chat-Triage', classes: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-300' };
    case 'email_triage':
      return { label: 'E-Mail-Triage', classes: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300' };
    case 'send_email':
      return { label: 'E-Mail-Versand', classes: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300' };
    case 'recurring':
      return { label: 'Recurring', classes: 'bg-teal-100 text-teal-700 dark:bg-teal-900/40 dark:text-teal-300' };
    case 'daily_briefing':
      return { label: 'Tagesbriefing', classes: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300' };
    case 'weekly_briefing':
      return { label: 'Wochenbriefing', classes: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300' };
    case 'monthly_briefing':
      return { label: 'Monatsbriefing', classes: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300' };
    case 'meeting_summary':
      return { label: 'Meeting-Protokoll', classes: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300' };
    default:
      return job.job_type
        ? { label: job.job_type, classes: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300' }
        : null;
  }
}

function TrashIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" />
    </svg>
  );
}

function ReplayButton({ messageId, onDone }: { messageId: string; onDone: () => void }) {
  const [running, setRunning] = useState(false);
  const handleReplay = async () => {
    setRunning(true);
    try {
      await api.post('/api/triage/replay', { message_id: messageId });
      onDone();
    } catch { /* */ }
    finally { setRunning(false); }
  };
  return (
    <button
      onClick={handleReplay}
      disabled={running}
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium text-indigo-500 transition-colors hover:bg-indigo-50 hover:text-indigo-700 disabled:opacity-50 dark:text-indigo-400 dark:hover:bg-indigo-950/30 dark:hover:text-indigo-300"
    >
      <svg className="h-2.5 w-2.5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182" />
      </svg>
      {running ? 'Läuft…' : 'Nochmals'}
    </button>
  );
}

function StopIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M5.25 7.5A2.25 2.25 0 0 1 7.5 5.25h9a2.25 2.25 0 0 1 2.25 2.25v9a2.25 2.25 0 0 1-2.25 2.25h-9a2.25 2.25 0 0 1-2.25-2.25v-9Z" />
    </svg>
  );
}

function ChatIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
    </svg>
  );
}


