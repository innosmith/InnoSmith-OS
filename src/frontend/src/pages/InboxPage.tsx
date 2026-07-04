import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { EmailBody } from '../components/EmailBody';
import { RichTextEditor } from '../components/RichTextEditor';
import { ConfidenceBadge } from '../components/agent/ConfidenceBadge';

/* ---------- Typen ---------- */

interface CalendarEvent {
  id: string;
  subject: string | null;
  start: string | null;
  end: string | null;
  is_all_day: boolean;
  location: string | null;
  show_as: string | null;
}

interface EmailSummary {
  id: string;
  subject: string | null;
  from_address: string | null;
  from_name: string | null;
  received_at: string | null;
  is_read: boolean;
  body_preview: string | null;
  categories: string[];
  inference_classification: string | null;
  importance: string | null;
  has_attachments: boolean;
  flag_status: string | null;
}

interface EmailDetail {
  id: string;
  subject: string | null;
  from_address: string | null;
  from_name: string | null;
  to_recipients: string[];
  cc_recipients: string[];
  received_at: string | null;
  body_html: string | null;
  body_preview: string | null;
  categories: string[];
  inference_classification: string | null;
  importance: string | null;
  has_attachments: boolean;
  is_read: boolean;
}

interface FolderInfo {
  id: string;
  display_name: string;
  total_count: number;
}

interface EmailListResponse {
  emails: EmailSummary[];
  total: number | null;
}

interface TriageItem {
  id: string;
  message_id: string;
  subject: string | null;
  from_address: string | null;
  from_name: string | null;
  received_at: string | null;
  inference_class: string | null;
  triage_class: string | null;
  confidence: number | null;
  suggested_action: {
    triage_class?: string;
    confidence?: number;
    rationale?: string;
    suggested_action?: { type?: string; detail?: string };
    type?: string;
    detail?: string;
  } | null;
  agent_job_id: string | null;
  status: string;
  created_at: string;
}

interface TriageStats {
  by_status: Record<string, number>;
  by_class: Record<string, number>;
  total_pending: number;
  followups_due?: number;
}

interface EmailSearchHit {
  source_type: string;
  source_id: string;
  title: string | null;
  snippet: string | null;
  score: number | null;
  similarity: number | null;
}

/* ---------- Triage-Farben & Labels ---------- */

const TRIAGE_CONFIG: Record<string, { label: string; bg: string; text: string; dot: string }> = {
  auto_reply: {
    label: 'Auto-Antwort',
    bg: 'bg-emerald-100 dark:bg-emerald-900/40',
    text: 'text-emerald-700 dark:text-emerald-300',
    dot: 'bg-emerald-500',
  },
  task: {
    label: 'Aufgabe',
    bg: 'bg-blue-100 dark:bg-blue-900/40',
    text: 'text-blue-700 dark:text-blue-300',
    dot: 'bg-blue-500',
  },
  fyi: {
    label: 'Zur Kenntnis',
    bg: 'bg-gray-100 dark:bg-gray-800',
    text: 'text-gray-600 dark:text-gray-400',
    dot: 'bg-gray-400',
  },
};

