import { useState, useCallback } from 'react';
import { api, getToken } from '../api/client';

interface DiffPair {
  original: string;
  fake: string;
  entity_type: string;
}

interface AnonymizeResult {
  session_id: string;
  anonymized_text: string;
  diff: DiffPair[];
}

interface AnonymizePanelProps {
  isOpen: boolean;
  onClose: () => void;
  onInsertText?: (text: string, sessionId: string) => void;
  initialText?: string;
}

const ENTITY_OPTIONS = [
  { id: 'PERSON', label: 'Personen', defaultOn: true },
  { id: 'ORG', label: 'Organisationen', defaultOn: true },
  { id: 'LOCATION', label: 'Orte', defaultOn: true },
  { id: 'EMAIL', label: 'E-Mail-Adressen', defaultOn: true },
  { id: 'PHONE', label: 'Telefonnummern', defaultOn: true },
  { id: 'IBAN', label: 'IBAN-Nummern', defaultOn: true },
];

const ENTITY_COLORS: Record<string, string> = {
  PERSON: 'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300',
  ORG: 'bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300',
  LOCATION: 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300',
  EMAIL: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
  PHONE: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  IBAN: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300',
  UNKNOWN: 'bg-gray-100 text-gray-800 dark:bg-gray-900/40 dark:text-gray-300',
};

