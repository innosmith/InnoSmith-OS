import { useState, useEffect, useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Video,
  ChevronDown,
  ChevronUp,
  Copy,
  Check,
  Download,
  FileDown,
  ShieldCheck,
  RefreshCw,
  Info,
} from 'lucide-react';
import { api, getToken } from '../../api/client';
import { ExportDialog } from '../ExportDialog';
import { MarkdownView } from '../MarkdownView';
import { useSSE } from '../../hooks/useSSE';

interface MeetingListItem {
  id: string;
  subject: string | null;
  organizer: string | null;
  started_at: string | null;
  ended_at: string | null;
  status: string;
  has_protocol: boolean;
  has_anonymized: boolean;
  transcript_chars: number;
  created_at: string;
}

interface MeetingDetail extends MeetingListItem {
  transcript_text: string | null;
  protocol_md: string | null;
  anonymized_text: string | null;
  anonymized_protocol_md: string | null;
  agent_job_id: string | null;
  error_message: string | null;
}

const STATUS_CONFIG: Record<string, { label: string; classes: string }> = {
  pending: { label: 'Wartend', classes: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300' },
  processing: { label: 'Wird analysiert', classes: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' },
  completed: { label: 'Protokoll bereit', classes: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300' },
  failed: { label: 'Fehler', classes: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' },
};

function formatMeetingTime(m: MeetingListItem): string {
  if (!m.started_at) return new Date(m.created_at).toLocaleDateString('de-CH');
  const start = new Date(m.started_at);
  const parts = [
    start.toLocaleDateString('de-CH', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }),
    start.toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit' }),
  ];
  if (m.ended_at) {
    const durMin = Math.round((new Date(m.ended_at).getTime() - start.getTime()) / 60000);
    if (durMin > 0) parts.push(`${durMin} Min`);
  }
  return parts.join(' · ');
}

async function downloadAuthed(path: string, filename: string): Promise<void> {
  const token = getToken();
  const resp = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** Meetings-Tab der Agenten-Seite: verarbeitete Teams-Meetings mit Protokoll,
 *  Transkript-Download, Clipboard-Copy und optionaler Anonymisierung. */
export function MeetingsPanel() {
  const [meetings, setMeetings] = useState<MeetingListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, MeetingDetail>>({});
  const [error, setError] = useState<string | null>(null);
  const [searchParams] = useSearchParams();
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const handledDeepLink = useRef<string | null>(null);

  const fetchMeetings = useCallback(async () => {
    try {
      const data = await api.get<MeetingListItem[]>('/api/meetings');
      setMeetings(data);
    } catch { /* handled */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchMeetings(); }, [fetchMeetings]);
  useSSE(event => { if (event === 'agent_jobs_changed') fetchMeetings(); });

  const loadDetail = useCallback(async (id: string) => {
    try {
      const detail = await api.get<MeetingDetail>(`/api/meetings/${id}`);
      setDetails(prev => ({ ...prev, [id]: detail }));
    } catch {
      setError('Meeting-Details konnten nicht geladen werden');
    }
  }, []);

  // Deep-Link (?meeting=<id>): betreffendes Meeting automatisch aufklappen + fokussieren.
  useEffect(() => {
    const target = searchParams.get('meeting');
    if (!target || loading || handledDeepLink.current === target) return;
    if (!meetings.some(m => m.id === target)) return;
    handledDeepLink.current = target;
    setExpandedId(target);
    loadDetail(target);
    requestAnimationFrame(() => {
      cardRefs.current[target]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }, [searchParams, loading, meetings, loadDetail]);

  const toggleExpand = (id: string) => {
    setExpandedId(prev => (prev === id ? null : id));
    if (!details[id]) loadDetail(id);
  };

  if (loading) {
    return (
      <div className="flex h-48 items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  if (meetings.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-xl border border-gray-200 bg-white/70 p-12 text-center backdrop-blur-sm dark:border-gray-800 dark:bg-gray-900/70">
        <Video className="mb-3 h-10 w-10 text-gray-300 dark:text-gray-600" />
        <p className="text-sm font-medium text-gray-700 dark:text-gray-300">Noch keine Meeting-Transkripte</p>
        <p className="mt-1 max-w-md text-xs text-gray-500 dark:text-gray-400">
          Beendete Teams-Meetings mit aktivierter Transkription werden automatisch geholt und
          als Protokoll aufbereitet. Voraussetzung ist das einmalige Admin-Setup
          (siehe docs/setup-teams-transkripte.md).
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200" role="alert">
          {error}
          <button type="button" onClick={() => setError(null)} className="ml-2 underline">Schliessen</button>
        </div>
      )}
      {meetings.map(m => {
        const cfg = STATUS_CONFIG[m.status] ?? { label: m.status, classes: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300' };
        const isExpanded = expandedId === m.id;
        const detail = details[m.id];
        return (
          <div
            key={m.id}
            ref={el => { cardRefs.current[m.id] = el; }}
            className="scroll-mt-4 rounded-xl border border-white/60 bg-white/70 p-4 shadow-sm backdrop-blur-sm transition-shadow hover:shadow-md dark:border-gray-800 dark:bg-gray-900/70"
          >
            <button
              type="button"
              onClick={() => toggleExpand(m.id)}
              className="flex w-full items-center gap-3 text-left"
            >
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-100/80 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">
                <Video className="h-4.5 w-4.5" />
              </div>
              <div className="min-w-0 flex-1">
                <h3 className="truncate text-sm font-semibold text-gray-900 dark:text-white">
                  {m.subject || 'Meeting ohne Betreff'}
                </h3>
                <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                  {formatMeetingTime(m)}
                  {m.organizer && ` · ${m.organizer}`}
                  {m.transcript_chars > 0 && ` · ${Math.round(m.transcript_chars / 1000)}k Zeichen`}
                </p>
              </div>
              <span className={`shrink-0 rounded-full px-2.5 py-0.5 text-[10px] font-medium leading-5 ${cfg.classes}`}>
                {cfg.label}
              </span>
              {m.has_anonymized && (
                <span className="hidden shrink-0 items-center gap-1 rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium leading-5 text-violet-700 sm:flex dark:bg-violet-900/40 dark:text-violet-300">
                  <ShieldCheck className="h-3 w-3" /> Anonymisiert
                </span>
              )}
              {isExpanded
                ? <ChevronUp className="h-4 w-4 shrink-0 text-gray-400" />
                : <ChevronDown className="h-4 w-4 shrink-0 text-gray-400" />}
            </button>

            {isExpanded && (
              <div className="mt-3 border-t border-gray-100 pt-3 dark:border-gray-800">
                {!detail ? (
                  <div className="flex h-16 items-center justify-center">
                    <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
                  </div>
                ) : (
                  <MeetingDetailView
                    detail={detail}
                    onChanged={(d) => setDetails(prev => ({ ...prev, [d.id]: d }))}
                    onReanalyzed={fetchMeetings}
                    onError={setError}
                  />
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function MeetingDetailView({
  detail, onChanged, onReanalyzed, onError,
}: {
  detail: MeetingDetail;
  onChanged: (d: MeetingDetail) => void;
  onReanalyzed: () => void;
  onError: (msg: string) => void;
}) {
  const [view, setView] = useState<'protokoll' | 'transkript'>(detail.has_protocol ? 'protokoll' : 'transkript');
  const [anonymized, setAnonymized] = useState(false);
  const [anonymizing, setAnonymizing] = useState(false);
  const [reanalyzing, setReanalyzing] = useState(false);
  const [copied, setCopied] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);

  const protocol = anonymized ? detail.anonymized_protocol_md : detail.protocol_md;
  const transcript = anonymized ? detail.anonymized_text : detail.transcript_text;
  const currentText = view === 'protokoll' ? protocol : transcript;

  const handleCopy = async () => {
    if (!currentText) return;
    try {
      await navigator.clipboard.writeText(currentText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* Clipboard nicht verfügbar */ }
  };

  const handleAnonymize = async () => {
    setAnonymizing(true);
    try {
      const updated = await api.post<MeetingDetail>(`/api/meetings/${detail.id}/anonymize`, {});
      onChanged(updated);
      setAnonymized(true);
    } catch (e) {
      onError(e instanceof Error ? e.message.slice(0, 200) : 'Anonymisierung fehlgeschlagen');
    } finally {
      setAnonymizing(false);
    }
  };

  const handleReanalyze = async () => {
    setReanalyzing(true);
    try {
      await api.post(`/api/meetings/${detail.id}/reanalyze`, {});
      onReanalyzed();
    } catch (e) {
      onError(e instanceof Error ? e.message.slice(0, 200) : 'Re-Analyse fehlgeschlagen');
    } finally {
      setReanalyzing(false);
    }
  };

  const handleDownload = async (kind: 'vtt' | 'txt') => {
    const stem = (detail.subject || 'meeting').replace(/[^\wäöüÄÖÜ -]/g, '_').slice(0, 60);
    try {
      if (kind === 'vtt') {
        await downloadAuthed(`/api/meetings/${detail.id}/transcript.vtt`, `${stem}.vtt`);
      } else {
        await downloadAuthed(
          `/api/meetings/${detail.id}/transcript.txt${anonymized ? '?anonymized=true' : ''}`,
          `${stem}${anonymized ? '_anonymisiert' : ''}.txt`,
        );
      }
    } catch (e) {
      onError(e instanceof Error ? e.message.slice(0, 200) : 'Download fehlgeschlagen');
    }
  };

  const handleMarkdownDownload = () => {
    if (!protocol) return;
    const stem = (detail.subject || 'meeting').replace(/[^\wäöüÄÖÜ -]/g, '_').slice(0, 60);
    const blob = new Blob([protocol], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${stem}${anonymized ? '_anonymisiert' : ''}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const btnClass = 'flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-800 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200';

  return (
    <div>
      {detail.error_message && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
          <span className="font-medium">Analyse-Fehler: </span>{detail.error_message}
        </div>
      )}

      {/* Ansicht-Umschalter + Aktionen */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="flex gap-1">
          {(['protokoll', 'transkript'] as const).map(v => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                view === v
                  ? 'bg-indigo-600 text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
              }`}
            >
              {v === 'protokoll' ? 'Protokoll' : 'Transkript'}
            </button>
          ))}
        </div>

        <div className="mx-1 h-5 w-px bg-gray-200 dark:bg-gray-700" />

        {/* Anonymisierungs-Schalter */}
        <label className="flex cursor-pointer items-center gap-1.5 text-xs font-medium text-gray-600 dark:text-gray-400">
          <input
            type="checkbox"
            checked={anonymized}
            disabled={!detail.has_anonymized}
            onChange={e => setAnonymized(e.target.checked)}
            className="h-3.5 w-3.5 rounded text-violet-600"
          />
          Anonymisierte Fassung
        </label>
        {!detail.has_anonymized && (
          <button onClick={handleAnonymize} disabled={anonymizing} className={`${btnClass} text-violet-600 dark:text-violet-400`}>
            <ShieldCheck className="h-3.5 w-3.5" />
            {anonymizing ? 'Anonymisiert…' : 'Anonymisieren'}
          </button>
        )}

        <div className="ml-auto flex flex-wrap items-center gap-1">
          <button onClick={handleCopy} disabled={!currentText} className={btnClass} title="In Zwischenablage kopieren">
            {copied ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : <Copy className="h-3.5 w-3.5" />}
            Kopieren
          </button>
          {view === 'transkript' ? (
            <>
              <button onClick={() => handleDownload('vtt')} className={btnClass} title="Original-WebVTT herunterladen">
                <Download className="h-3.5 w-3.5" /> VTT
              </button>
              <button onClick={() => handleDownload('txt')} className={btnClass} title="Klartext herunterladen">
                <Download className="h-3.5 w-3.5" /> Klartext
              </button>
            </>
          ) : (
            <>
              <button onClick={handleMarkdownDownload} disabled={!protocol} className={btnClass} title="Als Markdown herunterladen">
                <Download className="h-3.5 w-3.5" /> MD
              </button>
              <button onClick={() => setExportOpen(true)} disabled={!protocol} className={btnClass} title="Als DOCX/PDF exportieren">
                <FileDown className="h-3.5 w-3.5" /> DOCX/PDF
              </button>
            </>
          )}
          <button onClick={handleReanalyze} disabled={reanalyzing || !detail.transcript_text} className={btnClass} title="Protokoll neu erstellen (Original bleibt erhalten)">
            <RefreshCw className={`h-3.5 w-3.5 ${reanalyzing ? 'animate-spin' : ''}`} />
            Neu analysieren
          </button>
        </div>
      </div>

      {anonymized && (
        <div className="mb-3 flex items-start gap-2 rounded-lg bg-violet-50 px-3 py-2 dark:bg-violet-900/20">
          <Info className="mt-0.5 h-4 w-4 flex-shrink-0 text-violet-500" />
          <span className="text-xs text-violet-700 dark:text-violet-300">
            Die LLM-Anonymisierung ist gut, aber nicht garantiert lückenlos — vor dem Einfügen
            in ein öffentliches Modell kurz gegenlesen. Die Zuordnungstabelle bleibt lokal.
          </span>
        </div>
      )}

      {/* Inhalt */}
      {currentText ? (
        <div className="max-h-[32rem] overflow-y-auto rounded-lg bg-gray-50 p-4 text-sm text-gray-800 dark:bg-gray-800/60 dark:text-gray-200">
          <MarkdownView text={currentText} />
        </div>
      ) : (
        <p className="py-4 text-center text-xs italic text-gray-400 dark:text-gray-500">
          {anonymized
            ? 'Anonymisierte Fassung noch nicht erstellt'
            : view === 'protokoll'
              ? (detail.status === 'processing' ? 'Protokoll wird gerade erstellt…' : 'Noch kein Protokoll vorhanden')
              : 'Kein Transkript-Text vorhanden'}
        </p>
      )}

      {exportOpen && protocol && (
        <ExportDialog
          isOpen={exportOpen}
          onClose={() => setExportOpen(false)}
          rawContent={protocol}
        />
      )}
    </div>
  );
}
