import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { CrmBadge } from '../components/CrmBadge';
import { DraftEditor } from '../components/DraftEditor';
import { FormattedOutput } from '../components/FormattedOutput';
import { TaskDetailDialog } from '../components/TaskDetailDialog';
import { EmailThreadPanel } from '../components/EmailThreadPanel';
import { MeetingSourcePanel } from '../components/MeetingSourcePanel';
import { EmailBody } from '../components/EmailBody';
import { TracePanel } from '../components/TracePanel';
import { AgentRationale } from '../components/agent/AgentRationale';
import { ConfidenceBadge } from '../components/agent/ConfidenceBadge';
import { BriefingCard } from '../components/BriefingCard';
import { useSSE } from '../hooks/useSSE';
import { parseExcludeVendors, isExcludedVendor } from './creditors/creditors-helpers';
import type { AgentJob, TaskCard, PipelineData } from '../types';

interface SignaSignal {
  id: number;
  title: string;
  source_name: string;
  url: string | null;
  type: string | null;
  description: string | null;
  ai_reason: string | null;
  full_content: string | null;
  has_full_content: boolean;
  thumbnail_url: string | null;
  published_at: string | null;
  total_score: number;
  topic_name: string | null;
}

interface DraftPreview {
  draft_id: string;
  subject: string | null;
  body_html: string | null;
  body_preview: string | null;
  to_recipients: string[];
  cc_recipients: string[];
  source_subject: string | null;
  source_from: string | null;
  conversation_id: string | null;
}

interface PendingReviewTask {
  id: string;
  title: string;
  project_id: string;
  project_name: string;
  source_email_subject: string | null;
  source_email_from: string | null;
  email_conversation_id: string | null;
  meeting_transcript_id: string | null;
  meeting_subject: string | null;
  source: string | null; // 'followup' | 'meeting' | null
  needs_review: boolean;
}


interface TriageStats {
  by_status: Record<string, number>;
  by_class: Record<string, number>;
  total_pending: number;
}

interface CalendarEvent {
  id: string;
  subject: string | null;
  start: string | null;
  end: string | null;
  is_all_day: boolean;
  location: string | null;
  show_as: string | null;
  attendees_count: number;
  is_organizer: boolean;
  categories: string[];
}

interface FlaggedEmail {
  id: string;
  subject: string | null;
  from_address: string | null;
  from_name: string | null;
  received_at: string | null;
  body_preview: string | null;
  importance: string | null;
  has_attachments: boolean;
}

interface CockpitSettings {
  cockpit_background_url: string | null;
  cockpit_calendar_exclude_categories: string | null;
  cockpit_calendar_hide_private: boolean | null;
  creditors_overview_exclude_vendors: string | null;
}

interface PipedriveActivitySummary {
  id: number;
  subject: string;
  type: string | null;
  done: boolean | null;
  due_date: string | null;
  deal_id: number | null;
  person_name: string | null;
}

export const JOB_TYPE_LABELS: Record<string, string> = {
  email_triage: 'E-Mail-Triage',
  draft_email_reply: 'Antwort-Entwurf',
  create_task_from_email: 'Aufgabe erstellen',
  auto_reply: 'Auto-Antwort',
};

function formatTime(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit' });
}

function relativeDate(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'Gerade eben';
  if (diffMin < 60) return `Vor ${diffMin} Min.`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `Vor ${diffH} Std.`;
  const diffD = Math.floor(diffH / 24);
  return `Vor ${diffD} Tag${diffD > 1 ? 'en' : ''}`;
}

