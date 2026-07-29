import { useCallback, useEffect, useState } from 'react';
import { X, Save, AlertCircle, Check, RotateCcw, ShieldAlert } from 'lucide-react';
import { api } from '../../api/client';

interface MemoryFileFull {
  name: string;
  content: string;
  size: number;
  editable: boolean;
  truncated: boolean;
  hash: string | null;
}

function errMsg(e: unknown): { status: number | null; text: string } {
  if (e instanceof Error) {
    let text = e.message;
    try {
      const j = JSON.parse(e.message);
      if (j?.detail) text = String(j.detail);
    } catch { /* kein JSON */ }
    const status = (e as { status?: number }).status ?? null;
    return { status, text: text.slice(0, 300) };
  }
  return { status: null, text: 'Unbekannter Fehler' };
}

export function MemoryEditor({
  name, onClose, onSaved,
}: { name: string; onClose: () => void; onSaved: () => void }) {
  const [loaded, setLoaded] = useState<MemoryFileFull | null>(null);
  const [draft, setDraft] = useState('');
  const [baseHash, setBaseHash] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [agentBusy, setAgentBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setConflict(false);
    setSavedNote(null);
    try {
      const file = await api.get<MemoryFileFull>(`/api/memory/${encodeURIComponent(name)}`);
      setLoaded(file);
      setDraft(file.content);
      setBaseHash(file.hash);
    } catch (e) {
      setError(errMsg(e).text);
    } finally {
      setLoading(false);
    }
  }, [name]);

  useEffect(() => { load(); }, [load]);

  // Laeuft gerade ein Agent (running/queued)? Dann besteht Race-Gefahr beim
  // Schreiben. awaiting_approval zaehlt NICHT -- ein parkender HITL-Job schreibt
  // kein Memory, er wartet nur auf Freigabe.
  useEffect(() => {
    api.get<{ status: string }[]>('/api/agent-jobs')
      .then((jobs) => setAgentBusy(jobs.some((j) => j.status === 'running' || j.status === 'queued')))
      .catch(() => setAgentBusy(false));
  }, []);

  const dirty = loaded !== null && draft !== loaded.content;

  const handleSave = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    setError(null);
    setConflict(false);
    setSavedNote(null);
    try {
      const res = await api.put<{ name: string; size: number; hash: string; backup: string }>(
        `/api/memory/${encodeURIComponent(name)}`,
        { content: draft, base_hash: baseHash },
      );
      setBaseHash(res.hash);
      setLoaded((prev) => (prev ? { ...prev, content: draft, size: res.size, hash: res.hash } : prev));
      setSavedNote(`Gespeichert · Backup: ${res.backup}`);
      onSaved();
    } catch (e) {
      const { status, text } = errMsg(e);
      if (status === 409) {
        setConflict(true);
        setError(text);
      } else {
        setError(text);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm modal-safe" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-full w-full max-w-3xl lg:max-h-[90vh] flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl dark:border-gray-800 dark:bg-gray-900"
      >
        <div className="flex items-center justify-between border-b border-gray-100 p-4 dark:border-gray-800">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold text-gray-900 dark:text-white">{name} bearbeiten</h2>
            <p className="text-xs text-gray-500 dark:text-gray-400">Agent-Gedächtnis · jede Speicherung legt ein Backup an</p>
          </div>
          <button onClick={onClose} className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 space-y-3 overflow-y-auto p-4">
          {loading ? (
            <div className="flex h-48 items-center justify-center">
              <div className="h-8 w-8 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
            </div>
          ) : (
            <>
              {agentBusy && !conflict && (
                <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300">
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>Ein Agent arbeitet gerade. Er könnte das Gedächtnis gleichzeitig schreiben — speichere besser, wenn kein Job läuft. (Ein wartender Freigabe-Job ist unkritisch.)</span>
                </div>
              )}

              {loaded?.truncated && (
                <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>Datei zu gross zum sicheren Bearbeiten — Speichern deaktiviert, um Datenverlust zu vermeiden.</span>
                </div>
              )}

              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                disabled={loaded?.truncated}
                className="min-h-[440px] w-full resize-y rounded-xl border border-gray-300 bg-gray-50 p-4 font-mono text-[13px] leading-relaxed text-gray-800 focus:border-indigo-500 focus:bg-white focus:outline-none disabled:opacity-60 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
              />

              <div className="flex min-h-[20px] items-center justify-between text-xs">
                <span className="text-gray-400 dark:text-gray-500">{draft.length.toLocaleString('de-CH')} Zeichen · Markdown</span>
                {savedNote && (
                  <span className="flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
                    <Check className="h-3.5 w-3.5" /> {savedNote}
                  </span>
                )}
              </div>

              {error && (
                <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <span>{error}</span>
                    {conflict && (
                      <button onClick={load} className="ml-2 font-medium underline">Neu laden</button>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-gray-100 p-4 dark:border-gray-800">
          {dirty && !loaded?.truncated && (
            <button
              onClick={() => loaded && setDraft(loaded.content)}
              className="inline-flex items-center gap-1 rounded-lg px-3 py-2 text-sm font-medium text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
            >
              <RotateCcw className="h-4 w-4" /> Zurücksetzen
            </button>
          )}
          <button
            onClick={onClose}
            className="rounded-lg px-3.5 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
          >
            Schliessen
          </button>
          <button
            onClick={handleSave}
            disabled={!dirty || saving || loading || loaded?.truncated}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
          >
            <Save className="h-4 w-4" /> {saving ? 'Speichern…' : 'Speichern'}
          </button>
        </div>
      </div>
    </div>
  );
}
