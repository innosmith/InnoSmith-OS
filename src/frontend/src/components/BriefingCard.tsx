import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Newspaper, ChevronDown, ChevronUp, Copy, Check } from 'lucide-react';
import { api } from '../api/client';
import { MarkdownView } from './MarkdownView';
import { useSSE } from '../hooks/useSSE';
import type { AgentJob } from '../types';

const BRIEFING_TYPES = ['daily_briefing', 'weekly_briefing', 'monthly_briefing'] as const;
type BriefingType = (typeof BRIEFING_TYPES)[number];

const TYPE_LABELS: Record<BriefingType, string> = {
  daily_briefing: 'Tag',
  weekly_briefing: 'Woche',
  monthly_briefing: 'Monat',
};

const TYPE_BADGES: Record<BriefingType, string> = {
  daily_briefing: 'bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300',
  weekly_briefing: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
  monthly_briefing: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
};

function isToday(iso: string): boolean {
  const d = new Date(iso);
  const now = new Date();
  return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
}

function formatCreatedAt(iso: string): string {
  return new Date(iso).toLocaleString('de-CH', {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

interface BriefingCardProps {
  cardClass: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  hasBg: boolean;
}

/**
 * Zeigt das jüngste Briefing (Tag/Woche/Monat) zuoberst im Cockpit.
 * Am Erstellungstag aufgeklappt, danach eine schmale Zeile; bei mehreren
 * aktuellen Briefings Umschalt-Chips pro Typ.
 */
export function BriefingCard({ cardClass, textPrimary, textSecondary, textMuted, hasBg }: BriefingCardProps) {
  const navigate = useNavigate();
  const [briefings, setBriefings] = useState<Partial<Record<BriefingType, AgentJob>>>({});
  const [selectedType, setSelectedType] = useState<BriefingType | null>(null);
  const [expanded, setExpanded] = useState<boolean | null>(null); // null = noch nicht initialisiert
  const [copied, setCopied] = useState(false);

  const fetchBriefings = useCallback(async () => {
    try {
      const jobs = await api.get<AgentJob[]>(
        '/api/agent-jobs?job_type=daily_briefing,weekly_briefing,monthly_briefing&status=completed&limit=12',
      );
      const latest: Partial<Record<BriefingType, AgentJob>> = {};
      for (const job of jobs) {
        const t = job.job_type as BriefingType;
        if (!BRIEFING_TYPES.includes(t) || !job.output) continue;
        if (!latest[t] || new Date(job.created_at) > new Date(latest[t]!.created_at)) {
          latest[t] = job;
        }
      }
      setBriefings(latest);

      // Jüngstes Briefing vorauswählen; am Erstellungstag aufklappen.
      const newest = Object.values(latest).sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      )[0];
      if (newest) {
        setSelectedType(prev => (prev && latest[prev] ? prev : (newest.job_type as BriefingType)));
        setExpanded(prev => (prev === null ? isToday(newest.created_at) : prev));
      }
    } catch { /* Briefings sind optional */ }
  }, []);

  useEffect(() => { fetchBriefings(); }, [fetchBriefings]);
  useSSE(event => { if (event === 'agent_jobs_changed') fetchBriefings(); });

  const available = BRIEFING_TYPES.filter(t => briefings[t]);
  if (available.length === 0 || !selectedType || !briefings[selectedType]) return null;

  const current = briefings[selectedType]!;
  const isOpen = expanded === true;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(current.output || '');
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* Clipboard nicht verfügbar */ }
  };

  return (
    <section className={`rounded-xl border ${cardClass} ${isOpen ? 'p-4' : 'px-4 py-2.5'}`}>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          className="flex min-w-0 flex-1 items-center gap-3 text-left"
        >
          <Newspaper className={`h-4.5 w-4.5 shrink-0 ${hasBg ? 'text-sky-300' : 'text-sky-500 dark:text-sky-400'}`} />
          <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
            Briefing
          </h2>
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${TYPE_BADGES[selectedType]}`}>
            {TYPE_LABELS[selectedType]}
          </span>
          <span className={`hidden truncate text-xs sm:block ${textMuted}`}>
            {formatCreatedAt(current.created_at)}
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-1.5">
          {isOpen && available.length > 1 && (
            <div className="flex gap-1">
              {available.map(t => (
                <button
                  key={t}
                  onClick={() => setSelectedType(t)}
                  className={`rounded-lg px-2.5 py-1 text-[11px] font-medium transition-colors ${
                    selectedType === t
                      ? 'bg-indigo-600 text-white'
                      : hasBg
                        ? 'bg-white/10 text-white/80 hover:bg-white/20'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                  }`}
                >
                  {TYPE_LABELS[t]}
                </button>
              ))}
            </div>
          )}
          {isOpen && (
            <button
              onClick={handleCopy}
              className={`rounded-lg p-1.5 transition-colors ${
                hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'
              }`}
              title="In Zwischenablage kopieren"
            >
              {copied ? <Check className="h-4 w-4 text-emerald-500" /> : <Copy className="h-4 w-4" />}
            </button>
          )}
          {isOpen && (
            <button
              onClick={() => navigate('/agenten?type=briefing')}
              className={`hidden text-xs font-medium sm:block ${hasBg ? 'text-white/60 hover:text-white' : 'text-indigo-600 hover:text-indigo-800 dark:text-indigo-400'}`}
            >
              Archiv →
            </button>
          )}
          <button
            type="button"
            onClick={() => setExpanded(v => !v)}
            className={`rounded p-1 ${hasBg ? 'hover:bg-white/10' : 'hover:bg-gray-100 dark:hover:bg-gray-800'}`}
          >
            {isOpen
              ? <ChevronUp className={`h-4 w-4 ${textMuted}`} />
              : <ChevronDown className={`h-4 w-4 ${textMuted}`} />}
          </button>
        </div>
      </div>

      {isOpen && (
        <div className={`mt-3 max-h-[28rem] overflow-y-auto rounded-lg p-3 text-sm ${
          hasBg ? 'bg-white/5' : 'bg-gray-50/70 dark:bg-gray-800/40'
        } ${textPrimary}`}>
          <MarkdownView text={current.output || ''} />
        </div>
      )}
    </section>
  );
}
