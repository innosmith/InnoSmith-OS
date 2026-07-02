import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { ChevronDown, ChevronUp, FileText, ExternalLink } from 'lucide-react';
import { api } from '../api/client';
import { MarkdownView } from './MarkdownView';

interface MeetingDetail {
  id: string;
  subject: string | null;
  status: string;
  protocol_md: string | null;
  transcript_text: string | null;
}

interface MeetingSourcePanelProps {
  transcriptId: string;
  subject?: string | null;
  glassBg?: boolean;
}

/** Aufklappbare Protokoll-Vorschau für Task-Vorschläge aus Meeting-Transkripten.
 *  Muster analog zu EmailThreadPanel: lazy-load beim Öffnen, plus Deep-Link in
 *  die Meetings-Ansicht (Original-Transkript + vollständiges Protokoll). */
export function MeetingSourcePanel({ transcriptId, subject, glassBg = false }: MeetingSourcePanelProps) {
  const [detail, setDetail] = useState<MeetingDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open || detail) return;
    setLoading(true);
    setError(null);
    api.get<MeetingDetail>(`/api/meetings/${encodeURIComponent(transcriptId)}`)
      .then(setDetail)
      .catch(() => setError('Meeting-Protokoll aktuell nicht abrufbar.'))
      .finally(() => setLoading(false));
  }, [open, transcriptId, detail]);

  const borderClass = glassBg ? 'border-white/20' : 'border-gray-200 dark:border-gray-700';
  const bgClass = glassBg ? 'bg-white/5' : 'bg-gray-50 dark:bg-gray-800/50';
  const textMuted = glassBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500';
  const hoverBg = glassBg ? 'hover:bg-white/10' : 'hover:bg-gray-100 dark:hover:bg-gray-700/50';

  return (
    <div className={`mt-2 rounded-lg border ${borderClass} ${bgClass}`}>
      <button
        onClick={() => setOpen(!open)}
        className={`flex w-full items-center gap-2 px-3 py-2 text-xs font-medium ${textMuted} ${hoverBg} rounded-lg transition-colors`}
      >
        <FileText className="h-3.5 w-3.5" />
        Meeting-Protokoll
        <span className="ml-auto">
          {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        </span>
      </button>

      {open && (
        <div className={`border-t ${borderClass} px-3 py-2`}>
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className={`truncate text-xs font-medium ${textMuted}`}>
              {subject || detail?.subject || 'Meeting'}
            </span>
            <Link
              to={`/agenten?tab=meetings&meeting=${encodeURIComponent(transcriptId)}`}
              className={`inline-flex shrink-0 items-center gap-1 text-xs font-medium ${
                glassBg ? 'text-sky-200 hover:text-sky-100' : 'text-sky-600 hover:text-sky-700 dark:text-sky-400'
              }`}
            >
              In Meetings öffnen
              <ExternalLink className="h-3 w-3" />
            </Link>
          </div>

          {loading && (
            <div className={`flex items-center gap-2 py-3 text-xs ${textMuted}`}>
              <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-transparent" />
              Protokoll wird geladen…
            </div>
          )}

          {error && <div className={`py-2 text-xs ${textMuted}`}>{error}</div>}

          {!loading && !error && detail && (
            detail.protocol_md ? (
              <div className="max-h-72 overflow-y-auto rounded-md bg-white/60 p-3 text-sm text-gray-800 dark:bg-gray-900/40 dark:text-gray-200">
                <MarkdownView text={detail.protocol_md} />
              </div>
            ) : (
              <div className={`py-2 text-xs ${textMuted}`}>
                {detail.status === 'processing' ? 'Protokoll wird gerade erstellt…' : 'Noch kein Protokoll vorhanden.'}
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}
