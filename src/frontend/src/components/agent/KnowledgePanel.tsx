import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Pencil } from 'lucide-react';
import { api } from '../../api/client';
import { useSSE } from '../../hooks/useSSE';
import { Collapsible } from './Collapsible';
import { MemoryEditor } from './MemoryEditor';

interface LearningSignal {
  feedback_type: string;
  source?: string;
  reason?: string | null;
  sender_email?: string | null;
  created_at?: string | null;
}

interface LearningOverview {
  stats: {
    period_days: number;
    drafts_sent: number;
    drafts_edited: number;
    edit_rate: number;
    triage_reclass: number;
    episodes_total: number;
    episodes_corrected: number;
    rules_proposed: number;
    rules_active: number;
  };
  recent: LearningSignal[];
}

interface TriageStatsData {
  total: number;
  auto_reply: number;
  task: number;
  fyi: number;
  period_days: number;
  avg_per_day: number;
  reply_expected_count: number;
}

interface SenderProfile {
  email: string;
  name?: string | null;
  auto_reply_count: number;
  task_count: number;
  fyi_count: number;
  total_emails: number;
}

interface MemoryFile {
  name: string;
  content: string;
  size: number;
  editable?: boolean;
}

interface HeartbeatStatus {
  content: string;
  skills: string[];
}

const LEARN_SIGNAL_LABELS: Record<string, string> = {
  draft_edit: 'Entwurf editiert',
  approved_clean: 'Ohne Edit freigegeben',
  rejected: 'Entwurf abgelehnt',
  triage_reclass: 'Reklassifiziert',
  task_deleted: 'Aufgabe gelöscht',
  task_moved: 'Aufgabe verschoben',
  chat_teach: 'Im Chat gelernt',
};

