import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api/client';
import { DraftEditor } from '../DraftEditor';
import { EmailBody } from '../EmailBody';
import { FormattedOutput } from '../FormattedOutput';
import { EmailThreadPanel } from '../EmailThreadPanel';
import { CrmBadge } from '../CrmBadge';
import { AgentRationale } from './AgentRationale';
import { ConfidenceBadge } from './ConfidenceBadge';
import { ContextSources, type ContextSource } from './ContextSources';
import { IntentPreview } from './IntentPreview';

/**
 * ApprovalCard — einheitliche Draft-Freigabe (Cockpit, Agent-Queue, Inbox).
 *
 * Buendelt die bislang dreifach duplizierte Logik: Draft-Vorschau laden,
 * Intent-Preview (kein Blind-Senden), Inhalt zeigen, bearbeiten, freigeben,
 * ablehnen, bewerten — plus lesbares "Warum" (Rationale + Trace) und Confidence.
 * Externe Kommunikation bleibt ausnahmslos HITL: gesendet wird erst nach
 * bewusster Freigabe durch den Menschen.
 */

interface DraftPreviewData {
  draft_id: string;
  subject: string | null;
  body_html: string | null;
  body_preview: string | null;
  to_recipients: string[];
  cc_recipients: string[];
  source_subject: string | null;
  source_from: string | null;
  conversation_id?: string | null;
}

interface ApprovalCardProps {
  jobId: string;
  meta: Record<string, unknown>;
  glassBg?: boolean;
  /** Sicherheit der Einschaetzung (0..1). */
  confidence?: number | null;
  /** Lesbares "Warum" (1 Satz). */
  rationale?: string | null;
  /** Roher Agent-Output als Fallback, falls kein Draft vorhanden. */
  output?: string | null;
  /** Nach Freigabe/Ablehnung/Versand aufrufen (Daten neu laden). */
  onResolved: () => void;
  /** Trace-Aufklapper anzeigen (Standard: true). */
  showTrace?: boolean;
}