export function AnonymizePanel({ isOpen, onClose, onInsertText, initialText = '' }: AnonymizePanelProps) {
  const [inputText, setInputText] = useState(initialText);
  const [entities, setEntities] = useState<string[]>(
    ENTITY_OPTIONS.filter(e => e.defaultOn).map(e => e.id)
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnonymizeResult | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [copied, setCopied] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const toggleEntity = (id: string) => {
    setEntities(prev =>
      prev.includes(id) ? prev.filter(e => e !== id) : [...prev, id]
    );
  };

  const handleAnonymize = useCallback(async () => {
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      let data: AnonymizeResult;

      if (uploadFile) {
        const formData = new FormData();
        formData.append('file', uploadFile);
        formData.append('entities', entities.join(','));
        data = await api.upload<AnonymizeResult>('/api/content/anonymize/file', formData);
      } else {
        if (!inputText.trim()) {
          setError('Bitte Text eingeben oder Datei hochladen.');
          setLoading(false);
          return;
        }
        data = await api.post<AnonymizeResult>('/api/content/anonymize', {
          text: inputText,
          entities,
        });
      }

      setResult(data);
    } catch (e) {
      setError((e as Error).message || 'Anonymisierung fehlgeschlagen');
    } finally {
      setLoading(false);
    }
  }, [inputText, entities, uploadFile]);

  const handleCopy = async () => {
    if (!result) return;
    await navigator.clipboard.writeText(result.anonymized_text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleInsert = () => {
    if (!result || !onInsertText) return;
    onInsertText(result.anonymized_text, result.session_id);
    onClose();
  };

  const handleDownloadKeys = async () => {
    if (!result?.session_id) return;
    setDownloading(true);
    try {
      const token = getToken();
      const resp = await fetch(`/api/content/mapping-keys/${result.session_id}/download`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!resp.ok) throw new Error('Download fehlgeschlagen');
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `mapping-keys-${result.session_id.slice(0, 8)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDownloading(false);
    }
  };

  const handleExportAnonymized = () => {
    if (!result) return;
    const blob = new Blob([result.anonymized_text], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'anonymized.md';
    a.click();
    URL.revokeObjectURL(url);
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 modal-safe" onClick={onClose}>
      <div
        className="flex max-h-full w-full max-w-4xl lg:max-h-[90vh] flex-col rounded-xl border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-800"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4 dark:border-gray-700">
          <div>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Text anonymisieren</h3>
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              Personenbezogene Daten werden durch realistische Fake-Namen ersetzt
            </p>
          </div>
          <button onClick={onClose} className="rounded-lg p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-300">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-6">
          {!result ? (
            /* Eingabe-Ansicht */
            <div className="space-y-4">
              {/* Text-Eingabe */}
              <div>
                <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Text oder Markdown</label>
                <textarea
                  value={inputText}
                  onChange={e => setInputText(e.target.value)}
                  rows={8}
                  className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                  placeholder="Sensitiven Text hier einfügen..."
                />
              </div>

              {/* Datei-Upload */}
              <div>
                <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Oder Datei hochladen</label>
                <input
                  type="file"
                  accept=".md,.txt,.docx,.pdf"
                  onChange={e => setUploadFile(e.target.files?.[0] || null)}
                  className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 file:mr-3 file:rounded file:border-0 file:bg-indigo-50 file:px-3 file:py-1 file:text-sm file:font-medium file:text-indigo-600 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                />
                {uploadFile && (
                  <p className="mt-1 text-xs text-gray-500">{uploadFile.name} ({(uploadFile.size / 1024).toFixed(0)} KB)</p>
                )}
              </div>

              {/* Entity-Auswahl */}
              <div>
                <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">Zu anonymisieren</label>
                <div className="flex flex-wrap gap-2">
                  {ENTITY_OPTIONS.map(opt => {
                    const active = entities.includes(opt.id);
                    return (
                      <button
                        key={opt.id}
                        onClick={() => toggleEntity(opt.id)}
                        className={`inline-flex cursor-pointer items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                          active
                            ? ENTITY_COLORS[opt.id]
                            : 'bg-gray-100 text-gray-400 opacity-60 dark:bg-gray-700 dark:text-gray-500'
                        }`}
                        title={active ? `${opt.label} nicht anonymisieren` : `${opt.label} anonymisieren`}
                      >
                        {opt.label}
                        {active ? (
                          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>
                        ) : (
                          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" /></svg>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>

            
            </div>
          ) : (
            /* Ergebnis-Ansicht */
            <div className="space-y-4">
              {/* Zuordnungstabelle */}
              {result.diff.length > 0 && (
                <div>
                  <h4 className="mb-2 text-sm font-medium text-gray-700 dark:text-gray-300">
                    Anonymisierte Einträge ({result.diff.length})
                  </h4>
                  <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-600">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-gray-50 dark:bg-gray-700/50">
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400">Original</th>
                          <th className="px-3 py-2 text-center text-xs font-medium text-gray-500 dark:text-gray-400"></th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400">Ersetzt durch</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 dark:text-gray-400">Typ</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-200 dark:divide-gray-600">
                        {result.diff.map((d, i) => (
                          <tr key={i} className="hover:bg-gray-50 dark:hover:bg-gray-700/30">
                            <td className="px-3 py-2">
                              <span className="rounded bg-red-100 px-1.5 py-0.5 text-red-800 dark:bg-red-900/30 dark:text-red-300">{d.original}</span>
                            </td>
                            <td className="px-2 py-2 text-center text-gray-400">&rarr;</td>
                            <td className="px-3 py-2">
                              <span className={`rounded px-1.5 py-0.5 ${ENTITY_COLORS[d.entity_type] || ENTITY_COLORS.UNKNOWN}`}>{d.fake}</span>
                            </td>
                            <td className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">{d.entity_type}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {result.diff.length === 0 && (
                <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-800 dark:border-green-800 dark:bg-green-900/20 dark:text-green-300">
                  Keine personenbezogenen Daten erkannt. Der Text ist bereits sicher.
                </div>
              )}

              {/* Anonymisierter Text */}
              <div>
                <h4 className="mb-2 text-sm font-medium text-gray-700 dark:text-gray-300">Anonymisierter Text</h4>
                <div className="max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-gray-50 p-3 font-mono text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-700/50 dark:text-gray-100">
                  <pre className="whitespace-pre-wrap">{result.anonymized_text}</pre>
                </div>
              </div>

              {/* Sicherheits-Hinweis */}
              <div className="flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 dark:bg-amber-900/20">
                <svg className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z" /></svg>
                <span className="text-xs text-amber-700 dark:text-amber-300">
                  Die Mapping-Keys werden für 2 Stunden im Backend gespeichert. Danach ist keine De-Anonymisierung mehr möglich, sofern die Keys nicht heruntergeladen wurden.
                </span>
              </div>

              {/* Aktionen */}
              <div className="flex flex-wrap gap-2">
                {onInsertText && (
                  <button
                    onClick={handleInsert}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
                  >
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" /></svg>
                    In Chat einfügen
                  </button>
                )}
                <button
                  onClick={handleCopy}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  {copied ? 'Kopiert!' : 'Kopieren'}
                </button>
                <button
                  onClick={handleExportAnonymized}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  Als .md speichern
                </button>
                <button
                  onClick={handleDownloadKeys}
                  disabled={downloading}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" /></svg>
                  {downloading ? 'Laden...' : 'Mapping-Keys sichern'}
                </button>
              </div>
            </div>
          )}

          {error && (
            <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-between border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          {result ? (
            <button
              onClick={() => { setResult(null); setError(null); }}
              className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              Zurück
            </button>
          ) : (
            <div />
          )}
          <div className="flex gap-2">
            <button onClick={onClose} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700">
              Schliessen
            </button>
            {!result && (
              <button
                onClick={handleAnonymize}
                disabled={loading || (!inputText.trim() && !uploadFile)}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {loading ? 'Anonymisiert...' : 'Anonymisieren'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
