import { useState } from 'react';
import { FileText, Mail, Search } from 'lucide-react';

/**
 * ContextSources — zeigt, worauf sich ein Entwurf inhaltlich stützt.
 *
 * Die Kontext-Recherche (Pass 2a) durchsucht das gesamte Archiv bewusst ohne
 * harten Kundenfilter: ein Filter kostet Recall und bräuchte Metadaten, die der
 * Index nicht führt. Die Eingrenzung passiert stattdessen hier — vor der Freigabe
 * ist sichtbar, welche Dokumente und Mails eingeflossen sind. Taucht Material
 * eines anderen Mandats auf, fällt das beim Lesen auf, bevor die Mail rausgeht.
 */

export interface ContextSource {
  title: string;
  source_type: string;
  from?: string;
  date?: string;
  url?: string;
}

const TYPE_LABEL: Record<string, string> = {
  email: 'E-Mail',
  onedrive: 'Dokument',
  unbekannt: 'Quelle',
};

function SourceIcon({ type }: { type: string }) {
  if (type === 'email') return <Mail className="h-3 w-3 shrink-0" />;
  if (type === 'onedrive') return <FileText className="h-3 w-3 shrink-0" />;
  return <Search className="h-3 w-3 shrink-0" />;
}

export function ContextSources({
  sources,
  glassBg = false,
}: {
  sources: ContextSource[] | null | undefined;
  glassBg?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!sources || sources.length === 0) return null;

  const muted = glassBg ? 'text-white/50' : 'text-gray-400 dark:text-gray-500';
  const body = glassBg ? 'text-white/80' : 'text-gray-600 dark:text-gray-300';

  return (
    <div
      data-testid="approval-context-sources"
      className={`rounded-lg border px-3 py-2 ${
        glassBg
          ? 'border-white/15 bg-white/5'
          : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800/50'
      }`}
    >
      <button
        data-testid="approval-context-sources-toggle"
        onClick={() => setOpen(!open)}
        className={`flex w-full items-center gap-1.5 text-left text-xs font-medium ${muted}`}
      >
        <Search className="h-3 w-3" />
        Recherchierte Quellen ({sources.length})
        <span className="ml-auto opacity-60">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <ul className={`mt-2 space-y-1 text-[11px] ${body}`}>
          {sources.map((s, i) => (
            <li key={`${s.source_type}-${s.title}-${i}`} className="flex items-start gap-1.5">
              <span className="mt-0.5">
                <SourceIcon type={s.source_type} />
              </span>
              <span className="wrap-anywhere">
                {s.title}
                <span className={muted}>
                  {' '}
                  — {TYPE_LABEL[s.source_type] ?? s.source_type}
                  {s.from ? `, ${s.from}` : ''}
                  {s.date ? `, ${s.date}` : ''}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