function TriageBadge({ triageClass }: { triageClass: string | null | undefined }) {
  if (!triageClass) return null;
  const cfg = TRIAGE_CONFIG[triageClass];
  if (!cfg) return null;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${cfg.bg} ${cfg.text}`}>
      <span className={`inline-block h-1.5 w-1.5 rounded-full ${cfg.dot}`} />
      {cfg.label}
    </span>
  );
}

const CATEGORY_COLORS: Record<string, string> = {
  wichtig: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  important: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
  finanzen: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  finance: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  newsletter: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
};

function getCategoryClass(cat: string): string {
  return CATEGORY_COLORS[cat.toLowerCase()] || 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400';
}

/* ---------- Haupt-Komponente ---------- */

export function InboxPage() {
  const navigate = useNavigate();
  const [emails, setEmails] = useState<EmailSummary[]>([]);
  const [folders, setFolders] = useState<FolderInfo[]>([]);
  const [hiddenFolders, setHiddenFolders] = useState<string[]>([]);
  const [selectedEmail, setSelectedEmail] = useState<EmailDetail | null>(null);
  const [activeFolder, setActiveFolder] = useState('inbox');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCompose, setShowCompose] = useState(false);
  const [composeReplyTo, setComposeReplyTo] = useState<string | undefined>();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [activeTab, setActiveTab] = useState<'email' | 'teams'>('email');

  const [triageMap, setTriageMap] = useState<Record<string, TriageItem>>({});
  const [triageStats, setTriageStats] = useState<TriageStats | null>(null);
  const sseRef = useRef<EventSource | null>(null);
  const [senderAvatars, setSenderAvatars] = useState<Record<string, string | null>>({});
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [reclassReason, setReclassReason] = useState('');
  const [reclassBusy, setReclassBusy] = useState(false);
  const [reclassDone, setReclassDone] = useState<string | null>(null);

  // Semantische E-Mail-Suche (gleicher Backend-Endpoint wie die globale Suche)
  const [searchTerm, setSearchTerm] = useState('');
  const [searchHits, setSearchHits] = useState<EmailSearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchActive, setSearchActive] = useState(false);

  const runEmailSearch = useCallback(async () => {
    const q = searchTerm.trim();
    if (q.length < 2) return;
    setSearching(true);
    setSearchActive(true);
    try {
      const data = await api.get<{ results: EmailSearchHit[] }>(
        `/api/search/semantic?q=${encodeURIComponent(q)}&mode=hybrid&sources=email&limit=25`,
      );
      setSearchHits(data.results || []);
    } catch {
      setSearchHits([]);
    } finally {
      setSearching(false);
    }
  }, [searchTerm]);

  const clearEmailSearch = useCallback(() => {
    setSearchTerm('');
    setSearchHits([]);
    setSearchActive(false);
  }, []);

  /* -- E-Mails laden -- */
  const fetchEmails = useCallback(async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams({
        folder: activeFolder,
        top: '30',
        ...(unreadOnly ? { unread_only: 'true' } : {}),
      });
      const data = await api.get<EmailListResponse>(`/api/emails?${params}`);
      setEmails(data.emails);
      setError(null);
    } catch (e) {
      if (e instanceof Error && e.message.includes('503')) {
        setError('Graph API nicht konfiguriert. Bitte TP_GRAPH_* Umgebungsvariablen setzen.');
      } else {
        setError(e instanceof Error ? e.message : 'Fehler beim Laden');
      }
    } finally {
      setLoading(false);
    }
  }, [activeFolder, unreadOnly]);

  /* -- Triage-Daten laden -- */
  const fetchTriage = useCallback(async () => {
    try {
      const [items, stats] = await Promise.all([
        api.get<TriageItem[]>('/api/triage?limit=200'),
        api.get<TriageStats>('/api/triage/stats'),
      ]);
      const map: Record<string, TriageItem> = {};
      for (const item of items) {
        map[item.message_id] = item;
      }
      setTriageMap(map);
      setTriageStats(stats);
    } catch {
      /* triage optional */
    }
  }, []);

  /* -- Initiales Laden -- */
  useEffect(() => {
    fetchEmails();
    api.get<{ inbox_background_url: string | null }>('/api/settings')
      .then(s => setBgUrl(s.inbox_background_url))
      .catch(() => {});
  }, [fetchEmails]);

  useEffect(() => {
    fetchTriage();
    api.get<FolderInfo[]>('/api/emails/folders').then(setFolders).catch(() => {});
    api.get<{ inbox_hidden_folders: string[] | null }>('/api/settings/triage')
      .then(s => { if (s.inbox_hidden_folders) setHiddenFolders(s.inbox_hidden_folders); })
      .catch(err => { console.warn('Triage-Einstellungen konnten nicht geladen werden:', err); });
  }, [fetchTriage]);

  /* -- SSE für Live-Updates -- */
  useEffect(() => {
    const token = localStorage.getItem('taskpilot_token');
    if (!token) return;
    const es = new EventSource(`/api/sse/events?token=${token}`);
    sseRef.current = es;
    es.addEventListener('email_triage_changed', () => {
      fetchTriage();
    });
    es.onerror = () => {
      es.close();
    };
    return () => es.close();
  }, [fetchTriage]);

  /* -- E-Mail öffnen -- */
  const openEmail = async (email: EmailSummary) => {
    setDetailLoading(true);
    try {
      const detail = await api.get<EmailDetail>(`/api/emails/${email.id}`);
      setSelectedEmail(detail);
      if (!email.is_read) {
        await api.patch(`/api/emails/${email.id}/read`);
        setEmails((prev) => prev.map((e) => (e.id === email.id ? { ...e, is_read: true } : e)));
      }
    } catch {
      /* ignore */
    } finally {
      setDetailLoading(false);
    }
  };

  /* -- E-Mail per ID öffnen (aus Suchtreffer) -- */
  const openEmailById = async (messageId: string) => {
    setDetailLoading(true);
    try {
      const detail = await api.get<EmailDetail>(`/api/emails/${messageId}`);
      setSelectedEmail(detail);
      if (!detail.is_read) {
        await api.patch(`/api/emails/${messageId}/read`).catch(() => {});
      }
    } catch {
      /* ignore */
    } finally {
      setDetailLoading(false);
    }
  };

  /* -- Triage-Aktionen -- */
  const dismissTriage = async (triageId: string) => {
    try {
      await api.post(`/api/triage/${triageId}/dismiss`);
      await fetchTriage();
    } catch {
      /* ignore */
    }
  };

  /* -- Triage-Klasse korrigieren (lernen + ausfuehren) -- */
  const reclassifyTriage = async (triageId: string, targetClass: 'auto_reply' | 'task' | 'fyi') => {
    setReclassBusy(true);
    try {
      await api.post(`/api/triage/${triageId}/reclassify`, {
        triage_class: targetClass,
        reason: reclassReason.trim() || undefined,
      });
      setReclassReason('');
      const label = targetClass === 'auto_reply' ? 'Entwurf' : targetClass === 'task' ? 'Aufgabe' : 'FYI';
      setReclassDone(
        targetClass === 'fyi'
          ? 'Gelernt: als FYI markiert.'
          : `InnoPilot lernt aus der Korrektur und erzeugt jetzt ${label === 'Entwurf' ? 'einen' : 'eine'} ${label}.`,
      );
      await fetchTriage();
      setTimeout(() => setReclassDone(null), 6000);
    } catch {
      setReclassDone('Korrektur fehlgeschlagen.');
      setTimeout(() => setReclassDone(null), 4000);
    } finally {
      setReclassBusy(false);
    }
  };

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    if (isToday) return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    return d.toLocaleDateString('de-DE', { day: 'numeric', month: 'short' });
  };

  const selectedTriage = selectedEmail ? triageMap[selectedEmail.id] : null;

  useEffect(() => {
    const uniqueAddresses = [...new Set(emails.map(e => e.from_address).filter(Boolean))] as string[];
    const unknownAddresses = uniqueAddresses.filter(addr => senderAvatars[addr] === undefined);
    if (unknownAddresses.length === 0) return;

    const placeholder: Record<string, string | null> = {};
    for (const addr of unknownAddresses) placeholder[addr] = null;
    setSenderAvatars(prev => ({ ...prev, ...placeholder }));

    api.post<{ email: string; person: { id: number; pic_url: string | null } | null }[]>(
      '/api/pipedrive/lookup-emails',
      { emails: unknownAddresses }
    )
      .then(results => {
        const updates: Record<string, string | null> = {};
        for (const r of results) {
          if (r.person?.pic_url) updates[r.email] = r.person.pic_url;
        }
        if (Object.keys(updates).length > 0) {
          setSenderAvatars(prev => ({ ...prev, ...updates }));
        }
      })
      .catch(() => {});
  }, [emails]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { inbox_background_url: url });
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
      {/* Header */}
      <div className={`border-b px-4 py-4 sm:px-6 backdrop-blur-xl ${hasBg ? 'border-white/10 bg-black/35' : 'border-gray-200/60 bg-white/80 dark:border-gray-800/60 dark:bg-gray-900/80'}`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-6">
            <div>
              <h1 className={`text-xl font-bold ${hasBg ? 'text-white' : 'text-gray-900 dark:text-white'}`}>Posteingang</h1>
              <p className={`mt-0.5 text-sm ${hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'}`}>
                Nachrichten verwalten und beantworten
              </p>
            </div>
            <div className="flex rounded-lg border border-gray-200/60 bg-white/50 dark:border-gray-700/60 dark:bg-gray-800/50">
              {[
                { id: 'email' as const, label: 'E-Mail', icon: 'M21.75 6.75v10.5a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25m19.5 0v.243a2.25 2.25 0 0 1-1.07 1.916l-7.5 4.615a2.25 2.25 0 0 1-2.36 0L3.32 8.91a2.25 2.25 0 0 1-1.07-1.916V6.75' },
                { id: 'teams' as const, label: 'Teams', icon: 'M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155' },
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => { setActiveTab(tab.id); setSelectedEmail(null); }}
                  className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors first:rounded-l-lg last:rounded-r-lg ${
                    activeTab === tab.id
                      ? 'bg-indigo-600 text-white'
                      : hasBg ? 'text-white/70 hover:bg-white/10' : 'text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'
                  }`}
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d={tab.icon} />
                  </svg>
                  {tab.label}
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-3">
            {activeTab === 'email' && (
              <div className={`hidden items-center gap-1.5 rounded-lg border px-2.5 py-1.5 sm:flex ${hasBg ? 'border-white/20 bg-black/30' : 'border-gray-200/70 bg-white/60 dark:border-gray-700/60 dark:bg-gray-800/50'}`}>
                <svg className={`h-4 w-4 shrink-0 ${hasBg ? 'text-white/60' : 'text-gray-400'}`} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
                </svg>
                <input
                  type="text"
                  data-testid="inbox-search-input"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') runEmailSearch(); if (e.key === 'Escape') clearEmailSearch(); }}
                  placeholder="E-Mails durchsuchen…"
                  className={`w-44 bg-transparent text-xs outline-none ${hasBg ? 'text-white placeholder:text-white/50' : 'text-gray-900 placeholder:text-gray-400 dark:text-white dark:placeholder:text-gray-500'}`}
                />
                {searching && <span className="h-3 w-3 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />}
                {searchActive && !searching && (
                  <button onClick={clearEmailSearch} title="Suche zurücksetzen" className={`shrink-0 ${hasBg ? 'text-white/60 hover:text-white' : 'text-gray-400 hover:text-gray-600'}`}>
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
                    </svg>
                  </button>
                )}
              </div>
            )}
            <button
              onClick={() => setBgPickerOpen(true)}
              className={`rounded-lg p-2 transition-colors ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`}
              title="Hintergrund ändern"
            >
              <svg className="h-4.5 w-4.5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5a2.25 2.25 0 0 0 2.25-2.25V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
              </svg>
            </button>
            <label className={`flex items-center gap-2 text-xs cursor-pointer select-none ${hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'}`}>
              <input
                type="checkbox"
                checked={unreadOnly}
                onChange={(e) => setUnreadOnly(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
              />
              Nur ungelesen
            </label>
            <button
              onClick={fetchEmails}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-xs font-medium text-white shadow-sm transition-all hover:bg-indigo-700 hover:shadow active:scale-[0.97]"
            >
              Aktualisieren
            </button>
            <button
              onClick={() => { setComposeReplyTo(undefined); setShowCompose(true); }}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-xs font-medium text-white shadow-sm transition-all hover:bg-emerald-700 hover:shadow active:scale-[0.97]"
            >
              Entwurf schreiben
            </button>
          </div>
        </div>
      </div>

      {activeTab === 'teams' && <TeamsChatPanel hasBg={hasBg} />}

      {activeTab === 'email' && <>
      {/* Mobile folder tabs */}
      <div className={`relative flex items-stretch gap-1 overflow-x-auto border-b px-3 md:hidden ${hasBg ? 'border-white/10 bg-black/30 backdrop-blur-sm' : 'border-gray-200/60 bg-white/60 backdrop-blur-sm dark:border-gray-800/60 dark:bg-gray-900/60'} [scrollbar-width:none] [&::-webkit-scrollbar]:hidden`} style={{ WebkitOverflowScrolling: 'touch', maskImage: 'linear-gradient(to right, transparent 0, black 8px, black calc(100% - 24px), transparent 100%)' }}>
        {[
          { id: 'inbox', name: 'Posteingang' },
          { id: 'drafts', name: 'Entwürfe' },
          { id: 'sentitems', name: 'Gesendet' },
          ...folders
            .filter(f => !['Inbox', 'Drafts', 'Sent Items', 'Deleted Items', 'Junk Email', ...hiddenFolders].includes(f.display_name))
            .map(f => ({ id: f.id, name: f.display_name })),
        ].map(folder => (
          <button
            key={folder.id}
            onClick={() => { setActiveFolder(folder.id); setSelectedEmail(null); }}
            className={`relative shrink-0 whitespace-nowrap px-3 py-2.5 text-xs font-medium transition-colors ${
              activeFolder === folder.id
                ? hasBg ? 'text-white' : 'text-indigo-600 dark:text-indigo-400'
                : hasBg ? 'text-white/50' : 'text-gray-500 dark:text-gray-400'
            }`}
          >
            {folder.name}
            {activeFolder === folder.id && <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-indigo-500" />}
          </button>
        ))}
      </div>

      {/* Triage-Statistik-Leiste */}
      {triageStats && (triageStats.total_pending > 0 || (triageStats.followups_due ?? 0) > 0) && (
        <div className={`flex items-center gap-4 border-b px-4 py-2.5 text-xs sm:px-6 backdrop-blur-sm ${hasBg ? 'border-white/10 bg-black/30' : 'border-gray-200/60 bg-white/60 dark:border-gray-800/60 dark:bg-gray-900/60'}`}>
          <span className={`font-semibold ${hasBg ? 'text-white/80' : 'text-gray-700 dark:text-gray-300'}`}>Triage:</span>
          {Object.entries(triageStats.by_class).map(([cls, count]) => {
            const cfg = TRIAGE_CONFIG[cls];
            return (
              <span key={cls} className="flex items-center gap-1 text-gray-600 dark:text-gray-400">
                {cfg && <span className={`inline-block h-2 w-2 rounded-full ${cfg.dot}`} />}
                <span className="font-medium">{count}</span> {cfg?.label || cls}
              </span>
            );
          })}
          {(triageStats.followups_due ?? 0) > 0 && (
            <button
              onClick={() => navigate('/')}
              className="flex items-center gap-1 font-medium text-orange-600 transition-colors hover:text-orange-700 dark:text-orange-400 dark:hover:text-orange-300"
              title="Nachfass-Vorschläge im Cockpit prüfen"
            >
              <span className="inline-block h-2 w-2 rounded-full bg-orange-500" />
              {triageStats.followups_due} Follow-up{(triageStats.followups_due ?? 0) > 1 ? 's' : ''} fällig
            </button>
          )}
          <span className="ml-auto text-gray-400 dark:text-gray-500">
            {triageStats.total_pending} offen
          </span>
        </div>
      )}

      {error && (
        <div className="mx-6 mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-400">
          {error}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        {/* Folder sidebar */}
        <div className={`hidden w-52 shrink-0 border-r p-4 backdrop-blur-sm md:block ${hasBg ? 'border-white/10 bg-black/30' : 'border-gray-200/60 bg-white/40 dark:border-gray-800/60 dark:bg-gray-900/40'}`}>
          <h3 className={`mb-3 text-[11px] font-semibold uppercase tracking-wider ${hasBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500'}`}>
            Ordner
          </h3>
          <div className="space-y-1">
            {[
              { id: 'inbox', name: 'Posteingang' },
              { id: 'drafts', name: 'Entwürfe' },
              { id: 'sentitems', name: 'Gesendet' },
              ...(folders
                .filter((f) => !['Inbox', 'Drafts', 'Sent Items', 'Deleted Items', 'Junk Email', ...hiddenFolders].includes(f.display_name))
                .map((f) => ({ id: f.id, name: f.display_name }))),
            ].map((folder) => (
              <button
                key={folder.id}
                onClick={() => { setActiveFolder(folder.id); setSelectedEmail(null); }}
                className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium transition-all ${
                  activeFolder === folder.id
                    ? hasBg
                      ? 'bg-white/20 text-white shadow-sm'
                      : 'bg-indigo-100/80 text-indigo-700 shadow-sm dark:bg-indigo-950/60 dark:text-indigo-300'
                    : hasBg
                      ? 'text-white/70 hover:bg-white/10 hover:text-white'
                      : 'text-gray-600 hover:bg-gray-100/80 dark:text-gray-400 dark:hover:bg-gray-800/60'
                }`}
              >
                {folder.name}
              </button>
            ))}
          </div>
        </div>

        {/* Email list */}
        <div className={`${selectedEmail ? 'hidden md:block md:w-[380px]' : 'w-full'} shrink-0 overflow-y-auto overflow-x-hidden overscroll-x-none border-r ${hasBg ? 'border-white/10' : 'border-gray-200/60 dark:border-gray-800/60'}`}>
          {searchActive ? (
            <div className="p-3">
              <div className={`mb-2 flex items-center justify-between px-1 text-[11px] font-medium ${hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'}`}>
                <span>{searching ? 'Suche läuft…' : `${searchHits.length} Treffer für «${searchTerm.trim()}»`}</span>
                <button onClick={clearEmailSearch} className="text-indigo-500 hover:text-indigo-600 dark:text-indigo-400">Zurück zum Ordner</button>
              </div>
              {!searching && searchHits.length === 0 ? (
                <div className="flex h-40 flex-col items-center justify-center gap-1 text-sm text-gray-400 dark:text-gray-600">
                  Keine E-Mails gefunden
                </div>
              ) : (
                <div className="space-y-2">
                  {searchHits.map((h) => (
                    <button
                      key={`${h.source_id}`}
                      onClick={() => openEmailById(h.source_id)}
                      className={`group flex w-full flex-col gap-1 rounded-xl px-4 py-3 text-left transition-all ${
                        hasBg
                          ? 'bg-black/30 shadow-sm backdrop-blur-lg hover:bg-black/40'
                          : 'bg-white/70 shadow-sm hover:bg-white hover:shadow-md dark:bg-gray-900/50 dark:hover:bg-gray-900/80'
                      }`}
                    >
                      <span className={`truncate text-[13px] font-semibold ${hasBg ? 'text-white drop-shadow' : 'text-gray-900 dark:text-white'}`}>
                        {h.title || '(Kein Betreff)'}
                      </span>
                      {h.snippet && (
                        <span className={`line-clamp-2 text-xs ${hasBg ? 'text-white/70 drop-shadow-sm' : 'text-gray-500 dark:text-gray-400'}`}>
                          {h.snippet}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : loading ? (
            <div className="flex h-full items-center justify-center">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
            </div>
          ) : emails.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-gray-400 dark:text-gray-600">
              <svg className="h-12 w-12" fill="none" viewBox="0 0 24 24" strokeWidth={1} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 9v.906a2.25 2.25 0 0 1-1.183 1.981l-6.478 3.488M2.25 9v.906a2.25 2.25 0 0 0 1.183 1.981l6.478 3.488m8.839 2.51-4.66-2.51m0 0-1.023-.55a2.25 2.25 0 0 0-2.134 0l-1.022.55m0 0-4.661 2.51m16.5 1.615a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V8.844a2.25 2.25 0 0 1 1.183-1.981l7.5-4.039a2.25 2.25 0 0 1 2.134 0l7.5 4.039a2.25 2.25 0 0 1 1.183 1.98V19.5Z" />
              </svg>
              <span className="text-sm">Keine E-Mails</span>
            </div>
          ) : (
            <div className="space-y-2 p-3">
              {emails.map((email) => {
                const triage = triageMap[email.id];
                const initials = (email.from_name || email.from_address || '?')
                  .split(/[\s.@]+/)
                  .filter(Boolean)
                  .slice(0, 2)
                  .map(p => p[0]?.toUpperCase() || '')
                  .join('');
                const isSelected = selectedEmail?.id === email.id;
                const avatarColors = [
                  'bg-blue-500', 'bg-emerald-500', 'bg-violet-500', 'bg-rose-500',
                  'bg-amber-500', 'bg-teal-500', 'bg-pink-500', 'bg-indigo-500',
                ];
                const colorIdx = (email.from_address || '').split('').reduce((a, c) => a + c.charCodeAt(0), 0) % avatarColors.length;
                return (
                  <button
                    key={email.id}
                    onClick={() => openEmail(email)}
                    className={`group flex w-full items-start gap-3 rounded-xl px-4 py-3 text-left transition-all ${
                      hasBg
                        ? isSelected
                          ? 'bg-black/40 shadow-lg backdrop-blur-xl ring-1 ring-white/20'
                          : 'bg-black/30 shadow-sm backdrop-blur-lg hover:bg-black/40'
                        : isSelected
                          ? 'bg-indigo-50/80 shadow-md ring-1 ring-indigo-200/50 dark:bg-indigo-950/40 dark:ring-indigo-800/50'
                          : !email.is_read
                            ? 'bg-white/80 shadow-sm hover:bg-white hover:shadow-md dark:bg-gray-900/60 dark:hover:bg-gray-900/80'
                            : 'bg-white/50 shadow-sm hover:bg-white/70 dark:bg-gray-900/40 dark:hover:bg-gray-900/60'
                    }`}
                  >
                    {/* Avatar */}
                    {senderAvatars[email.from_address || ''] ? (
                      <img
                        src={senderAvatars[email.from_address || '']!}
                        alt=""
                        className="h-10 w-10 shrink-0 rounded-full object-cover"
                      />
                    ) : (
                      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white ${avatarColors[colorIdx]}`}>
                        {initials}
                      </div>
                    )}

                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5 min-w-0">
                          {email.flag_status === 'flagged' && (
                            <svg className="h-4 w-4 shrink-0 text-orange-500" viewBox="0 0 20 20" fill="currentColor">
                              <path d="M3.5 2.75a.75.75 0 0 0-1.5 0v14.5a.75.75 0 0 0 1.5 0v-4.392l1.657-.348a6.25 6.25 0 0 1 4.44.05l.29.12a7.75 7.75 0 0 0 4.908.293l2.054-.492a.75.75 0 0 0 .582-.73V3.498a.75.75 0 0 0-.96-.72l-2.143.513a6.25 6.25 0 0 1-3.965-.236l-.29-.12a7.75 7.75 0 0 0-5.507-.062L3.5 3.22V2.75Z" />
                            </svg>
                          )}
                          <span className={`text-[13px] truncate ${hasBg ? (!email.is_read ? 'font-semibold text-white drop-shadow' : 'font-medium text-white/90 drop-shadow-sm') : (!email.is_read ? 'font-semibold text-gray-900 dark:text-white' : 'font-medium text-gray-600 dark:text-gray-300')}`}>
                            {email.from_name || email.from_address || 'Unbekannt'}
                          </span>
                        </div>
                        <div className="flex shrink-0 items-center gap-1.5">
                          {email.has_attachments && (
                            <svg className={`h-3 w-3 ${hasBg ? 'text-white/60' : 'text-gray-400 dark:text-gray-500'}`} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" d="m18.375 12.739-7.693 7.693a4.5 4.5 0 0 1-6.364-6.364l10.94-10.94A3 3 0 1 1 19.5 7.372L8.552 18.32m.009-.01-.01.01m5.699-9.941-7.81 7.81a1.5 1.5 0 0 0 2.112 2.13" />
                            </svg>
                          )}
                          <span className={`text-[10px] ${hasBg ? 'text-white/70 drop-shadow-sm' : 'text-gray-400 dark:text-gray-500'}`}>
                            {formatDate(email.received_at)}
                          </span>
                        </div>
                      </div>
                      <div className="mt-0.5 flex items-center gap-2">
                        <p className={`flex-1 truncate text-[13px] ${hasBg ? (!email.is_read ? 'font-medium text-white/95 drop-shadow-sm' : 'text-white/80 drop-shadow-sm') : (!email.is_read ? 'font-medium text-gray-800 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400')}`}>
                          {email.subject || '(Kein Betreff)'}
                        </p>
                        {triage && triage.status === 'pending' && (
                          <>
                            <TriageBadge triageClass={triage.triage_class} />
                            <ConfidenceBadge confidence={triage.confidence ?? triage.suggested_action?.confidence} dotOnly glassBg={hasBg} />
                          </>
                        )}
                      </div>
                      <p className={`mt-0.5 truncate text-xs ${hasBg ? 'text-white/60 drop-shadow-sm' : 'text-gray-400 dark:text-gray-500'}`}>
                        {email.body_preview}
                      </p>
                      {email.categories.length > 0 && (
                        <div className="mt-1 flex gap-1">
                          {email.categories.map((cat) => (
                            <span key={cat} className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${getCategoryClass(cat)}`}>
                              {cat}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    {!email.is_read && (
                      <div className="mt-2 h-2 w-2 shrink-0 rounded-full bg-indigo-500" />
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Detail pane */}
        <div className={`${selectedEmail ? 'flex-1' : 'hidden md:flex md:flex-1'} overflow-y-auto overflow-x-hidden overscroll-x-none ${hasBg ? 'bg-black/20 backdrop-blur-sm' : 'bg-white/40 dark:bg-gray-900/20'}`}>
          {detailLoading ? (
            <div className="flex h-full items-center justify-center">
              <div className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
            </div>
          ) : selectedEmail ? (
            <div className="p-6">
              {/* Aktionsleiste */}
              <div className="mb-5 flex flex-wrap items-center gap-2 rounded-xl border border-gray-200/80 bg-white/80 p-3 shadow-sm backdrop-blur-sm dark:border-gray-700/60 dark:bg-gray-900/80">
                {selectedTriage && selectedTriage.status === 'pending' && (
                  <TriageBadge triageClass={selectedTriage.triage_class} />
                )}
                {selectedTriage && selectedTriage.status === 'pending' && (
                  <ConfidenceBadge confidence={selectedTriage.confidence ?? selectedTriage.suggested_action?.confidence} />
                )}
                {selectedTriage?.suggested_action?.rationale && (
                  <span className="text-xs text-gray-500 dark:text-gray-400 italic">
                    {selectedTriage.suggested_action.rationale}
                  </span>
                )}
                <div className="ml-auto flex items-center gap-2">
                  <button
                    onClick={() => {
                      setComposeReplyTo(selectedEmail.id);
                      setShowCompose(true);
                    }}
                    className="rounded-lg bg-emerald-600 px-4 py-2 text-xs font-medium text-white shadow-sm transition-all hover:bg-emerald-700 hover:shadow active:scale-[0.97]"
                  >
                    Antwort entwerfen
                  </button>
                  <button
                    onClick={() => {
                      const event = new CustomEvent('open-create-task-from-email', {
                        detail: {
                          emailId: selectedEmail.id,
                          subject: selectedEmail.subject,
                          bodyPreview: selectedEmail.body_preview,
                          fromAddress: selectedEmail.from_address,
                          triageId: selectedTriage?.id,
                        },
                      });
                      window.dispatchEvent(event);
                    }}
                    className="rounded-lg bg-blue-600 px-4 py-2 text-xs font-medium text-white shadow-sm transition-all hover:bg-blue-700 hover:shadow active:scale-[0.97]"
                  >
                    Task erstellen
                  </button>
                  {selectedTriage && selectedTriage.status === 'pending' && (
                    <button
                      onClick={() => dismissTriage(selectedTriage.id)}
                      className="rounded-lg bg-gray-200 px-3 py-1.5 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-300 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      Archivieren / FYI
                    </button>
                  )}
                </div>
              </div>

              {/* Triage-Korrektur: lernen + ausfuehren */}
              {selectedTriage && (
                <div className="mb-5 rounded-xl border border-indigo-200/70 bg-indigo-50/60 p-3 dark:border-indigo-800/50 dark:bg-indigo-950/30">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-medium text-indigo-700 dark:text-indigo-300">
                      Einschätzung korrigieren:
                    </span>
                    {([
                      { cls: 'auto_reply', label: 'Entwurf' },
                      { cls: 'task', label: 'Aufgabe' },
                      { cls: 'fyi', label: 'FYI' },
                    ] as const).map(({ cls, label }) => (
                      <button
                        key={cls}
                        disabled={reclassBusy || selectedTriage.triage_class === cls}
                        onClick={() => reclassifyTriage(selectedTriage.id, cls)}
                        className={`rounded-full px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                          selectedTriage.triage_class === cls
                            ? 'bg-indigo-600 text-white'
                            : 'bg-white text-indigo-700 ring-1 ring-indigo-200 hover:bg-indigo-100 dark:bg-gray-800 dark:text-indigo-300 dark:ring-indigo-800 dark:hover:bg-gray-700'
                        }`}
                      >
                        → {label}
                      </button>
                    ))}
                    <input
                      type="text"
                      value={reclassReason}
                      onChange={(e) => setReclassReason(e.target.value)}
                      placeholder="Grund (optional) – hilft beim Lernen"
                      className="min-w-[180px] flex-1 rounded-lg border border-indigo-200/70 bg-white/80 px-3 py-1 text-xs text-gray-700 placeholder:text-gray-400 focus:outline-none focus:ring-1 focus:ring-indigo-400 dark:border-indigo-800/50 dark:bg-gray-900/60 dark:text-gray-200"
                    />
                  </div>
                  {reclassDone && (
                    <p className="mt-2 text-xs text-indigo-600 dark:text-indigo-400">{reclassDone}</p>
                  )}
                </div>
              )}

              {/* Kalender-Kontext */}
              <CalendarContext selectedTriage={selectedTriage} emailSubject={selectedEmail.subject} />

              {/* E-Mail Header */}
              <div className="mb-4 flex items-start justify-between">
                <div>
                  <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                    {selectedEmail.subject || '(Kein Betreff)'}
                  </h2>
                  <div className="mt-1 flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                    <span className="font-medium text-gray-700 dark:text-gray-300">
                      {selectedEmail.from_name || selectedEmail.from_address}
                    </span>
                    {selectedEmail.from_name && (
                      <span className="text-xs">&lt;{selectedEmail.from_address}&gt;</span>
                    )}
                  </div>
                  {selectedEmail.to_recipients.length > 0 && (
                    <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
                      An: {selectedEmail.to_recipients.join(', ')}
                    </p>
                  )}
                  <p className="mt-0.5 text-xs text-gray-400 dark:text-gray-500">
                    {selectedEmail.received_at ? new Date(selectedEmail.received_at).toLocaleString('de-DE') : ''}
                  </p>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => setSelectedEmail(null)}
                    className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 md:hidden"
                  >
                    <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>

              {/* Kategorien */}
              {selectedEmail.categories.length > 0 && (
                <div className="mb-4 flex gap-1.5">
                  {selectedEmail.categories.map((cat) => (
                    <span key={cat} className={`rounded-full px-2 py-0.5 text-xs font-medium ${getCategoryClass(cat)}`}>
                      {cat}
                    </span>
                  ))}
                  {selectedEmail.inference_classification && (
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-400">
                      {selectedEmail.inference_classification === 'focused' ? 'Relevant' : 'Sonstige'}
                    </span>
                  )}
                </div>
              )}

              {/* Body */}
              {selectedEmail.body_html ? (
                <EmailBody html={selectedEmail.body_html} />
              ) : (
                <p className="whitespace-pre-wrap text-sm text-gray-700 dark:text-gray-300">
                  {selectedEmail.body_preview}
                </p>
              )}
            </div>
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
              <svg className="h-16 w-16 text-gray-200 dark:text-gray-700" fill="none" viewBox="0 0 24 24" strokeWidth={0.8} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25m19.5 0v.243a2.25 2.25 0 0 1-1.07 1.916l-7.5 4.615a2.25 2.25 0 0 1-2.36 0L3.32 8.91a2.25 2.25 0 0 1-1.07-1.916V6.75" />
              </svg>
              <p className="text-sm font-medium text-gray-400 dark:text-gray-500">E-Mail auswählen</p>
              <p className="text-xs text-gray-300 dark:text-gray-600">Klicke auf eine E-Mail, um Details anzuzeigen</p>
            </div>
          )}
        </div>
      </div>

      {/* Compose dialog */}
      {showCompose && (
        <ComposeDialog
          replyToId={composeReplyTo}
          onClose={() => setShowCompose(false)}
          onSent={() => { setShowCompose(false); fetchEmails(); }}
        />
      )}

      {/* CreateTaskFromEmail dialog */}
      <CreateTaskFromEmailListener onTaskCreated={fetchTriage} />
      </>}
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

/* ---------- Compose Dialog ---------- */

function ComposeDialog({
  replyToId,
  onClose,
  onSent,
}: {
  replyToId?: string;
  onClose: () => void;
  onSent: () => void;
}) {
  const [to, setTo] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [saving, setSaving] = useState(false);
  const [draftId, setDraftId] = useState<string | null>(null);

  useEffect(() => {
    if (replyToId) {
      api.get<EmailDetail>(`/api/emails/${replyToId}`).then((detail) => {
        setTo(detail.from_address || '');
        setSubject(detail.subject ? `Re: ${detail.subject.replace(/^Re:\s*/i, '')}` : '');
      }).catch(() => {});
    }
  }, [replyToId]);

  const saveDraft = async () => {
    if (!to.trim() || !subject.trim()) return;
    setSaving(true);
    try {
      const res = await api.post<{ id: string }>('/api/emails/drafts', {
        subject,
        body_html: `<p>${body.replace(/\n/g, '<br/>')}</p>`,
        to_recipients: to.split(',').map((s) => s.trim()).filter(Boolean),
      });
      setDraftId(res.id);
    } catch {
      /* ignore */
    } finally {
      setSaving(false);
    }
  };

  const sendDraft = async () => {
    if (!draftId) return;
    try {
      await api.post(`/api/emails/${draftId}/send`);
      onSent();
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl dark:bg-gray-950">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
            {replyToId ? 'Antwort entwerfen' : 'Neuer Entwurf'}
          </h3>
          <button onClick={onClose} className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="space-y-3">
          <input
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="An (kommagetrennt)..."
            className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-indigo-300 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
          />
          <input
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder="Betreff..."
            className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-indigo-300 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
          />
          <RichTextEditor
            content={body}
            onChange={setBody}
            editable
            minHeight="160px"
          />
        </div>

        <div className="mt-4 flex items-center justify-between">
          {draftId ? (
            <span className="text-xs text-green-600 dark:text-green-400">
              Entwurf gespeichert (ID: {draftId.slice(0, 8)}...)
            </span>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            <button
              onClick={saveDraft}
              disabled={saving || !to.trim() || !subject.trim()}
              className="rounded-lg bg-gray-100 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-40 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              {saving ? 'Speichert...' : 'Als Entwurf speichern'}
            </button>
            {draftId && (
              <button
                onClick={sendDraft}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
              >
                Senden
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------- CreateTaskFromEmail Listener ---------- */

interface CreateTaskEvent {
  emailId: string;
  subject: string | null;
  bodyPreview: string | null;
  fromAddress: string | null;
  triageId?: string;
}

interface ProjectListItem {
  id: string;
  name: string;
  board_columns?: { id: string; name: string; position: number }[];
}

/* ---------- Kalender-Kontext ---------- */

function CalendarContext({
  selectedTriage,
  emailSubject,
}: {
  selectedTriage: TriageItem | null | undefined;
  emailSubject: string | null;
}) {
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
    const end = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59).toISOString();
    api.get<CalendarEvent[]>(`/api/calendar/events?start=${start}&end=${end}&top=10`)
      .then((data) => { setEvents(data); setLoaded(true); })
      .catch(() => setLoaded(true));
  }, []);

  const createTimeBlocker = async () => {
    setCreating(true);
    try {
      const now = new Date();
      const start = new Date(now.getTime() + 60 * 60 * 1000);
      start.setMinutes(0, 0, 0);
      const endTime = new Date(start.getTime() + 60 * 60 * 1000);
      await api.post('/api/calendar/events', {
        subject: `Zeitblocker: ${emailSubject || 'Aufgabe'}`,
        start: start.toISOString(),
        end: endTime.toISOString(),
        show_as: 'busy',
      });
      const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
      const todayEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59).toISOString();
      const updated = await api.get<CalendarEvent[]>(`/api/calendar/events?start=${todayStart}&end=${todayEnd}&top=10`);
      setEvents(updated);
    } catch {
      /* ignore */
    } finally {
      setCreating(false);
    }
  };

  if (!loaded) return null;

  const upcomingEvents = events
    .filter((e) => e.start && new Date(e.start) > new Date())
    .sort((a, b) => new Date(a.start!).getTime() - new Date(b.start!).getTime())
    .slice(0, 3);

  const formatTime = (iso: string | null) => {
    if (!iso) return '';
    return new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  };

  return (
    <div className="mb-4 rounded-xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-900/60">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">
          Heutige Termine
        </h4>
        {selectedTriage?.triage_class === 'task' && (
          <button
            onClick={createTimeBlocker}
            disabled={creating}
            className="rounded-lg bg-violet-600 px-2.5 py-1 text-[10px] font-medium text-white transition-colors hover:bg-violet-700 disabled:opacity-50"
          >
            {creating ? 'Erstellt...' : 'Zeitblocker erstellen'}
          </button>
        )}
      </div>
      {upcomingEvents.length === 0 ? (
        <p className="text-xs text-gray-400 dark:text-gray-500 italic">Keine weiteren Termine heute</p>
      ) : (
        <div className="space-y-1">
          {upcomingEvents.map((ev) => (
            <div key={ev.id} className="flex items-center gap-2 text-xs">
              <span className="shrink-0 font-medium text-gray-700 dark:text-gray-300">
                {formatTime(ev.start)}
              </span>
              <span className="truncate text-gray-600 dark:text-gray-400">{ev.subject}</span>
              {ev.location && (
                <span className="shrink-0 text-gray-400 dark:text-gray-500">({ev.location})</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------- CreateTaskFromEmail Listener ---------- */

function CreateTaskFromEmailListener({ onTaskCreated }: { onTaskCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [eventData, setEventData] = useState<CreateTaskEvent | null>(null);
  const [projects, setProjects] = useState<ProjectListItem[]>([]);
  const [selectedProject, setSelectedProject] = useState('');
  const [selectedColumn, setSelectedColumn] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<CreateTaskEvent>).detail;
      setEventData(detail);
      setTitle(detail.subject || '');
      setDescription(
        `Erstellt aus E-Mail von ${detail.fromAddress || 'unbekannt'}\n\n${detail.bodyPreview || ''}`.trim(),
      );
      setOpen(true);

      api.get<ProjectListItem[]>('/api/projects').then((data) => {
        setProjects(data);
        if (data.length > 0) {
          setSelectedProject(data[0].id);
          const cols = data[0].board_columns || [];
          if (cols.length > 0) {
            const sorted = [...cols].sort((a, b) => a.position - b.position);
            setSelectedColumn(sorted[0].id);
          }
        }
      }).catch(() => {});
    };
    window.addEventListener('open-create-task-from-email', handler);
    return () => window.removeEventListener('open-create-task-from-email', handler);
  }, []);

  const currentProject = projects.find((p) => p.id === selectedProject);
  const columns = (currentProject?.board_columns || []).sort((a, b) => a.position - b.position);

  useEffect(() => {
    if (columns.length > 0 && !columns.find((c) => c.id === selectedColumn)) {
      setSelectedColumn(columns[0].id);
    }
  }, [selectedProject, columns, selectedColumn]);

  const create = async () => {
    if (!title.trim() || !selectedProject || !selectedColumn) return;
    setCreating(true);
    try {
      await api.post('/api/tasks', {
        title,
        description,
        project_id: selectedProject,
        board_column_id: selectedColumn,
        board_position: 0,
        email_message_id: eventData?.emailId || null,
      });
      if (eventData?.triageId) {
        await api.post(`/api/triage/${eventData.triageId}/act`).catch(() => {});
      }
      onTaskCreated();
      setOpen(false);
    } catch {
      /* ignore */
    } finally {
      setCreating(false);
    }
  };

  if (!open || !eventData) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl dark:bg-gray-950">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Task aus E-Mail erstellen</h3>
          <button
            onClick={() => setOpen(false)}
            className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Titel</label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-indigo-300 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Beschreibung</label>
            <RichTextEditor
              content={description}
              onChange={setDescription}
              editable
              minHeight="100px"
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Projekt</label>
              <select
                value={selectedProject}
                onChange={(e) => setSelectedProject(e.target.value)}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none dark:border-gray-700 dark:bg-gray-900 dark:text-white"
              >
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            </div>
            <div className="flex-1">
              <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Spalte</label>
              <select
                value={selectedColumn}
                onChange={(e) => setSelectedColumn(e.target.value)}
                className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none dark:border-gray-700 dark:bg-gray-900 dark:text-white"
              >
                {columns.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={() => setOpen(false)}
            className="rounded-lg bg-gray-100 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Abbrechen
          </button>
          <button
            onClick={create}
            disabled={creating || !title.trim() || !selectedProject}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {creating ? 'Erstellt...' : 'Task erstellen'}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---------- Teams Chat Panel ---------- */

interface TeamsChatSummary {
  id: string;
  topic: string | null;
  chatType: string | null;
  createdDateTime: string | null;
  lastMessage: {
    body: string;
    from: string | null;
    createdDateTime: string | null;
  } | null;
}

interface TeamsChatMessage {
  id: string;
  from: string | null;
  fromId: string | null;
  body: string;
  bodyContentType: string | null;
  createdDateTime: string | null;
  messageType: string | null;
}

function TeamsChatPanel({ hasBg }: { hasBg: boolean }) {
  const [chats, setChats] = useState<TeamsChatSummary[]>([]);
  const [messages, setMessages] = useState<TeamsChatMessage[]>([]);
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [msgLoading, setMsgLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api.get<TeamsChatSummary[]>('/api/teams/chats?top=30')
      .then(setChats)
      .catch(e => setError(e instanceof Error ? e.message : 'Fehler beim Laden'))
      .finally(() => setLoading(false));
  }, []);

  const openChat = async (chatId: string) => {
    setSelectedChatId(chatId);
    setMsgLoading(true);
    try {
      const msgs = await api.get<TeamsChatMessage[]>(`/api/teams/chats/${chatId}/messages?top=30`);
      setMessages(msgs);
    } catch {
      setMessages([]);
    } finally {
      setMsgLoading(false);
    }
  };

  const fmtDate = (dateStr: string | null) => {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}.${d.getFullYear()}`;
  };

  const selectedChat = chats.find(c => c.id === selectedChatId);

  return (
    <div className="flex flex-1 overflow-hidden">
      {/* Chat-Liste */}
      <div className={`${selectedChatId ? 'hidden md:block md:w-[380px]' : 'w-full'} shrink-0 overflow-y-auto border-r ${hasBg ? 'border-white/10' : 'border-gray-200/60 dark:border-gray-800/60'}`}>
        {loading ? (
          <div className="flex h-full items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
          </div>
        ) : error ? (
          <div className="mx-4 mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-400">
            {error}
          </div>
        ) : chats.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-gray-400 dark:text-gray-600">
            <svg className="h-12 w-12" fill="none" viewBox="0 0 24 24" strokeWidth={1} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
            </svg>
            <span className="text-sm">Keine Teams-Chats</span>
          </div>
        ) : (
          <div className="space-y-2 p-3">
            {chats.map(chat => {
              const isSelected = chat.id === selectedChatId;
              const chatLabel = chat.topic || chat.lastMessage?.from || chat.chatType || 'Chat';
              const avatarColors = ['bg-violet-500', 'bg-blue-500', 'bg-teal-500', 'bg-rose-500', 'bg-amber-500'];
              const colorIdx = (chat.id || '').split('').reduce((a, c) => a + c.charCodeAt(0), 0) % avatarColors.length;
              const initials = chatLabel.split(/[\s.]+/).filter(Boolean).slice(0, 2).map(p => p[0]?.toUpperCase() || '').join('');

              return (
                <button
                  key={chat.id}
                  onClick={() => openChat(chat.id)}
                  className={`group flex w-full items-start gap-3 rounded-xl px-4 py-3 text-left transition-all ${
                    hasBg
                      ? isSelected
                        ? 'bg-black/40 shadow-lg backdrop-blur-xl ring-1 ring-white/20'
                        : 'bg-black/30 shadow-sm backdrop-blur-lg hover:bg-black/40'
                      : isSelected
                        ? 'bg-indigo-50/80 shadow-md ring-1 ring-indigo-200/50 dark:bg-indigo-950/40 dark:ring-indigo-800/50'
                        : 'bg-white/80 shadow-sm hover:bg-white hover:shadow-md dark:bg-gray-900/60 dark:hover:bg-gray-900/80'
                  }`}
                >
                  <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white ${avatarColors[colorIdx]}`}>
                    {initials}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className={`text-[13px] truncate ${hasBg ? 'font-semibold text-white drop-shadow' : 'font-semibold text-gray-900 dark:text-white'}`}>
                        {chatLabel}
                      </span>
                      <span className={`shrink-0 text-[10px] ${hasBg ? 'text-white/70' : 'text-gray-400 dark:text-gray-500'}`}>
                        {fmtDate(chat.lastMessage?.createdDateTime || chat.createdDateTime)}
                      </span>
                    </div>
                    {chat.chatType && (
                      <span className={`inline-block mt-0.5 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                        chat.chatType === 'group' ? 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300' : 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300'
                      }`}>
                        {chat.chatType === 'group' ? 'Gruppe' : chat.chatType === 'oneOnOne' ? '1:1' : chat.chatType}
                      </span>
                    )}
                    {chat.lastMessage?.body && (
                      <p className={`mt-0.5 truncate text-xs ${hasBg ? 'text-white/60' : 'text-gray-400 dark:text-gray-500'}`}>
                        {chat.lastMessage.from && <span className="font-medium">{chat.lastMessage.from}: </span>}
                        {chat.lastMessage.body.replace(/<[^>]+>/g, '').substring(0, 100)}
                      </p>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Chat-Detail / Nachrichten */}
      <div className={`${selectedChatId ? 'flex-1' : 'hidden md:flex md:flex-1'} flex flex-col overflow-hidden ${hasBg ? 'bg-black/20 backdrop-blur-sm' : 'bg-white/40 dark:bg-gray-900/20'}`}>
        {msgLoading ? (
          <div className="flex h-full items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
          </div>
        ) : selectedChatId && messages.length > 0 ? (
          <>
            <div className="flex items-center gap-3 border-b border-gray-200/60 px-4 py-3 dark:border-gray-800/60">
              <button
                onClick={() => setSelectedChatId(null)}
                className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 md:hidden"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M10.5 19.5 3 12m0 0 7.5-7.5M3 12h18" />
                </svg>
              </button>
              <h3 className="text-sm font-semibold text-gray-900 dark:text-white">
                {selectedChat?.topic || selectedChat?.lastMessage?.from || 'Chat'}
              </h3>
            </div>
            <div className="flex-1 overflow-y-auto p-4 space-y-3">
              {[...messages].reverse().map(msg => {
                if (msg.messageType === 'systemEventMessage') return null;
                return (
                  <div key={msg.id} className="flex gap-3">
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-violet-100 text-[10px] font-bold text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">
                      {(msg.from || '?').split(/[\s.]+/).slice(0, 2).map(p => p[0]?.toUpperCase() || '').join('')}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-gray-900 dark:text-white">{msg.from || 'Unbekannt'}</span>
                        <span className="text-[10px] text-gray-400 dark:text-gray-500">{fmtDate(msg.createdDateTime)}</span>
                      </div>
                      {msg.bodyContentType === 'html' ? (
                        <EmailBody html={msg.body} className="mt-0.5" />
                      ) : (
                        <p className="mt-0.5 text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{msg.body}</p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        ) : selectedChatId && messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-gray-400 dark:text-gray-600">
            <span className="text-sm">Keine Nachrichten in diesem Chat</span>
          </div>
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
            <svg className="h-16 w-16 text-gray-200 dark:text-gray-700" fill="none" viewBox="0 0 24 24" strokeWidth={0.8} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
            </svg>
            <p className="text-sm font-medium text-gray-400 dark:text-gray-500">Teams-Chat auswählen</p>
            <p className="text-xs text-gray-300 dark:text-gray-600">Klicke auf einen Chat, um Nachrichten anzuzeigen</p>
          </div>
        )}
      </div>
    </div>
  );
}