export function ApprovalCard({
  jobId,
  meta,
  glassBg = false,
  confidence,
  rationale,
  output,
  onResolved,
  showTrace = true,
}: ApprovalCardProps) {
  const [preview, setPreview] = useState<DraftPreviewData | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [failed, setFailed] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [editing, setEditing] = useState(false);

  const fromAddress = (meta.from_address as string) || '';
  const fromName = (meta.from_name as string) || '';
  const sourceSubject = (meta.subject as string) || '';
  const metaConvId = (meta.conversation_id as string) || '';
  const contextSources = (meta.context_sources as ContextSource[] | undefined) || null;

  const loadPreview = useCallback(() => {
    setLoadingPreview(true);
    setFailed(false);
    api.get<DraftPreviewData>(`/api/agent-jobs/${jobId}/draft-preview`)
      .then(setPreview)
      .catch(() => setFailed(true))
      .finally(() => setLoadingPreview(false));
  }, [jobId]);

  useEffect(() => { loadPreview(); }, [loadPreview]);

  const approve = async () => {
    setProcessing(true);
    try {
      await api.patch(`/api/agent-jobs/${jobId}`, { status: 'completed' });
      onResolved();
    } catch { /* belassen */ }
    finally { setProcessing(false); }
  };

  const reject = async () => {
    setProcessing(true);
    try {
      await api.patch(`/api/agent-jobs/${jobId}`, { status: 'failed', error_message: 'Vom Benutzer abgelehnt' });
      onResolved();
    } catch { /* belassen */ }
    finally { setProcessing(false); }
  };

  const muted = glassBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500';

  if (editing && preview) {
    return (
      <div className={`rounded-lg border p-3 ${glassBg ? 'border-white/15 bg-white/5' : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800'}`}>
        <DraftEditor
          jobId={jobId}
          subject={preview.subject || ''}
          bodyHtml={preview.body_html || ''}
          toRecipients={preview.to_recipients || []}
          ccRecipients={preview.cc_recipients || []}
          glassBg={glassBg}
          onSaved={() => { setEditing(false); loadPreview(); }}
          onSentAfterEdit={() => { setEditing(false); onResolved(); }}
          onCancel={() => setEditing(false)}
        />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Lesbares Warum + Confidence */}
      {(rationale || confidence != null) && (
        <div className="flex items-start justify-between gap-2">
          <AgentRationale summary={rationale} jobId={showTrace ? jobId : null} glassBg={glassBg} />
          <ConfidenceBadge confidence={confidence} glassBg={glassBg} className="mt-0.5 shrink-0" />
        </div>
      )}

      {/* Antwort auf (Quell-Mail) */}
      {sourceSubject && (
        <div className={`rounded-lg px-3 py-2 text-xs ${glassBg ? 'bg-white/10' : 'bg-gray-50 dark:bg-gray-800/50'} ${muted}`}>
          <span className="font-medium">Antwort auf:</span> {sourceSubject}
          {(fromAddress || fromName) && <span> — von {fromName || fromAddress}</span>}
        </div>
      )}

      {fromAddress && (
        <CrmBadge emailAddress={fromAddress} senderName={fromName} glassBg={glassBg} compact onCreateContact={() => {}} />
      )}

      {/* Intent-Preview: kein Blind-Senden */}
      {preview && (
        <IntentPreview
          recipients={preview.to_recipients || []}
          subject={preview.subject}
          snippet={preview.body_html || preview.body_preview}
          glassBg={glassBg}
        />
      )}

      {loadingPreview && !preview && (
        <div className={`flex items-center gap-2 text-sm ${muted}`}>
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent opacity-50" />
          Entwurf wird geladen…
        </div>
      )}

      {/* Voller Entwurf */}
      {preview?.body_html && (
        <div className={`overflow-hidden rounded-lg border ${glassBg ? 'border-white/20 bg-white/5' : 'border-gray-200 bg-white dark:border-gray-600 dark:bg-gray-800'}`}>
          <div className={`border-b px-4 py-2 ${glassBg ? 'border-white/10' : 'border-gray-100 dark:border-gray-700'}`}>
            <span className={`text-xs font-medium ${muted}`}>E-Mail-Entwurf</span>
          </div>
          <div className="max-h-56 overflow-y-auto px-4 py-3">
            <EmailBody html={preview.body_html} glassBg={glassBg} />
          </div>
        </div>
      )}

      {!preview?.body_html && preview?.body_preview && (
        <div className={`rounded-lg border p-4 text-sm ${glassBg ? 'border-white/20 bg-white/5 text-white/90' : 'border-gray-200 bg-white text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100'}`}>
          <div className={`mb-1 text-xs font-medium ${muted}`}>E-Mail-Entwurf</div>
          {preview.body_preview}
        </div>
      )}

      {/* Herkunft des Fachkontexts — die Kundeneingrenzung passiert hier, beim
          Lesen, nicht als harter Suchfilter (der kostet Recall). */}
      <ContextSources sources={contextSources} glassBg={glassBg} />

      {!preview && failed && output && (
        <div className={`rounded-lg border p-3 ${glassBg ? 'border-white/20 bg-white/5' : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800'}`}>
          <div className={`mb-1 text-xs font-medium ${muted}`}>Kein Entwurf verfügbar — Agent-Output:</div>
          <div className={`max-h-40 overflow-y-auto text-sm ${glassBg ? 'text-white/80' : 'text-gray-600 dark:text-gray-300'}`}>
            <FormattedOutput output={output} />
          </div>
        </div>
      )}

      {/* Aktionen */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={approve}
          disabled={processing}
          className={`rounded-lg px-4 py-2 text-sm font-medium shadow-sm transition-colors disabled:opacity-50 ${
            glassBg ? 'bg-emerald-600/90 text-white hover:bg-emerald-600' : 'bg-emerald-600 text-white hover:bg-emerald-700'
          }`}
        >
          {processing ? 'Wird gesendet…' : 'Freigeben & Senden'}
        </button>
        {preview && (
          <button
            onClick={() => setEditing(true)}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              glassBg ? 'border border-white/20 bg-white/10 text-white hover:bg-white/20'
                : 'bg-indigo-50 text-indigo-700 hover:bg-indigo-100 dark:bg-indigo-900/20 dark:text-indigo-300 dark:hover:bg-indigo-900/40'
            }`}
          >
            Bearbeiten
          </button>
        )}
        <button
          onClick={reject}
          disabled={processing}
          className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:opacity-50 ${
            glassBg ? 'border border-red-400/30 bg-red-500/20 text-red-200 hover:bg-red-500/30'
              : 'border border-red-300 text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-900/20'
          }`}
        >
          Ablehnen
        </button>
      </div>

      {(preview?.conversation_id || metaConvId) && (
        <EmailThreadPanel conversationId={(preview?.conversation_id || metaConvId) as string} glassBg={glassBg} compact />
      )}
    </div>
  );
}
