import { useState, useEffect, type ReactNode } from 'react';
import { api } from '../api/client';
import { ChevronDown, ChevronUp, Mail, MessageSquare } from 'lucide-react';
import { EmailBody } from './EmailBody';

interface ThreadMessage {
  id: string;
  subject: string | null;
  from_address: string | null;
  from_name: string | null;
  received_at: string | null;
  body_html: string | null;
  body_preview: string | null;
}

interface ThreadResponse {
  conversation_id: string;
  messages: ThreadMessage[];
}

interface EmailThreadPanelProps {
  conversationId: string;
  glassBg?: boolean;
  compact?: boolean;
  /** Ersetzt die Beschriftung «E-Mail-Thread» in der Kopfzeile. */
  title?: string;
  /** Zweite Kopfzeile, z. B. Betreff und Absender. */
  subtitle?: string;
  /** Rechts in der Kopfzeile, klappt beim Klick nicht auf/zu. */
  action?: ReactNode;
  /** Mehr Höhe für die Nachrichtenkörper, wenn breit platziert. */
  roomy?: boolean;
}

export function EmailThreadPanel({
  conversationId,
  glassBg = false,
  compact = false,
  title,
  subtitle,
  action,
  roomy = false,
}: EmailThreadPanelProps) {
  const [messages, setMessages] = useState<ThreadMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [expandedMsg, setExpandedMsg] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!open || messages.length > 0) return;
    setLoading(true);
    setError(null);
    api.get<ThreadResponse>(`/api/emails/thread/${encodeURIComponent(conversationId)}`)
      .then(data => {
        const ordered = data.messages.slice().reverse();
        setMessages(ordered);
        // In der breiten Platzierung ist Lesen der Zweck: die neueste Nachricht
        // direkt zeigen, statt einen zweiten Klick zu verlangen.
        if (roomy && ordered[0]) setExpandedMsg(new Set([ordered[0].id]));
      })
      .catch(() => setError('Thread aktuell nicht abrufbar (z. B. CC-only-Mail). Bitte direkt in Outlook öffnen.'))
      .finally(() => setLoading(false));
  }, [open, conversationId, messages.length, roomy]);

  const toggleMsg = (id: string) => {
    setExpandedMsg(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const formatDate = (iso: string | null) => {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleString('de-CH', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch { return iso; }
  };

  const borderClass = glassBg ? 'border-white/20' : 'border-gray-200 dark:border-gray-700';
  const bgClass = glassBg ? 'bg-white/5' : 'bg-gray-50 dark:bg-gray-800/50';
  const textPrimary = glassBg ? 'text-white' : 'text-gray-900 dark:text-gray-100';
  const textMuted = glassBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500';
  const hoverBg = glassBg ? 'hover:bg-white/10' : 'hover:bg-gray-100 dark:hover:bg-gray-700/50';

  return (
    <div className={`mt-2 rounded-lg border ${borderClass} ${bgClass}`}>
      <div className={`flex items-center gap-2 rounded-lg pr-2 ${hoverBg} transition-colors`}>
        <button
          onClick={() => setOpen(!open)}
          className={`flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left text-xs font-medium ${textMuted}`}
        >
          <MessageSquare className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 flex-1">
            <span className="flex items-center gap-1.5">
              {title ?? 'E-Mail-Thread'}
              {messages.length > 0 && (
                <span className={`inline-flex h-4 min-w-[16px] items-center justify-center rounded-full px-1 text-[10px] font-bold ${
                  glassBg ? 'bg-white/20 text-white/80' : 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
                }`}>
                  {messages.length}
                </span>
              )}
            </span>
            {subtitle && (
              <span className={`mt-0.5 block truncate text-[11px] font-normal ${textMuted}`}>
                {subtitle}
              </span>
            )}
          </span>
          <span className="ml-auto shrink-0 pl-2">
            {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
          </span>
        </button>
        {action && (
          <div onClick={(e) => e.stopPropagation()} className="shrink-0">
            {action}
          </div>
        )}
      </div>

      {open && (
        <div className={`border-t ${borderClass} px-3 py-2`}>
          {loading && (
            <div className={`flex items-center gap-2 py-3 text-xs ${textMuted}`}>
              <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-transparent" />
              Thread wird geladen…
            </div>
          )}

          {error && (
            <div className={`py-2 text-xs ${textMuted}`}>{error}</div>
          )}

          {!loading && !error && messages.length === 0 && (
            <div className={`py-2 text-xs ${textMuted}`}>Kein Thread gefunden.</div>
          )}

          {messages.length > 0 && (
            <div className="space-y-1.5 max-h-[28rem] overflow-y-auto">
              {messages.map((msg, idx) => {
                const isExpanded = expandedMsg.has(msg.id);
                const isLast = idx === messages.length - 1;
                return (
                  <div
                    key={msg.id}
                    className={`rounded-lg border ${borderClass} ${isLast && !compact ? bgClass : ''}`}
                  >
                    <button
                      onClick={() => toggleMsg(msg.id)}
                      className={`flex w-full items-center gap-2 px-3 py-2 text-left ${hoverBg} rounded-t-lg transition-colors`}
                    >
                      <Mail className={`h-3 w-3 shrink-0 ${textMuted}`} />
                      <div className="min-w-0 flex-1">
                        <span className={`text-xs font-medium truncate block ${textPrimary}`}>
                          {msg.from_name || msg.from_address || 'Unbekannt'}
                        </span>
                      </div>
                      <span className={`text-[10px] shrink-0 ${textMuted}`}>
                        {formatDate(msg.received_at)}
                      </span>
                      {isExpanded
                        ? <ChevronUp className={`h-3 w-3 shrink-0 ${textMuted}`} />
                        : <ChevronDown className={`h-3 w-3 shrink-0 ${textMuted}`} />
                      }
                    </button>

                    {!isExpanded && msg.body_preview && (
                      <div className={`px-3 pb-2 text-[11px] truncate ${textMuted}`}>
                        {msg.body_preview.substring(0, 120)}…
                      </div>
                    )}

                    {isExpanded && msg.body_html && (
                      <div className={`border-t ${borderClass} overflow-y-auto px-3 py-2 ${roomy ? 'max-h-[32rem]' : 'max-h-48'}`}>
                        <EmailBody html={msg.body_html} glassBg={glassBg} size={roomy ? 'sm' : 'xs'} />
                      </div>
                    )}

                    {isExpanded && !msg.body_html && msg.body_preview && (
                      <div className={`border-t ${borderClass} px-3 py-2 text-xs ${textPrimary}`}>
                        {msg.body_preview}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
