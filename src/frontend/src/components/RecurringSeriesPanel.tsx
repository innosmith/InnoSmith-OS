import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';
import type { RecurringSeries } from '../types';

interface RecurringSeriesPanelProps {
  isOpen: boolean;
  onClose: () => void;
  /** Öffnet die Vorlage im Task-Detail — dort liegt der vollständige Editor. */
  onOpenTask: (taskId: string) => void;
}

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('de-CH', {
    weekday: 'short',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

function formatDateTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('de-CH', { weekday: 'short', day: '2-digit', month: 'short' })
    + ', ' + d.toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit' });
}

export function RecurringSeriesPanel({ isOpen, onClose, onOpenTask }: RecurringSeriesPanelProps) {
  const [series, setSeries] = useState<RecurringSeries[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const backdropRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSeries(await api.get<RecurringSeries[]>('/api/tasks/recurring'));
    } catch {
      setError('Die Serien konnten nicht geladen werden.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) load();
  }, [isOpen, load]);

  useEffect(() => {
    if (!isOpen) return;
    const handleEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      ref={backdropRef}
      className="fixed inset-0 z-50 flex justify-end bg-black/30 backdrop-blur-sm modal-safe"
      onClick={(e) => { if (e.target === backdropRef.current) onClose(); }}
    >
      <div className="flex h-full w-full max-w-xl flex-col overflow-hidden bg-white shadow-2xl ring-1 ring-gray-200 dark:bg-gray-950 dark:ring-gray-700/60">
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4 dark:border-gray-800">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Wiederkehrende Serien</h2>
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              Alle Vorlagen, aus denen automatisch Aufgaben entstehen
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
            aria-label="Schliessen"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex h-32 items-center justify-center">
              <div className="h-7 w-7 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
            </div>
          ) : error ? (
            <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-800/50 dark:bg-red-950/20 dark:text-red-300">
              {error}
            </div>
          ) : series.length === 0 ? (
            <div className="rounded-xl border border-dashed border-gray-200 px-4 py-8 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
              Noch keine wiederkehrenden Serien. Setze im Task-Detail eine Wiederholung, um eine Vorlage zu erstellen.
            </div>
          ) : (
            <ul className="space-y-2">
              {series.map((s) => (
                <li key={s.id}>
                  <button
                    onClick={() => { onOpenTask(s.id); onClose(); }}
                    className="w-full rounded-xl border border-gray-200 bg-white p-3.5 text-left transition-colors hover:border-indigo-300 hover:bg-indigo-50/40 dark:border-gray-800 dark:bg-gray-900/40 dark:hover:border-indigo-700 dark:hover:bg-indigo-950/20"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span
                            className="h-2 w-2 shrink-0 rounded-full"
                            style={{ backgroundColor: s.project_color || '#6B7280' }}
                          />
                          <span className="truncate text-sm font-medium text-gray-900 dark:text-white">
                            {s.title}
                          </span>
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-gray-500 dark:text-gray-400">
                          <span>{s.project_name}</span>
                          <span className="text-gray-300 dark:text-gray-700">·</span>
                          <span className={s.is_valid ? '' : 'font-medium text-red-600 dark:text-red-400'}>
                            {s.recurrence_description}
                          </span>
                          {s.assignee === 'agent' && (
                            <>
                              <span className="text-gray-300 dark:text-gray-700">·</span>
                              <span className="rounded-full bg-violet-100 px-1.5 py-0.5 font-medium text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">
                                Agent
                              </span>
                            </>
                          )}
                        </div>
                      </div>
                      <div className="shrink-0 text-right">
                        <div className="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">
                          Nächster Termin
                        </div>
                        <div className="text-xs font-medium text-gray-700 dark:text-gray-300">
                          {formatDateTime(s.next_occurrence)}
                        </div>
                      </div>
                    </div>

                    <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-gray-100 pt-2 text-[11px] text-gray-500 dark:border-gray-800 dark:text-gray-400">
                      <span>{s.instance_count} Instanzen erzeugt</span>
                      <span>Zuletzt: {formatDate(s.last_spawn)}</span>
                      {s.open_instance_id && (
                        <span className="rounded-full bg-amber-100 px-1.5 py-0.5 font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                          Offene Instanz {s.open_instance_due_date ? `(${formatDate(s.open_instance_due_date)})` : ''}
                        </span>
                      )}
                      {s.recurrence_end_date && <span>Endet am {formatDate(s.recurrence_end_date)}</span>}
                      {s.recurrence_max_instances && <span>Max. {s.recurrence_max_instances}</span>}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="border-t border-gray-200 px-6 py-3 text-[11px] text-gray-500 dark:border-gray-800 dark:text-gray-400">
          Klick auf eine Serie öffnet die Vorlage — dort lässt sie sich ändern oder beenden.
        </div>
      </div>
    </div>
  );
}
