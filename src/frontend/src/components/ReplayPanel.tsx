import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';

interface EmailSummary {
  id: string;
  subject: string | null;
  from_address: string | null;
  from_name: string | null;
  received_at: string | null;
  is_read: boolean;
  body_preview: string | null;
  has_attachments: boolean;
}

interface FolderInfo {
  id: string;
  display_name: string;
  total_count: number;
  unread_count: number;
}

interface EmailListResponse {
  emails: EmailSummary[];
  total: number | null;
}

export function ReplayPanel({ onJobCreated }: { onJobCreated?: () => void }) {
  const [open, setOpen] = useState(false);
  const [folders, setFolders] = useState<FolderInfo[]>([]);
  const [selectedFolder, setSelectedFolder] = useState('inbox');
  const [searchQuery, setSearchQuery] = useState('');
  const [emails, setEmails] = useState<EmailSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [replayingIds, setReplayingIds] = useState<Set<string>>(new Set());
  const [batchCount, setBatchCount] = useState(5);
  const [batchRunning, setBatchRunning] = useState(false);

  useEffect(() => {
    if (!open) return;
    api.get<FolderInfo[]>('/api/emails/folders')
      .then(setFolders)
      .catch(() => {});
  }, [open]);

  const loadEmails = useCallback(async () => {
    setLoading(true);
    try {
      if (searchQuery.trim()) {
        const data = await api.get<EmailListResponse>(
          `/api/emails/search?q=${encodeURIComponent(searchQuery)}&top=20`
        );
        setEmails(data.emails);
      } else {
        const data = await api.get<EmailListResponse>(
          `/api/emails?folder=${encodeURIComponent(selectedFolder)}&top=20`
        );
        setEmails(data.emails);
      }
    } catch { /* */ }
    finally { setLoading(false); }
  }, [selectedFolder, searchQuery]);

  useEffect(() => {
    if (!open) return;
    loadEmails();
  }, [open, loadEmails]);

  const handleReplay = async (messageId: string) => {
    setReplayingIds(prev => new Set(prev).add(messageId));
    try {
      await api.post('/api/triage/replay', { message_id: messageId });
      onJobCreated?.();
    } catch { /* */ }
    finally {
      setReplayingIds(prev => {
        const n = new Set(prev);
        n.delete(messageId);
        return n;
      });
    }
  };

  const handleBatchReplay = async () => {
    setBatchRunning(true);
    try {
      await api.post('/api/triage/replay-batch', { count: batchCount });
      onJobCreated?.();
    } catch { /* */ }
    finally { setBatchRunning(false); }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-lg border border-indigo-200 bg-indigo-50 px-4 py-2 text-sm font-medium text-indigo-700 transition-colors hover:bg-indigo-100 dark:border-indigo-800 dark:bg-indigo-950/30 dark:text-indigo-300 dark:hover:bg-indigo-900/40"
      >
        <ReplayIcon className="h-4 w-4" />
        E-Mail Replay / Test
      </button>
    );
  }

  return (
    <div className="rounded-xl border border-indigo-200 bg-white p-4 shadow-sm dark:border-indigo-800/50 dark:bg-gray-900">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ReplayIcon className="h-4 w-4 text-indigo-600 dark:text-indigo-400" />
          <h3 className="text-sm font-semibold text-gray-900 dark:text-white">E-Mail Replay / Test</h3>
        </div>
        <button
          onClick={() => setOpen(false)}
          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
        >
          <CloseIcon className="h-4 w-4" />
        </button>
      </div>

      {/* Ordner + Suche */}
      <div className="mb-3 flex gap-2">
        <select
          value={selectedFolder}
          onChange={e => { setSelectedFolder(e.target.value); setSearchQuery(''); }}
          className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-1.5 text-xs text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
        >
          <option value="inbox">Posteingang</option>
          {folders.map(f => (
            <option key={f.id} value={f.id}>
              {f.display_name} ({f.total_count})
            </option>
          ))}
        </select>
        <div className="relative flex-1">
          <input
            type="text"
            placeholder="Suche über alle Ordner…"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') loadEmails(); }}
            className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-1.5 pl-8 text-xs text-gray-700 placeholder-gray-400 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:placeholder-gray-500"
          />
          <SearchIcon className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
        </div>
        <button
          onClick={loadEmails}
          className="rounded-lg bg-gray-100 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700"
        >
          Laden
        </button>
      </div>

      {/* E-Mail-Liste */}
      {/* Vier Spalten brauchen rund 690px. Gleichmässig verteilt bliebe mobil
          kaum Text lesbar, darum horizontal scrollen statt kürzen. */}
      <div className="max-h-64 overflow-x-auto overflow-y-auto rounded-lg border border-gray-100 dark:border-gray-800">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
          </div>
        ) : emails.length === 0 ? (
          <div className="py-8 text-center text-xs text-gray-400">Keine E-Mails gefunden</div>
        ) : (
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-gray-50 dark:bg-gray-800">
              <tr className="border-b border-gray-100 dark:border-gray-700">
                <th className="px-3 py-2 font-medium text-gray-500 dark:text-gray-400">Von</th>
                <th className="px-3 py-2 font-medium text-gray-500 dark:text-gray-400">Betreff</th>
                <th className="px-3 py-2 font-medium text-gray-500 dark:text-gray-400">Datum</th>
                <th className="w-24 px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {emails.map(email => (
                <tr key={email.id} className="border-b border-gray-50 hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-800/50">
                  <td className="px-3 py-2 text-gray-700 dark:text-gray-300">
                    <div className="max-w-[160px] truncate">{email.from_name || email.from_address || '—'}</div>
                  </td>
                  <td className="px-3 py-2 text-gray-900 dark:text-white">
                    <div className="max-w-[280px] truncate font-medium">{email.subject || '(kein Betreff)'}</div>
                    {email.body_preview && (
                      <div className="max-w-[280px] truncate text-gray-400 dark:text-gray-500">{email.body_preview}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-gray-500 dark:text-gray-400 whitespace-nowrap">
                    {email.received_at
                      ? new Date(email.received_at).toLocaleDateString('de-CH', { day: '2-digit', month: '2-digit', year: '2-digit' })
                      : '—'}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => handleReplay(email.id)}
                      disabled={replayingIds.has(email.id)}
                      className="rounded-lg bg-indigo-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                    >
                      {replayingIds.has(email.id) ? 'Läuft…' : 'Triagieren'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Batch-Replay */}
      <div className="mt-3 flex items-center gap-3 border-t border-gray-100 pt-3 dark:border-gray-800">
        <span className="text-xs text-gray-500 dark:text-gray-400">Batch-Replay:</span>
        <select
          value={batchCount}
          onChange={e => setBatchCount(Number(e.target.value))}
          className="rounded border border-gray-200 bg-gray-50 px-2 py-1 text-xs dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
        >
          {[3, 5, 10].map(n => (
            <option key={n} value={n}>Letzte {n}</option>
          ))}
        </select>
        <button
          onClick={handleBatchReplay}
          disabled={batchRunning}
          className="rounded-lg border border-indigo-200 px-3 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-50 disabled:opacity-50 dark:border-indigo-800 dark:text-indigo-300 dark:hover:bg-indigo-950/30"
        >
          {batchRunning ? 'Wird erstellt…' : 'Erneut triagieren'}
        </button>
      </div>
    </div>
  );
}

function ReplayIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182" />
    </svg>
  );
}

function SearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
    </svg>
  );
}

function CloseIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
    </svg>
  );
}