export function KnowledgePanel() {
  const [loading, setLoading] = useState(true);
  const [learning, setLearning] = useState<LearningOverview | null>(null);
  const [triageStats, setTriageStats] = useState<TriageStatsData | null>(null);
  const [senderProfiles, setSenderProfiles] = useState<SenderProfile[]>([]);
  const [totalSenders, setTotalSenders] = useState(0);
  const [memFiles, setMemFiles] = useState<MemoryFile[]>([]);
  const [heartbeat, setHeartbeat] = useState<HeartbeatStatus | null>(null);
  const [editingMemory, setEditingMemory] = useState<string | null>(null);

  const reloadMemory = () =>
    api.get<MemoryFile[]>('/api/memory').then((mf) => setMemFiles(mf ?? [])).catch(() => {});

  const reloadLearning = useCallback(() => {
    api.get<LearningOverview>('/api/intelligence/learning?days=7')
      .then((lo) => setLearning(lo))
      .catch(() => {});
  }, []);
  useSSE((event) => { if (event === 'learned_rules_changed') reloadLearning(); });

  useEffect(() => {
    Promise.all([
      api.get<LearningOverview>('/api/intelligence/learning?days=7').catch(() => null),
      api.get<TriageStatsData>('/api/intelligence/triage-stats?days=30').catch(() => null),
      api.get<{ profiles: SenderProfile[]; total_senders: number }>('/api/intelligence/sender-profiles?limit=20').catch(() => ({ profiles: [], total_senders: 0 })),
      api.get<MemoryFile[]>('/api/memory').catch(() => []),
      api.get<HeartbeatStatus>('/api/memory/status/heartbeat').catch(() => null),
    ]).then(([lo, ts, sp, mf, hb]) => {
      setLearning(lo);
      setTriageStats(ts);
      setSenderProfiles(sp?.profiles ?? []);
      setTotalSenders(sp?.total_senders ?? 0);
      setMemFiles(mf ?? []);
      setHeartbeat(hb);
    }).finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center">
        <div className="h-9 w-9 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl">
      <div className="rounded-2xl border border-white/40 bg-white/70 p-5 shadow-sm backdrop-blur-sm sm:p-6 dark:border-gray-800 dark:bg-gray-900/70">
        <div className="mb-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Wissen</h2>
          <p className="mt-0.5 text-sm text-gray-500 dark:text-gray-400">
            Was der Agent gelernt hat — Lernfortschritt, Triage-Statistik, Absenderprofile und Gedächtnis.
          </p>
        </div>

        <div className="space-y-2.5">
          {learning && (
            <Collapsible
              title="Lernfortschritt"
              subtitle={`Letzte ${learning.stats.period_days} Tage · ${learning.stats.episodes_total} Episoden`}
              badge={<Badge tone="emerald">Lernend</Badge>}
              defaultOpen
            >
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat value={`${Math.round((1 - learning.stats.edit_rate) * 100)}%`} label="ohne Edit freigegeben" tone="emerald" />
                <Stat value={learning.stats.drafts_sent} label="Entwürfe versendet" tone="gray" />
                <Stat value={learning.stats.triage_reclass} label="Reklassifikationen" tone="amber" />
                <Stat value={learning.stats.rules_active} label="aktive Regeln" tone="indigo" />
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
                <span>{learning.stats.drafts_edited} editiert</span>
                <span>{learning.stats.episodes_corrected} korrigierte Episoden</span>
                {learning.stats.rules_proposed > 0 && <span>{learning.stats.rules_proposed} Regel-Vorschläge offen</span>}
              </div>
              {learning.recent.length > 0 && (
                <div className="mt-3 divide-y divide-gray-100 dark:divide-gray-700/60">
                  {learning.recent.map((sig, i) => (
                    <div key={i} className="flex items-center gap-2 py-1.5 text-xs">
                      <span className="rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                        {LEARN_SIGNAL_LABELS[sig.feedback_type] ?? sig.feedback_type}
                      </span>
                      {sig.source === 'outlook' && <Badge tone="blue">Outlook</Badge>}
                      <span className="min-w-0 flex-1 truncate text-gray-500 dark:text-gray-400">{sig.reason || sig.sender_email || ''}</span>
                      <span className="shrink-0 text-gray-400 dark:text-gray-500">
                        {sig.created_at ? new Date(sig.created_at).toLocaleDateString('de-CH', { day: '2-digit', month: '2-digit' }) : ''}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Collapsible>
          )}

          {triageStats && (
            <Collapsible
              title="Triage-Statistik"
              subtitle={`Letzte ${triageStats.period_days} Tage · ${triageStats.total} E-Mails`}
              defaultOpen
            >
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat value={triageStats.total} label="Gesamt" tone="gray" />
                <Stat value={triageStats.auto_reply} label="Auto-Reply" tone="emerald" />
                <Stat value={triageStats.task} label="Aufgaben" tone="blue" />
                <Stat value={triageStats.fyi} label="FYI" tone="gray" />
              </div>
              <div className="mt-3 flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
                <span>⌀ {triageStats.avg_per_day} E-Mails/Tag</span>
                <span>{triageStats.reply_expected_count} mit erwarteter Antwort</span>
              </div>
            </Collapsible>
          )}

          {senderProfiles.length > 0 && (
            <Collapsible title="Absenderprofile" subtitle={`${totalSenders} Absender bekannt`}>
              <div className="divide-y divide-gray-100 dark:divide-gray-700/60">
                {senderProfiles.map((sp) => (
                  <div key={sp.email} className="flex items-center gap-3 py-2.5">
                    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-xs font-semibold text-indigo-600 dark:bg-indigo-900/40 dark:text-indigo-300">
                      {(sp.name || sp.email).charAt(0).toUpperCase()}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-gray-900 dark:text-white">{sp.name || sp.email}</p>
                      <p className="truncate text-xs text-gray-400 dark:text-gray-500">{sp.email}</p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className="rounded bg-green-100 px-1.5 py-0.5 text-[10px] font-medium text-green-700 dark:bg-green-900/30 dark:text-green-300" title="Auto-Reply">{sp.auto_reply_count}</span>
                      <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-300" title="Aufgaben">{sp.task_count}</span>
                      <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-400" title="FYI">{sp.fyi_count}</span>
                      <span className="text-xs text-gray-400 dark:text-gray-500">{sp.total_emails}×</span>
                    </div>
                  </div>
                ))}
              </div>
            </Collapsible>
          )}

          {memFiles.map((file) => (
            <Collapsible
              key={file.name}
              title={file.name}
              badge={<Badge tone="gray">{file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`}</Badge>}
            >
              {file.editable && (
                <div className="mb-2 flex justify-end">
                  <button
                    onClick={() => setEditingMemory(file.name)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 transition-colors hover:border-indigo-300 hover:bg-indigo-50 hover:text-indigo-700 dark:border-gray-600 dark:text-gray-300 dark:hover:border-indigo-500/50 dark:hover:bg-indigo-900/20 dark:hover:text-indigo-300"
                  >
                    <Pencil className="h-3.5 w-3.5" /> Bearbeiten
                  </button>
                </div>
              )}
              <div className="max-h-80 overflow-y-auto whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-sm text-gray-800 dark:bg-gray-800 dark:text-gray-200">
                {file.content || <span className="italic text-gray-400">Leer</span>}
              </div>
            </Collapsible>
          ))}

          {heartbeat && (
            <Collapsible
              title="Heartbeat"
              badge={
                <span className="flex items-center gap-1.5 rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-700 dark:bg-green-900/40 dark:text-green-300">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-green-500" /> Aktiv
                </span>
              }
            >
              <div className="whitespace-pre-wrap text-sm text-gray-800 dark:text-gray-200">{heartbeat.content}</div>
              {heartbeat.skills.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {heartbeat.skills.map((skill) => (
                    <span key={skill} className="rounded-full bg-indigo-100 px-2.5 py-1 text-xs font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">{skill}</span>
                  ))}
                </div>
              )}
            </Collapsible>
          )}

          {!learning && !triageStats && senderProfiles.length === 0 && memFiles.length === 0 && !heartbeat && (
            <div className="rounded-xl border border-dashed border-gray-300 py-12 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
              Noch keine Daten — sobald der Agent arbeitet, erscheint hier sein Wissen.
            </div>
          )}
        </div>
      </div>

      {editingMemory && (
        <MemoryEditor
          name={editingMemory}
          onClose={() => setEditingMemory(null)}
          onSaved={reloadMemory}
        />
      )}
    </div>
  );
}

const TONES: Record<string, string> = {
  emerald: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-300',
  amber: 'bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300',
  indigo: 'bg-indigo-50 text-indigo-700 dark:bg-indigo-900/20 dark:text-indigo-300',
  blue: 'bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-300',
  gray: 'bg-gray-50 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
};

function Stat({ value, label, tone }: { value: string | number; label: string; tone: keyof typeof TONES }) {
  return (
    <div className={`rounded-lg p-3 text-center ${TONES[tone]}`}>
      <div className="text-2xl font-bold">{value}</div>
      <div className="text-xs opacity-80">{label}</div>
    </div>
  );
}

const BADGE_TONES: Record<string, string> = {
  emerald: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
  blue: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  gray: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400',
};

function Badge({ tone, children }: { tone: keyof typeof BADGE_TONES; children: ReactNode }) {
  return <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${BADGE_TONES[tone]}`}>{children}</span>;
}