export function CockpitPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [creditorsExcludeVendors, setCreditorsExcludeVendors] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const [approvalJobs, setApprovalJobs] = useState<AgentJob[]>([]);
  const [focusTasks, setFocusTasks] = useState<TaskCard[]>([]);
  const [weekTasks, setWeekTasks] = useState<TaskCard[]>([]);
  const [overdueTasks, setOverdueTasks] = useState<TaskCard[]>([]);
  const [pendingReview, setPendingReview] = useState<PendingReviewTask[]>([]);
  const [_triageStats, setTriageStats] = useState<TriageStats | null>(null);
  const [calendarEvents, setCalendarEvents] = useState<CalendarEvent[]>([]);
  const [flaggedEmails, setFlaggedEmails] = useState<FlaggedEmail[]>([]);

  const [pipedriveActivities, setPipedriveActivities] = useState<PipedriveActivitySummary[]>([]);

  const [previews, setPreviews] = useState<Record<string, DraftPreview>>({});
  const [loadingPreviews, setLoadingPreviews] = useState<Set<string>>(new Set());
  const [failedPreviews, setFailedPreviews] = useState<Set<string>>(new Set());
  const [processing, setProcessing] = useState<Set<string>>(new Set());
  const [editingJobId, setEditingJobId] = useState<string | null>(null);
  const [expandedApprovals, setExpandedApprovals] = useState<Set<string>>(new Set());

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [reviewTaskId, setReviewTaskId] = useState<string | null>(null);
  const [senderAvatars, setSenderAvatars] = useState<Record<string, { pic_url: string | null; person_id: number | null; name: string | null }>>({});
  const lookedUpEmails = useRef<Set<string>>(new Set());

  const [signaSignals, setSignaSignals] = useState<SignaSignal[]>([]);
  const [signaHasMore, setSignaHasMore] = useState(true);
  const [signaLoading, setSignaLoading] = useState(false);
  const signaOffsetRef = useRef(0);
  const signaObserverRef = useRef<HTMLDivElement | null>(null);
  const [signaModalSignal, setSignaModalSignal] = useState<SignaSignal | null>(null);
  const [signaModalLoading, setSignaModalLoading] = useState(false);
  const [weekCapacity, setWeekCapacity] = useState<{ total_hours: number; booked_hours: number; meeting_hours: number; blocker_hours: number; free_hours: number; work_days: number } | null>(null);
  const [monthCapacity, setMonthCapacity] = useState<{ total_hours: number; booked_hours: number; meeting_hours: number; blocker_hours: number; free_hours: number; work_days: number } | null>(null);
  const [aiStats, setAiStats] = useState<{ pending_decisions: number; completed_week: number; completed_month: number; breakdown_week: { triage: number; drafts: number; suggestions: number; other: number } } | null>(null);
  // KPI-Leiste: eingeklappt eine schmale Zeile, aufgeklappt die drei Kacheln (Wahl bleibt erhalten)
  const [kpiExpanded, setKpiExpanded] = useState<boolean>(() => localStorage.getItem('cockpit_kpi_expanded') === '1');
  const toggleKpi = () => {
    setKpiExpanded(v => {
      localStorage.setItem('cockpit_kpi_expanded', v ? '0' : '1');
      return !v;
    });
  };
  const loadSignaSignals = useCallback(async (reset = false) => {
    if (signaLoading) return;
    setSignaLoading(true);
    const offset = reset ? 0 : signaOffsetRef.current;
    try {
      const data = await api.get<{ signals: SignaSignal[]; total: number }>(
        `/api/signa/signals?min_score=8.0&since=2weeks&status=relevant&limit=10&offset=${offset}`
      );
      if (reset) {
        setSignaSignals(data.signals);
      } else {
        setSignaSignals(prev => [...prev, ...data.signals]);
      }
      signaOffsetRef.current = offset + data.signals.length;
      setSignaHasMore(signaOffsetRef.current < data.total);
    } catch { /* ignore */ }
    setSignaLoading(false);
  }, [signaLoading]);

  const fetchPipedriveData = useCallback(async () => {
    try {
      const acts = await api.get<PipedriveActivitySummary[]>('/api/pipedrive/activities?done=false&limit=8');
      setPipedriveActivities(acts);
    } catch { /* Pipedrive optional */ }
  }, []);

  const fetchAppData = useCallback(async () => {
    try {
      const now = new Date();
      const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
      const endOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59).toISOString();

      const settingsData = await api.get<CockpitSettings>('/api/settings');
      setBgUrl(settingsData.cockpit_background_url);
      setCreditorsExcludeVendors(settingsData.creditors_overview_exclude_vendors ?? null);

      const excludeCats = settingsData.cockpit_calendar_exclude_categories ?? 'Transfer, Privat';
      const hidePrivate = settingsData.cockpit_calendar_hide_private ?? true;

      const [jobsData, reviewData, triageData, pipelineData, calData, flaggedData] = await Promise.all([
        api.get<AgentJob[]>('/api/agent-jobs'),
        api.get<PendingReviewTask[]>('/api/tasks/pending-review').catch(() => [] as PendingReviewTask[]),
        api.get<TriageStats>('/api/triage/stats').catch(() => null),
        api.get<PipelineData>('/api/pipeline').catch(() => null),
        api.get<CalendarEvent[]>(`/api/calendar/events?start=${encodeURIComponent(startOfDay)}&end=${encodeURIComponent(endOfDay)}&exclude_categories=${encodeURIComponent(excludeCats)}&hide_private=${hidePrivate}&hide_free=true`).catch(() => [] as CalendarEvent[]),
        api.get<FlaggedEmail[]>('/api/emails/flagged?top=10').catch(() => [] as FlaggedEmail[]),
      ]);

      const awaitingApproval = jobsData.filter(j => j.status === 'awaiting_approval');
      // Responsive Salience: unsichere Entwürfe zuerst (niedrigste Confidence oben),
      // damit der Mensch die riskanten Fälle zuerst prüft.
      const confOf = (j: AgentJob) => {
        const c = (j.metadata_json as Record<string, unknown> | null)?.confidence;
        const n = typeof c === 'number' ? c : (c != null ? Number(c) : NaN);
        return Number.isNaN(n) ? 1.1 : n; // ohne Angabe ans Ende
      };
      awaitingApproval.sort((a, b) => confOf(a) - confOf(b));
      setApprovalJobs(awaitingApproval);
      setPendingReview(reviewData);
      setTriageStats(triageData);
      setCalendarEvents(calData.filter(ev => {
        if (ev.is_all_day) return true;
        if (!ev.end) return true;
        const endTime = new Date(ev.end);
        return endTime > now;
      }));
      setFlaggedEmails(flaggedData);

      if (pipelineData?.columns) {
        const sorted = [...pipelineData.columns].sort((a, b) => a.position - b.position);
        const fokus = sorted[0]?.tasks ?? [];
        setFocusTasks(fokus);
        const fokusIds = new Set(fokus.map(t => t.id));
        const week = (sorted[1]?.tasks ?? []).filter(t => !fokusIds.has(t.id));
        setWeekTasks(week);

        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const allTasks = pipelineData.columns.flatMap(c => c.tasks);
        const overdue = allTasks.filter(t => t.due_date && !t.is_completed && new Date(t.due_date) < today);
        overdue.sort((a, b) => new Date(a.due_date!).getTime() - new Date(b.due_date!).getTime());
        setOverdueTasks(overdue);
      }
    } catch { /* */ }
    finally { setLoading(false); }

    loadSignaSignals(true);

    // Kapazität und AI-Stats vom Backend laden
    api.get<{ week: { total_hours: number; booked_hours: number; meeting_hours: number; blocker_hours: number; free_hours: number; work_days: number }; month: { total_hours: number; booked_hours: number; meeting_hours: number; blocker_hours: number; free_hours: number; work_days: number } }>('/api/calendar/capacity')
      .then(data => {
        setWeekCapacity(data.week);
        setMonthCapacity(data.month);
      })
      .catch(() => {});

    api.get<{ pending_decisions: number; completed_week: number; completed_month: number; breakdown_week: { triage: number; drafts: number; suggestions: number; other: number } }>('/api/agent-jobs/stats')
      .then(data => setAiStats(data))
      .catch(() => {});
  }, []);

  useEffect(() => { fetchAppData(); fetchPipedriveData(); }, [fetchAppData, fetchPipedriveData]);

  useEffect(() => {
    const el = signaObserverRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      entries => { if (entries[0]?.isIntersecting && signaHasMore && !signaLoading) loadSignaSignals(); },
      { threshold: 0.1 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [signaHasMore, signaLoading, loadSignaSignals]);

  const openSignalModal = useCallback(async (signal: SignaSignal) => {
    if (signal.has_full_content && !signal.full_content) {
      setSignaModalLoading(true);
      setSignaModalSignal(signal);
      try {
        const detail = await api.get<SignaSignal>(`/api/signa/signals/${signal.id}`);
        setSignaModalSignal(detail);
      } catch {
        setSignaModalSignal(signal);
      } finally {
        setSignaModalLoading(false);
      }
    } else {
      setSignaModalSignal(signal);
    }
  }, []);

  useSSE((event) => {
    if (['agent_jobs_changed', 'tasks_changed', 'email_triage_changed'].includes(event)) {
      fetchAppData();
    }
  });

  useEffect(() => {
    for (const job of approvalJobs) {
      if (!previews[job.id] && !loadingPreviews.has(job.id) && !failedPreviews.has(job.id)) {
        setLoadingPreviews(prev => new Set(prev).add(job.id));
        api.get<DraftPreview>(`/api/agent-jobs/${job.id}/draft-preview`)
          .then(preview => {
            setPreviews(prev => ({ ...prev, [job.id]: preview }));
          })
          .catch(() => {
            setFailedPreviews(prev => new Set(prev).add(job.id));
          })
          .finally(() => {
            setLoadingPreviews(prev => {
              const next = new Set(prev);
              next.delete(job.id);
              return next;
            });
          });
      }
    }
  }, [approvalJobs, previews, loadingPreviews, failedPreviews]);

  useEffect(() => {
    const unknownEmails: string[] = [];
    for (const job of approvalJobs) {
      const meta = (job.metadata_json || {}) as Record<string, string>;
      const email = meta.from_address;
      if (email && !senderAvatars[email] && !lookedUpEmails.current.has(email)) unknownEmails.push(email);
    }
    if (unknownEmails.length === 0) return;

    for (const email of unknownEmails) lookedUpEmails.current.add(email);

    const placeholder: Record<string, { pic_url: string | null; person_id: number | null; name: string | null }> = {};
    for (const email of unknownEmails) placeholder[email] = { pic_url: null, person_id: null, name: null };
    setSenderAvatars(prev => ({ ...prev, ...placeholder }));

    api.post<{ email: string; person: { id: number; name: string; pic_url: string | null } | null }[]>(
      '/api/pipedrive/lookup-emails',
      { emails: unknownEmails }
    )
      .then(results => {
        const updates: Record<string, { pic_url: string | null; person_id: number | null; name: string | null }> = {};
        for (const r of results) {
          if (r.person && r.person.id) {
            updates[r.email] = { pic_url: r.person.pic_url, person_id: r.person.id, name: r.person.name };
          }
        }
        if (Object.keys(updates).length > 0) {
          setSenderAvatars(prev => ({ ...prev, ...updates }));
        }
      })
      .catch(() => {});
  }, [approvalJobs, senderAvatars]);

  useEffect(() => {
    if (!settingsOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setSettingsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [settingsOpen]);

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { cockpit_background_url: url });
    setBgUrl(url);
  };

  const handleApprove = async (jobId: string) => {
    setProcessing(prev => new Set(prev).add(jobId));
    try {
      await api.patch(`/api/agent-jobs/${jobId}`, { status: 'completed' });
      fetchAppData();
    } catch { /* */ }
    finally { setProcessing(prev => { const n = new Set(prev); n.delete(jobId); return n; }); }
  };

  const handleReject = async (jobId: string) => {
    setProcessing(prev => new Set(prev).add(jobId));
    try {
      await api.patch(`/api/agent-jobs/${jobId}`, { status: 'failed', error_message: 'Vom Benutzer abgelehnt' });
      fetchAppData();
    } catch { /* */ }
    finally { setProcessing(prev => { const n = new Set(prev); n.delete(jobId); return n; }); }
  };

  const handleDismissTask = async (taskId: string) => {
    try {
      await api.post(`/api/tasks/${taskId}/dismiss-review`);
      fetchAppData();
    } catch { /* */ }
  };

  const openReviewDialog = (taskId: string) => {
    setReviewTaskId(taskId);
    setSelectedTaskId(taskId);
  };

  const handleReviewConfirm = async () => {
    if (!reviewTaskId) return;
    try {
      await api.post(`/api/tasks/${reviewTaskId}/confirm`, {});
    } catch { /* */ }
    setSelectedTaskId(null);
    setReviewTaskId(null);
    fetchAppData();
  };

  const handleReviewDismiss = async () => {
    if (!reviewTaskId) return;
    try {
      await api.post(`/api/tasks/${reviewTaskId}/dismiss-review`);
    } catch { /* */ }
    setSelectedTaskId(null);
    setReviewTaskId(null);
    fetchAppData();
  };

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  const hasBg = !!bgUrl;
  const isGradient = bgUrl?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient
    ? { background: bgUrl!.slice('gradient:'.length) }
    : hasBg
      ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' }
      : undefined;

  const cardClass = hasBg
    ? 'bg-white/10 backdrop-blur-md border-white/20 text-white'
    : 'bg-white border-gray-200 dark:bg-gray-900 dark:border-gray-800';

  const textPrimary = hasBg ? 'text-white' : 'text-gray-900 dark:text-white';
  const textSecondary = hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400';
  const textMuted = hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500';

  const pendingDecisions = approvalJobs.length + pendingReview.length;

  return (
    <div className="relative flex h-full flex-col" style={bgStyle}>
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/30" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/5 dark:bg-black/20" />}

      {/* Header */}
      <div className={`relative z-20 border-b px-4 py-4 sm:px-6 ${hasBg ? 'border-white/10 bg-black/20 backdrop-blur-sm' : 'border-gray-200 dark:border-gray-800'}`}>
        <div className="flex items-center justify-between">
          <div>
            <h1 className={`hidden text-xl font-bold lg:block ${textPrimary}`}>Cockpit</h1>
            <p className={`text-sm lg:mt-0.5 ${textSecondary}`}>
              Dein Überblick — Entscheidungen, Agenten, Termine und Fokus
            </p>
          </div>
          <div className="relative" ref={settingsRef}>
            <button
              onClick={() => setSettingsOpen(!settingsOpen)}
              className={`rounded-lg p-2 transition-colors ${
                hasBg
                  ? 'text-white/70 hover:bg-white/10 hover:text-white'
                  : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'
              }`}
              title="Cockpit-Einstellungen"
            >
              <SettingsGearIcon className="h-5 w-5" />
            </button>
            {settingsOpen && (
              <div className="absolute right-0 top-10 z-50 w-56 rounded-xl border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-900">
                <button
                  onClick={() => { setBgPickerOpen(true); setSettingsOpen(false); }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  <ImageIcon className="h-4 w-4" />
                  Hintergrundbild ändern
                </button>
                <button
                  onClick={() => { navigate('/settings?tab=cockpit'); setSettingsOpen(false); }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                >
                  <FilterIcon className="h-4 w-4" />
                  Kalender-Filter anpassen
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Inhalt */}
      <div className="relative z-10 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
        <div className="mx-auto max-w-6xl space-y-4 lg:space-y-6">

          {/* ── Zone 1: Briefing (zuoberst) ── */}
          <BriefingCard
            cardClass={cardClass}
            textPrimary={textPrimary}
            textSecondary={textSecondary}
            textMuted={textMuted}
            hasBg={hasBg}
          />

          {/* ── Zone 2: Agenda (dominant) | Kalender heute ── */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">

            {/* Agenda: grösste Fläche (2/3) */}
            <section className={`rounded-xl border p-4 lg:col-span-2 ${cardClass}`}>
              <div className="mb-3 flex items-center justify-between">
                <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
                  Agenda
                </h2>
                <button
                  onClick={() => navigate('/pipeline')}
                  className={`text-xs font-medium ${hasBg ? 'text-white/60 hover:text-white' : 'text-indigo-600 hover:text-indigo-800 dark:text-indigo-400'}`}
                >
                  Agenda öffnen →
                </button>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  {/* Fokus */}
                  <div className="mb-3">
                    <h3 className={`mb-1.5 text-xs font-semibold uppercase tracking-wide ${hasBg ? 'text-amber-300' : 'text-amber-600 dark:text-amber-400'}`}>
                      Fokus ({focusTasks.length})
                    </h3>
                    {focusTasks.length === 0 ? (
                      <p className={`text-xs ${textMuted}`}>Keine Fokus-Aufgaben</p>
                    ) : (
                      <div className="space-y-1">
                        {focusTasks.slice(0, 8).map(task => (
                          <div
                            key={task.id}
                            className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 cursor-pointer ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                            onClick={() => setSelectedTaskId(task.id)}
                          >
                            <div className="h-1.5 w-1.5 rounded-full shrink-0 bg-amber-400" />
                            <span className={`text-sm truncate ${textPrimary}`}>{task.title}</span>
                          </div>
                        ))}
                        {focusTasks.length > 8 && (
                          <p className={`text-xs pl-5 ${textMuted}`}>+{focusTasks.length - 8} weitere</p>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Überfällig */}
                  {overdueTasks.length > 0 && (
                    <div>
                      <h3 className={`mb-1.5 text-xs font-semibold uppercase tracking-wide ${hasBg ? 'text-red-300' : 'text-red-600 dark:text-red-400'}`}>
                        Überfällig ({overdueTasks.length})
                      </h3>
                      <div className="space-y-1">
                        {overdueTasks.slice(0, 6).map(task => (
                          <div
                            key={task.id}
                            className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 cursor-pointer ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                            onClick={() => setSelectedTaskId(task.id)}
                          >
                            <div className="h-1.5 w-1.5 rounded-full shrink-0 bg-red-500" />
                            <div className="min-w-0 flex-1 flex items-center gap-2">
                              <span className={`text-sm truncate ${textPrimary}`}>{task.title}</span>
                              <span className={`shrink-0 text-[10px] font-medium text-red-500`}>
                                {new Date(task.due_date!).toLocaleDateString('de-CH', { day: 'numeric', month: 'short' })}
                              </span>
                            </div>
                          </div>
                        ))}
                        {overdueTasks.length > 6 && (
                          <p className={`text-xs pl-5 ${textMuted}`}>+{overdueTasks.length - 6} weitere</p>
                        )}
                      </div>
                    </div>
                  )}
                </div>

                {/* Diese Woche */}
                <div>
                  <h3 className={`mb-1.5 text-xs font-semibold uppercase tracking-wide ${hasBg ? 'text-blue-300' : 'text-blue-600 dark:text-blue-400'}`}>
                    Diese Woche ({weekTasks.length})
                  </h3>
                  {weekTasks.length === 0 ? (
                    <p className={`text-xs ${textMuted}`}>Keine Aufgaben diese Woche</p>
                  ) : (
                    <div className="space-y-1">
                      {weekTasks.slice(0, 10).map(task => (
                        <div
                          key={task.id}
                          className={`flex items-center gap-2 rounded-lg px-2.5 py-1.5 cursor-pointer ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                          onClick={() => setSelectedTaskId(task.id)}
                        >
                          <div className="h-1.5 w-1.5 rounded-full shrink-0 bg-blue-400" />
                          <span className={`text-sm truncate ${textPrimary}`}>{task.title}</span>
                        </div>
                      ))}
                      {weekTasks.length > 10 && (
                        <p className={`text-xs pl-5 ${textMuted}`}>+{weekTasks.length - 10} weitere</p>
                      )}
                    </div>
                  )}
                </div>
              </div>
            </section>

            {/* Rechte Spalte: Heute + E-Mails & CRM gestapelt */}
            <div className="space-y-4">

            {/* Kalender heute */}
            <section className={`rounded-xl border p-4 ${cardClass}`}>
              <div className="mb-3 flex items-center justify-between">
                <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
                  Heute
                </h2>
                <span className={`text-xs ${textMuted}`}>
                  {new Date().toLocaleDateString('de-CH', { weekday: 'long', day: 'numeric', month: 'long' })}
                </span>
              </div>
              {calendarEvents.length === 0 ? (
                <div className={`flex h-20 items-center justify-center rounded-lg text-sm ${textMuted}`}>
                  Keine Termine heute
                </div>
              ) : (
                <div className="space-y-1.5">
                  {calendarEvents.map(ev => {
                    const isMeeting = ev.attendees_count > 1;
                    return (
                      <div
                        key={ev.id}
                        className={`flex items-start gap-2.5 rounded-lg p-2.5 ${
                          hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                        }`}
                      >
                        <div className="mt-0.5 shrink-0">
                          {ev.is_all_day ? (
                            <div className={`h-5 w-5 rounded text-center text-[10px] font-bold leading-5 ${
                              hasBg ? 'bg-white/20 text-white' : 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300'
                            }`}>∞</div>
                          ) : isMeeting ? (
                            <UsersIcon className={`h-4 w-4 ${hasBg ? 'text-white/60' : 'text-blue-500 dark:text-blue-400'}`} />
                          ) : (
                            <ClockIcon className={`h-4 w-4 ${hasBg ? 'text-white/40' : 'text-gray-400 dark:text-gray-500'}`} />
                          )}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className={`text-sm font-medium truncate ${textPrimary}`}>{ev.subject || 'Ohne Titel'}</div>
                          <div className={`text-[11px] ${textMuted}`}>
                            {ev.is_all_day ? 'Ganztägig' : `${formatTime(ev.start)} – ${formatTime(ev.end)}`}
                            {ev.location && ` · ${ev.location}`}
                          </div>
                        </div>
                        {isMeeting && (
                          <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                            hasBg ? 'bg-blue-500/30 text-blue-200' : 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
                          }`}>
                            {ev.attendees_count}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </section>

            {/* E-Mails & CRM (zurück in der Agenda/Heute-Zeile) */}
            <section className={`rounded-xl border p-4 ${cardClass}`}>
              <div className="mb-3 flex items-center justify-between">
                <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
                  E-Mails & CRM
                </h2>
                <div className={`flex items-center gap-1.5 text-xs font-medium`}>
                  <button
                    onClick={() => navigate('/inbox')}
                    className={`${hasBg ? 'text-white/60 hover:text-white' : 'text-indigo-600 hover:text-indigo-800 dark:text-indigo-400'}`}
                  >
                    Posteingang
                  </button>
                  <span className={`${hasBg ? 'text-white/30' : 'text-gray-300 dark:text-gray-600'}`}>·</span>
                  <a
                    href="https://innosmith.pipedrive.com"
                    target="_blank"
                    rel="noopener noreferrer"
                    className={`${hasBg ? 'text-white/60 hover:text-white' : 'text-green-600 hover:text-green-800 dark:text-green-400'}`}
                  >
                    Pipedrive
                  </a>
                </div>
              </div>
              <div className="space-y-1.5 max-h-80 overflow-y-auto">
                {flaggedEmails.map(email => (
                  <div
                    key={email.id}
                    className={`flex items-start gap-2.5 rounded-lg p-2.5 cursor-pointer transition-colors ${
                      hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                    }`}
                    onClick={() => navigate('/inbox')}
                  >
                    <FlagIcon className={`mt-0.5 h-4 w-4 shrink-0 ${hasBg ? 'text-orange-300' : 'text-orange-500 dark:text-orange-400'}`} />
                    <div className="min-w-0 flex-1">
                      <div className={`text-sm font-medium truncate ${textPrimary}`}>{email.subject || 'Kein Betreff'}</div>
                      <div className={`text-[11px] ${textMuted}`}>
                        {email.from_name || email.from_address}
                        {email.received_at && ` · ${relativeDate(email.received_at)}`}
                      </div>
                    </div>
                    {email.has_attachments && (
                      <PaperclipIcon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${textMuted}`} />
                    )}
                  </div>
                ))}
                {pipedriveActivities.length > 0 && flaggedEmails.length > 0 && (
                  <div className={`my-2 border-t ${hasBg ? 'border-white/10' : 'border-gray-100 dark:border-gray-800'}`} />
                )}
                {pipedriveActivities.map(act => {
                  const isOverdue = act.due_date && new Date(act.due_date) < new Date();
                  return (
                    <a
                      key={`pd-${act.id}`}
                      href={`https://innosmith.pipedrive.com/activities/${act.id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={`flex items-start gap-2.5 rounded-lg p-2.5 transition-colors ${
                        hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                      }`}
                    >
                      <div className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${isOverdue ? 'bg-red-500' : 'bg-orange-400'}`} />
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
                {flaggedEmails.length === 0 && pipedriveActivities.length === 0 && (
                  <div className={`flex h-20 items-center justify-center rounded-lg text-sm ${textMuted}`}>
                    Keine E-Mails oder Aufgaben
                  </div>
                )}
              </div>
            </section>
            </div>
          </div>

          {/* ── Zone 3: Freigaben (kompakt, aufklappbar) ── */}
          {approvalJobs.length > 0 && (
            <section>
              <h2 className={`mb-3 text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
                Freigaben
                <span className={`ml-2 inline-flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-bold ${
                  hasBg ? 'bg-amber-500/30 text-amber-200' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                }`}>{approvalJobs.length}</span>
              </h2>
              <div className="space-y-2">
                {approvalJobs.map(job => {
                  const meta = (job.metadata_json || {}) as Record<string, string>;
                  const preview = previews[job.id];
                  const isProcessing = processing.has(job.id);
                  const hasFailed = failedPreviews.has(job.id);
                  const isExpanded = expandedApprovals.has(job.id) || editingJobId === job.id;

                  const toggleExpand = () => {
                    setExpandedApprovals(prev => {
                      const next = new Set(prev);
                      if (next.has(job.id)) next.delete(job.id); else next.add(job.id);
                      return next;
                    });
                  };

                  // Responsive Salience: unsichere Entwürfe ziehen den Blick stärker
                  // auf sich (rot/amber), sichere bleiben dezent — der Mensch prüft
                  // gezielt dort genauer, wo das Risiko höher ist.
                  const conf = meta.confidence != null ? Number(meta.confidence) : null;
                  const salience = conf == null
                    ? ''
                    : conf < 0.5
                      ? 'border-l-[3px] border-l-red-500'
                      : conf < 0.8
                        ? 'border-l-[3px] border-l-amber-400'
                        : 'border-l-[3px] border-l-emerald-400';

                  return (
                    <div key={job.id} className={`rounded-xl border ${cardClass} ${salience} ${isExpanded ? 'p-5' : 'p-3'}`}>
                      {/* Kompakte Kopfzeile */}
                      <div
                        className={`flex items-center gap-3 ${!isExpanded ? 'cursor-pointer' : ''}`}
                        onClick={!isExpanded ? toggleExpand : undefined}
                      >
                        {(() => {
                          const avatar = meta.from_address ? senderAvatars[meta.from_address] : null;
                          if (avatar?.pic_url) {
                            return avatar.person_id ? (
                              <a
                                href={`https://innosmith.pipedrive.com/person/${avatar.person_id}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                onClick={(e) => e.stopPropagation()}
                                className="shrink-0"
                                title={avatar.name || meta.from_name || ''}
                              >
                                <img src={avatar.pic_url} alt="" className="h-9 w-9 rounded-full object-cover ring-2 ring-green-400/50" />
                              </a>
                            ) : (
                              <img src={avatar.pic_url} alt="" className="h-9 w-9 shrink-0 rounded-full object-cover" />
                            );
                          }
                          return (
                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-amber-100/80 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                              <MailDraftIcon className="h-4.5 w-4.5" />
                            </div>
                          );
                        })()}
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className={`text-sm font-semibold truncate ${textPrimary}`}>
                              {preview?.subject || meta.subject || 'E-Mail-Entwurf'}
                            </span>
                            {meta.confidence != null && (
                              <ConfidenceBadge confidence={Number(meta.confidence)} glassBg={hasBg} className="shrink-0" />
                            )}
                          </div>
                          <div className={`text-xs ${textMuted}`}>
                            An: {preview?.to_recipients?.join(', ') || meta.from_address || 'Unbekannt'}
                            {meta.from_name && (() => {
                              const avatar = meta.from_address ? senderAvatars[meta.from_address] : null;
                              if (avatar?.person_id) {
                                return (
                                  <>
                                    {' — von '}
                                    <a
                                      href={`https://innosmith.pipedrive.com/person/${avatar.person_id}`}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      onClick={(e) => e.stopPropagation()}
                                      className="font-medium text-green-600 hover:underline dark:text-green-400"
                                    >
                                      {avatar.name || meta.from_name}
                                    </a>
                                  </>
                                );
                              }
                              return ` — von ${meta.from_name}`;
                            })()}
                          </div>
                        </div>
                        {!isExpanded && (
                          <div className="flex shrink-0 items-center gap-1.5">
                            {/* Kein Blind-Senden: erst pruefen (aufklappen), dann freigeben */}
                            <button
                              onClick={(e) => { e.stopPropagation(); toggleExpand(); }}
                              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                                hasBg
                                  ? 'bg-emerald-600/90 text-white hover:bg-emerald-600'
                                  : 'bg-emerald-600 text-white hover:bg-emerald-700'
                              }`}
                            >
                              Prüfen
                            </button>
                            <button
                              onClick={(e) => { e.stopPropagation(); handleReject(job.id); }}
                              disabled={isProcessing}
                              className={`rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
                                hasBg
                                  ? 'text-red-300 hover:bg-red-500/20'
                                  : 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20'
                              }`}
                            >
                              ✕
                            </button>
                            <ChevronDownIcon className={`h-4 w-4 ${textMuted}`} />
                          </div>
                        )}
                        {isExpanded && (
                          <button onClick={toggleExpand} className={`shrink-0 p-1 rounded ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-100 dark:hover:bg-gray-800'}`}>
                            <ChevronUpIcon className={`h-4 w-4 ${textMuted}`} />
                          </button>
                        )}
                      </div>

                      {/* Aufgeklappter Inhalt */}
                      {isExpanded && (
                        <div className="mt-4">
                          {/* Lesbares Warum + Sicherheit */}
                          {(meta.rationale || meta.summary || meta.confidence != null) && (
                            <div className="mb-3 flex items-start justify-between gap-2">
                              <AgentRationale
                                summary={(meta.rationale as string) || (meta.summary as string) || null}
                                jobId={job.id}
                                glassBg={hasBg}
                              />
                              {meta.confidence != null && (
                                <ConfidenceBadge confidence={Number(meta.confidence)} glassBg={hasBg} className="mt-0.5 shrink-0" />
                              )}
                            </div>
                          )}
                          {meta.subject && (
                            <div className={`mb-3 rounded-lg px-3 py-2 text-xs ${hasBg ? 'bg-white/10' : 'bg-gray-50 dark:bg-gray-800/50'} ${textMuted}`}>
                              <span className="font-medium">Antwort auf:</span> {meta.subject}
                              {(meta.from_address || meta.from_name) && (
                                <span> — von {meta.from_name || meta.from_address}</span>
                              )}
                            </div>
                          )}

                          {meta.from_address && (
                            <div className="mb-3 sm:hidden">
                              <CrmBadge emailAddress={meta.from_address} senderName={meta.from_name} glassBg={hasBg} onCreateContact={() => {}} />
                            </div>
                          )}

                          {!preview && !hasFailed && (
                            <div className={`mb-3 flex items-center gap-2 text-sm ${textMuted}`}>
                              <div className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-transparent" />
                              Entwurf wird geladen…
                            </div>
                          )}

                          {editingJobId === job.id && preview ? (
                            <DraftEditor
                              jobId={job.id}
                              subject={preview.subject || ''}
                              bodyHtml={preview.body_html || ''}
                              toRecipients={preview.to_recipients || []}
                              ccRecipients={preview.cc_recipients || []}
                              glassBg={hasBg}
                              onSaved={() => {
                                setEditingJobId(null);
                                setPreviews(prev => { const n = { ...prev }; delete n[job.id]; return n; });
                                setFailedPreviews(prev => { const n = new Set(prev); n.delete(job.id); return n; });
                              }}
                              onSentAfterEdit={() => {
                                setEditingJobId(null);
                                fetchAppData();
                              }}
                              onCancel={() => setEditingJobId(null)}
                            />
                          ) : (
                            <>
                              {preview?.body_html && (
                                <div className={`mb-3 rounded-lg border overflow-hidden ${hasBg ? 'border-white/20 bg-white/5' : 'border-gray-200 bg-white dark:border-gray-600 dark:bg-gray-800'}`}>
                                  <div className={`border-b px-4 py-2 ${hasBg ? 'border-white/10' : 'border-gray-100 dark:border-gray-700'}`}>
                                    <span className={`text-xs font-medium ${textMuted}`}>E-Mail-Entwurf</span>
                                  </div>
                                  <div className="max-h-56 overflow-y-auto px-4 py-3">
                                    <EmailBody html={preview.body_html} glassBg={hasBg} />
                                  </div>
                                </div>
                              )}

                              {!preview?.body_html && preview?.body_preview && (
                                <div className={`mb-3 rounded-lg border p-4 text-sm ${hasBg ? 'border-white/20 bg-white/5' : 'border-gray-200 bg-white dark:border-gray-600 dark:bg-gray-800'} ${textPrimary}`}>
                                  <div className={`mb-1 text-xs font-medium ${textMuted}`}>E-Mail-Entwurf</div>
                                  {preview.body_preview}
                                </div>
                              )}

                              {!preview && hasFailed && job.output && (
                                <div className={`mb-3 rounded-lg border p-3 ${hasBg ? 'border-white/20 bg-white/5' : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800'}`}>
                                  <div className={`mb-1 text-xs font-medium ${textMuted}`}>Agent-Output</div>
                                  <div className={`text-sm max-h-40 overflow-y-auto ${textSecondary}`}>
                                    <FormattedOutput output={job.output} />
                                  </div>
                                </div>
                              )}

                              <div className="flex gap-2">
                                <button
                                  onClick={() => handleApprove(job.id)}
                                  disabled={isProcessing}
                                  className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${
                                    hasBg
                                      ? 'bg-emerald-600/90 text-white hover:bg-emerald-600 shadow-sm'
                                      : 'bg-emerald-600 text-white hover:bg-emerald-700 shadow-sm'
                                  }`}
                                >
                                  {isProcessing ? 'Wird gesendet…' : 'Freigeben & Senden'}
                                </button>
                                {preview && (
                                  <button
                                    onClick={() => setEditingJobId(job.id)}
                                    className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                                      hasBg
                                        ? 'bg-white/10 text-white hover:bg-white/20 border border-white/20'
                                        : 'bg-indigo-50 text-indigo-700 hover:bg-indigo-100 dark:bg-indigo-900/20 dark:text-indigo-300 dark:hover:bg-indigo-900/40'
                                    }`}
                                  >
                                    <span className="flex items-center gap-1.5">
                                      <PencilIcon className="h-3.5 w-3.5" />
                                      Bearbeiten
                                    </span>
                                  </button>
                                )}
                                <button
                                  onClick={() => handleReject(job.id)}
                                  disabled={isProcessing}
                                  className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${
                                    hasBg
                                      ? 'bg-red-500/20 text-red-200 hover:bg-red-500/30 border border-red-400/30'
                                      : 'border border-red-300 text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-900/20'
                                  }`}
                                >
                                  Ablehnen
                                </button>
                              </div>
                            </>
                          )}

                          <TracePanel jobId={job.id} compact />

                          {(preview?.conversation_id || meta.conversation_id) && (
                            <EmailThreadPanel
                              conversationId={(preview?.conversation_id || meta.conversation_id) as string}
                              glassBg={hasBg}
                              compact
                            />
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          {/* Aufgaben-Vorschläge */}
          {pendingReview.length > 0 && (
            <section>
              <h2 className={`mb-3 text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
                Aufgaben-Vorschläge
              </h2>
              <div className="space-y-3">
                {pendingReview.map(task => (
                  <div key={task.id} className={`rounded-xl border p-4 ${cardClass}`}>
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-blue-100/80 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
                        <TaskIcon className="h-4 w-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <div className={`text-sm font-semibold truncate ${textPrimary}`}>{task.title}</div>
                          {task.source === 'followup' && (
                            <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                              hasBg ? 'bg-rose-500/30 text-rose-200' : 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300'
                            }`}>
                              Follow-up
                            </span>
                          )}
                          {task.source === 'meeting' && (
                            <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                              hasBg ? 'bg-violet-500/30 text-violet-200' : 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300'
                            }`}>
                              Meeting
                            </span>
                          )}
                        </div>
                        {task.source_email_subject && (
                          <div className={`text-xs truncate ${textMuted}`}>
                            Aus E-Mail: {task.source_email_subject}
                            {task.source_email_from && ` von ${task.source_email_from}`}
                          </div>
                        )}
                        {task.meeting_transcript_id && (
                          <div className={`text-xs truncate ${textMuted}`}>
                            Aus Meeting: {task.meeting_subject || 'Meeting'}
                          </div>
                        )}
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        <button
                          onClick={() => openReviewDialog(task.id)}
                          className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700"
                        >
                          Task planen
                        </button>
                        <button
                          onClick={() => handleDismissTask(task.id)}
                          className="rounded-lg px-3 py-1.5 text-xs font-medium text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20"
                        >
                          Verwerfen
                        </button>
                      </div>
                    </div>
                    {task.email_conversation_id && (
                      <EmailThreadPanel
                        conversationId={task.email_conversation_id}
                        glassBg={hasBg}
                        compact
                      />
                    )}
                    {task.meeting_transcript_id && (
                      <MeetingSourcePanel
                        transcriptId={task.meeting_transcript_id}
                        subject={task.meeting_subject}
                        glassBg={hasBg}
                      />
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* ── Zone 4: KPI-Leiste (aufklappbar, ohne Inhaltsverlust) ── */}
          <section className={`rounded-xl border ${cardClass} ${kpiExpanded ? 'p-4' : 'px-4 py-2.5'}`}>
            <button
              type="button"
              onClick={toggleKpi}
              className="flex w-full items-center justify-between gap-3 text-left"
            >
              <div className="flex min-w-0 items-center gap-3">
                <h2 className={`shrink-0 text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
                  Kennzahlen
                </h2>
                {!kpiExpanded && (
                  <span className={`truncate text-xs ${textMuted}`}>
                    {[
                      `${aiStats?.pending_decisions ?? 0} Freigaben offen`,
                      `${focusTasks.length} Fokus`,
                      overdueTasks.length > 0 ? `${overdueTasks.length} überfällig` : null,
                      weekCapacity ? `${weekCapacity.free_hours}h frei diese Woche` : null,
                    ].filter(Boolean).join(' · ')}
                  </span>
                )}
              </div>
              {kpiExpanded
                ? <ChevronUpIcon className={`h-4 w-4 shrink-0 ${textMuted}`} />
                : <ChevronDownIcon className={`h-4 w-4 shrink-0 ${textMuted}`} />}
            </button>
            {kpiExpanded && (
              <div className="mt-3 grid grid-cols-2 gap-4 sm:grid-cols-3">
                <AiFreigabenCard stats={aiStats} hasBg={hasBg} />
                <AgendaKpiCard fokus={focusTasks.length} overdue={overdueTasks.length} week={weekTasks.length} hasBg={hasBg} />
                <CapacityCard weekCapacity={weekCapacity} monthCapacity={monthCapacity} hasBg={hasBg} className="col-span-2 sm:col-span-1" />
              </div>
            )}
          </section>

          {/* ── Fällige Kreditoren-Zahlungen ── */}
          <UpcomingPaymentsCard cardClass={cardClass} textSecondary={textSecondary} textMuted={textMuted} excludeVendors={creditorsExcludeVendors} />

          {/* ── SIGNA-Signale ── */}
          {signaSignals.length > 0 && (
            <section className={`rounded-xl border p-4 ${cardClass}`}>
              <div className="mb-3 flex items-center justify-between">
                <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
                  Signale
                </h2>
                <button
                  onClick={() => navigate('/signale')}
                  className={`text-xs font-medium ${hasBg ? 'text-white/60 hover:text-white' : 'text-indigo-600 hover:text-indigo-800 dark:text-indigo-400'}`}
                >
                  Alle Signale →
                </button>
              </div>
              <div className="max-h-96 overflow-y-auto space-y-1.5">
                {signaSignals.map(signal => (
                  <div
                    key={signal.id}
                    className={`flex items-start gap-2.5 rounded-lg px-2.5 py-2 cursor-pointer transition-colors ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-50 dark:hover:bg-gray-800'}`}
                    onClick={() => openSignalModal(signal)}
                  >
                    <span className="mt-0.5 shrink-0 text-sm">
                      {signal.type === 'youtube' ? '🎬' : signal.type === 'rss' ? '📰' : '🌐'}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className={`text-sm font-medium truncate ${textPrimary}`}>{signal.title}</div>
                      <div className={`text-[11px] ${textMuted} flex items-center gap-2 mt-0.5`}>
                        <span>{signal.source_name}</span>
                        {signal.topic_name && <span>· {signal.topic_name}</span>}
                        {signal.published_at && <span>· {relativeDate(signal.published_at)}</span>}
                        <span className="ml-auto shrink-0 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">
                          {signal.total_score.toFixed(1)}
                        </span>
                      </div>
                    </div>
                  </div>
                ))}
                {signaHasMore && (
                  <div ref={signaObserverRef} className="flex justify-center py-2">
                    {signaLoading && <div className="h-4 w-4 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />}
                  </div>
                )}
              </div>
            </section>
          )}

          {/* Alles erledigt */}
          {pendingDecisions === 0 && focusTasks.length === 0 && flaggedEmails.length === 0 && calendarEvents.length === 0 && (
            <div className={`flex flex-col items-center justify-center rounded-xl border p-12 ${cardClass}`}>
              <CheckCircleIcon className={`h-12 w-12 mb-3 ${hasBg ? 'text-white/40' : 'text-emerald-300 dark:text-emerald-700'}`} />
              <p className={`text-lg font-medium ${textPrimary}`}>Alles erledigt</p>
              <p className={`mt-1 text-sm ${textSecondary}`}>
                Keine offenen Entscheidungen, Termine oder markierten E-Mails.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Task-Detail Modal */}
      {selectedTaskId && (
        <TaskDetailDialog
          taskId={selectedTaskId}
          onClose={() => { setSelectedTaskId(null); setReviewTaskId(null); }}
          onUpdated={fetchAppData}
          onOpenTask={setSelectedTaskId}
          reviewMode={selectedTaskId === reviewTaskId && reviewTaskId !== null}
          onReviewConfirm={handleReviewConfirm}
          onReviewDismiss={handleReviewDismiss}
        />
      )}

      {/* Background Picker */}
      <BackgroundPicker
        isOpen={bgPickerOpen}
        onClose={() => setBgPickerOpen(false)}
        currentUrl={bgUrl}
        onSelect={(url) => { handleBgSelect(url); setBgPickerOpen(false); }}
      />

      {/* SIGNA Signal Detail Modal */}
      {signaModalSignal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 modal-safe" onClick={() => setSignaModalSignal(null)}>
          <div className="fixed inset-0 bg-black/50 backdrop-blur-sm" />
          <div
            className="relative z-10 max-h-full w-full max-w-2xl lg:max-h-[80vh] overflow-y-auto rounded-2xl border bg-white p-6 shadow-xl dark:border-gray-700 dark:bg-gray-900"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={() => setSignaModalSignal(null)}
              className="absolute right-4 top-4 rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
            >
              <XMarkIcon className="h-5 w-5" />
            </button>
            <div className="mb-4">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-lg">
                  {signaModalSignal.type === 'youtube' ? '🎬' : signaModalSignal.type === 'rss' ? '📰' : '🌐'}
                </span>
                <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">
                  Score: {signaModalSignal.total_score.toFixed(1)}
                </span>
              </div>
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">{signaModalSignal.title}</h3>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                <span>{signaModalSignal.source_name}</span>
                {signaModalSignal.topic_name && <span>· {signaModalSignal.topic_name}</span>}
                {signaModalSignal.published_at && <span>· {new Date(signaModalSignal.published_at).toLocaleDateString('de-CH', { day: 'numeric', month: 'long', year: 'numeric' })}</span>}
              </div>
            </div>

            {signaModalLoading ? (
              <div className="flex h-24 items-center justify-center">
                <div className="h-6 w-6 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
              </div>
            ) : (
              <div className="space-y-4">
                {signaModalSignal.type === 'youtube' && signaModalSignal.url && (
                  <div className="aspect-video w-full overflow-hidden rounded-lg">
                    <iframe
                      className="h-full w-full"
                      src={`https://www.youtube.com/embed/${new URL(signaModalSignal.url).searchParams.get('v') || signaModalSignal.url.split('/').pop()}`}
                      allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                      allowFullScreen
                    />
                  </div>
                )}

                {signaModalSignal.ai_reason && (
                  <p className="text-sm text-gray-700 dark:text-gray-300">{signaModalSignal.ai_reason}</p>
                )}

                {signaModalSignal.full_content && signaModalSignal.type !== 'youtube' && (
                  <div className="max-h-80 overflow-y-auto rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-700 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-300">
                    {/<[a-z][\s\S]*>/i.test(signaModalSignal.full_content) ? (
                      <div className="prose prose-sm max-w-none dark:prose-invert" dangerouslySetInnerHTML={{ __html: signaModalSignal.full_content }} />
                    ) : (
                      <p className="whitespace-pre-wrap">{signaModalSignal.full_content}</p>
                    )}
                  </div>
                )}

                {signaModalSignal.url && signaModalSignal.type !== 'youtube' && (
                  <a
                    href={signaModalSignal.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 transition-colors"
                  >
                    Artikel lesen ↗
                  </a>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── AI-Freigaben Card (Split-Layout mit Breakdown) ── */

type AiStats = { pending_decisions: number; completed_week: number; completed_month: number; breakdown_week: { triage: number; drafts: number; suggestions: number; other: number } };

function AiFreigabenCard({ stats, hasBg }: { stats: AiStats | null; hasBg: boolean }) {
  const pending = stats?.pending_decisions ?? 0;
  const week = stats?.completed_week ?? 0;
  const month = stats?.completed_month ?? 0;
  const bd = stats?.breakdown_week;

  const pendingColor = pending > 0
    ? (hasBg ? 'text-amber-300' : 'text-amber-600 dark:text-amber-400')
    : (hasBg ? 'text-white' : 'text-gray-700 dark:text-gray-300');
  const labelClass = `text-[10px] ${hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500'}`;
  const valueClass = `text-xs font-medium ${hasBg ? 'text-white/90' : 'text-gray-700 dark:text-gray-300'}`;

  return (
    <div className={`rounded-xl border p-3 lg:p-4 ${
      hasBg ? 'bg-white/10 backdrop-blur-md border-white/20' : 'bg-white border-gray-200 dark:bg-gray-900 dark:border-gray-800'
    }`}>
      <div className={`mb-1.5 text-[10px] font-semibold uppercase tracking-wide ${hasBg ? 'text-white/60' : 'text-gray-500 dark:text-gray-400'}`}>
        AI-Aktivität
      </div>
      {/* Auf 393px ist die Karte ~170px breit: nebeneinander bleibt für «diese Woche»
          kein Platz, darum mobil untereinander. */}
      <div className="flex h-full flex-col gap-1.5 sm:flex-row sm:gap-3">
        {/* Links: Freigaben offen */}
        <div className={`flex flex-col justify-center max-sm:border-b max-sm:pb-1.5 sm:min-w-[60px] sm:border-r sm:pr-3 ${hasBg ? 'border-white/10' : 'border-gray-100 dark:border-gray-800'}`}>
          <div className={`text-2xl font-bold lg:text-3xl ${pendingColor}`}>{pending}</div>
          <div className={labelClass}>Freigaben{'\n'}offen</div>
        </div>
        {/* Rechts: Woche / Monat + Breakdown */}
        <div className="flex flex-col justify-center gap-1 min-w-0">
          <div className="flex items-baseline gap-1">
            <span className={valueClass}>{week}</span>
            <span className={labelClass}>diese Woche</span>
          </div>
          <div className="flex items-baseline gap-1">
            <span className={valueClass}>{month}</span>
            <span className={labelClass}>dieser Monat</span>
          </div>
          {bd && (bd.triage > 0 || bd.drafts > 0 || bd.suggestions > 0 || bd.other > 0) && (
            <div className={`text-[9px] mt-0.5 ${hasBg ? 'text-white/40' : 'text-gray-400 dark:text-gray-500'}`}>
              {[
                bd.triage > 0 && `${bd.triage} Triage`,
                bd.suggestions > 0 && `${bd.suggestions} Vorschläge`,
                bd.drafts > 0 && `${bd.drafts} Drafts`,
                bd.other > 0 && `${bd.other} Andere`,
              ].filter(Boolean).join(' · ')}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Agenda KPI Card ── */

function AgendaKpiCard({ fokus, overdue, week, hasBg }: { fokus: number; overdue: number; week: number; hasBg: boolean }) {
  const valClass = `text-xs font-medium ${hasBg ? 'text-white/90' : 'text-gray-700 dark:text-gray-300'}`;
  const labelClass = `text-[10px] ${hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500'}`;
  return (
    <div className={`rounded-xl border p-3 lg:p-4 ${
      hasBg ? 'bg-white/10 backdrop-blur-md border-white/20' : 'bg-white border-gray-200 dark:bg-gray-900 dark:border-gray-800'
    }`}>
      <div className={`mb-1.5 text-[10px] font-semibold uppercase tracking-wide ${hasBg ? 'text-white/60' : 'text-gray-500 dark:text-gray-400'}`}>
        Agenda
      </div>
      <div className="space-y-1">
        <div className="flex items-center gap-1.5">
          <div className="h-2 w-2 rounded-full bg-amber-500 shrink-0" />
          <span className={valClass}>{fokus}</span>
          <span className={labelClass}>Fokus</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="h-2 w-2 rounded-full bg-red-500 shrink-0" />
          <span className={valClass}>{overdue}</span>
          <span className={labelClass}>Überfällig</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="h-2 w-2 rounded-full bg-blue-500 shrink-0" />
          <span className={valClass}>{week}</span>
          <span className={labelClass}>Diese Woche</span>
        </div>
      </div>
    </div>
  );
}

/* ── Capacity Card (Woche + Monat) ── */

type CapData = { total_hours: number; booked_hours: number; meeting_hours: number; blocker_hours: number; free_hours: number; work_days: number };

function CapacityCard({ weekCapacity, monthCapacity, hasBg, className = '' }: { weekCapacity: CapData | null; monthCapacity: CapData | null; hasBg: boolean; className?: string }) {
  const labelClass = `text-[10px] whitespace-nowrap ${hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500'}`;
  const valueClass = `text-xs font-medium ${hasBg ? 'text-white/90' : 'text-gray-700 dark:text-gray-300'}`;

  const renderCol = (label: string, data: CapData | null) => (
    <div>
      <div className={`mb-1 text-[10px] font-semibold uppercase tracking-wide ${hasBg ? 'text-white/60' : 'text-gray-500 dark:text-gray-400'}`}>
        {label}
      </div>
      {data ? (
        <div className="space-y-0.5">
          <div className="flex items-center gap-1">
            <div className="h-2 w-2 rounded-full bg-emerald-500 shrink-0" />
            <span className={valueClass}>{data.free_hours}h</span>
            <span className={labelClass}>frei</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="h-2 w-2 rounded-full bg-orange-400 shrink-0" />
            <span className={valueClass}>{data.meeting_hours}h</span>
            <span className={labelClass}>Meetings</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="h-2 w-2 rounded-full bg-blue-400 shrink-0" />
            <span className={valueClass}>{data.blocker_hours}h</span>
            <span className={labelClass}>verplant</span>
          </div>
          <div className="flex items-center gap-1">
            <div className="h-2 w-2 rounded-full bg-gray-400 shrink-0" />
            <span className={valueClass}>{data.work_days % 1 === 0 ? data.work_days : data.work_days.toFixed(1)}</span>
            <span className={labelClass}>Arbeitstage</span>
          </div>
        </div>
      ) : (
        <div className={`text-[10px] ${hasBg ? 'text-white/40' : 'text-gray-400'}`}>…</div>
      )}
    </div>
  );

  return (
    <div className={`rounded-xl border p-3 lg:p-4 ${className} ${
      hasBg
        ? 'bg-white/10 backdrop-blur-md border-white/20'
        : 'bg-white border-gray-200 dark:bg-gray-900 dark:border-gray-800'
    }`}>
      <div className="grid grid-cols-2 gap-3">
        {renderCol('Woche', weekCapacity)}
        {renderCol('Monat', monthCapacity)}
      </div>
    </div>
  );
}

/* ── Icons ── */

function SettingsGearIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.431.992a7.723 7.723 0 0 1 0 .255c-.007.378.138.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
    </svg>
  );
}

function ImageIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5a2.25 2.25 0 0 0 2.25-2.25V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
    </svg>
  );
}

function FilterIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 0 1-.659 1.591l-5.432 5.432a2.25 2.25 0 0 0-.659 1.591v2.927a2.25 2.25 0 0 1-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 0 0-.659-1.591L3.659 7.409A2.25 2.25 0 0 1 3 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0 1 12 3Z" />
    </svg>
  );
}

function MailDraftIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
    </svg>
  );
}

function TaskIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
    </svg>
  );
}

function CheckCircleIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
    </svg>
  );
}

function PencilIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L10.582 16.07a4.5 4.5 0 0 1-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 0 1 1.13-1.897l8.932-8.931Zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0 1 15.75 21H5.25A2.25 2.25 0 0 1 3 18.75V8.25A2.25 2.25 0 0 1 5.25 6H10" />
    </svg>
  );
}

function UsersIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 19.128a9.38 9.38 0 0 0 2.625.372 9.337 9.337 0 0 0 4.121-.952 4.125 4.125 0 0 0-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 0 1 8.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0 1 11.964-3.07M12 6.375a3.375 3.375 0 1 1-6.75 0 3.375 3.375 0 0 1 6.75 0Zm8.25 2.25a2.625 2.625 0 1 1-5.25 0 2.625 2.625 0 0 1 5.25 0Z" />
    </svg>
  );
}

function ClockIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
    </svg>
  );
}

function FlagIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3 3v1.5M3 21v-6m0 0 2.77-.693a9 9 0 0 1 6.208.682l.108.054a9 9 0 0 0 6.086.71l3.114-.732a48.524 48.524 0 0 1-.005-10.499l-3.11.732a9 9 0 0 1-6.085-.711l-.108-.054a9 9 0 0 0-6.208-.682L3 4.5M3 15V4.5" />
    </svg>
  );
}

function PaperclipIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m18.375 12.739-7.693 7.693a4.5 4.5 0 0 1-6.364-6.364l10.94-10.94A3 3 0 1 1 19.5 7.372L8.552 18.32m.009-.01-.01.01m5.699-9.941-7.81 7.81a1.5 1.5 0 0 0 2.112 2.13" />
    </svg>
  );
}

function ChevronDownIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
    </svg>
  );
}

function ChevronUpIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 15.75 7.5-7.5 7.5 7.5" />
    </svg>
  );
}

function XMarkIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
    </svg>
  );
}

// ── Fällige Kreditoren-Zahlungen (kompakt, im Cockpit) ──

interface UpcomingPayment {
  vendor: string;
  product?: string;
  next_date?: string;
  days_until?: number;
  amount_chf?: number;
  cycle?: string;
  invoice_id?: number;
}

function formatDateCH(iso: string | undefined): string {
  if (!iso) return '–';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}.${mm}.${d.getFullYear()}`;
}

function UpcomingPaymentsCard({ cardClass, textSecondary, textMuted, excludeVendors }: {
  cardClass: string; textSecondary: string; textMuted: string; excludeVendors: string | null;
}) {
  const navigate = useNavigate();
  const [payments, setPayments] = useState<UpcomingPayment[]>([]);
  const [pdfModalUrl, setPdfModalUrl] = useState<string | null>(null);

  useEffect(() => {
    const terms = parseExcludeVendors(excludeVendors);
    api.get<unknown>('/api/creditors/upcoming?n=20')
      .then(data => {
        const raw = Array.isArray(data) ? data : (data as Record<string, unknown>)?.payments as Record<string, unknown>[] || [];
        setPayments(raw.map((p: Record<string, unknown>) => ({
          vendor: (p.vendor ?? p.Kreditor ?? '–') as string,
          product: (p.product ?? p.Produkt) as string | undefined,
          next_date: (p.next_date ?? p.Renewal_Date_Parsed ?? p.Renewal_Date) as string | undefined,
          days_until: (p.days_until ?? p.Tage_bis_Renewal) as number | undefined,
          amount_chf: (p.amount_chf ?? p.Betrag_CHF) as number | undefined,
          cycle: (p.cycle ?? p.Abrechnungszyklus) as string | undefined,
          invoice_id: (p.invoice_id ?? p.index) as number | undefined,
        })).filter((p) => !isExcludedVendor(p.vendor, p.product, terms)));
      })
      .catch(() => {});
  }, [excludeVendors]);

  if (payments.length === 0) return null;

  const urgentCount = payments.filter(p => (p.days_until ?? 999) < 7).length;

  const handleRowClick = (p: UpcomingPayment) => {
    if (p.invoice_id != null) {
      setPdfModalUrl(`/api/creditors/invoice/${p.invoice_id}/pdf/view`);
    } else {
      navigate('/kreditoren');
    }
  };

  return (
    <>
      <section className={`rounded-xl border p-4 ${cardClass}`}>
        <div className="mb-3 flex items-center justify-between">
          <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
            Fällige Zahlungen
            {urgentCount > 0 && (
              <span className="ml-2 inline-flex items-center rounded-full bg-red-100 px-2 py-0.5 text-xs font-bold text-red-700 dark:bg-red-900/40 dark:text-red-400">
                {urgentCount}
              </span>
            )}
          </h2>
          <button
            onClick={() => navigate('/kreditoren')}
            className={`text-xs font-medium ${textMuted} transition-colors hover:text-indigo-500`}
          >
            Alle →
          </button>
        </div>
        <div className="max-h-56 space-y-2 overflow-y-auto">
          {payments.map((p, i) => (
            <div
              key={i}
              className="flex cursor-pointer items-center justify-between rounded-lg p-1.5 text-sm transition-colors hover:bg-gray-50 dark:hover:bg-gray-800/40"
              onClick={() => handleRowClick(p)}
            >
              <div className="flex items-center gap-2 min-w-0">
                {(p.days_until ?? 999) < 7 && <span className="h-2 w-2 shrink-0 rounded-full bg-red-500" />}
                {(p.days_until ?? 999) >= 7 && (p.days_until ?? 999) < 30 && <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500" />}
                {(p.days_until ?? 999) >= 30 && <span className="h-2 w-2 shrink-0 rounded-full bg-green-500" />}
                <span className="min-w-0">
                  <span className="block truncate text-gray-900 dark:text-white">{p.vendor}{p.product ? ` – ${p.product}` : ''}</span>
                  {/* Datum mobil unter den Namen: in einer Zeile blieben dem
                      Kreditor sonst knapp 100px und der Betrag wurde beschnitten. */}
                  <span className={`block text-xs tabular-nums sm:hidden ${textMuted}`}>
                    {formatDateCH(p.next_date)}
                  </span>
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <span className={`hidden w-[78px] text-right text-xs tabular-nums sm:block ${textMuted}`}>
                  {formatDateCH(p.next_date)}
                </span>
                <span className="whitespace-nowrap text-right font-medium tabular-nums text-gray-900 sm:w-[112px] dark:text-white">
                  {p.amount_chf != null
                    ? new Intl.NumberFormat('de-CH', { style: 'currency', currency: 'CHF' }).format(p.amount_chf)
                    : ''}
                </span>
              </div>
            </div>
          ))}
        </div>
      </section>
      {pdfModalUrl && (
        <PdfModal url={pdfModalUrl} onClose={() => setPdfModalUrl(null)} />
      )}
    </>
  );
}

function PdfModal({ url, onClose }: { url: string; onClose: () => void }) {
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
