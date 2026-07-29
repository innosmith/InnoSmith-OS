import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Activity,
  ClipboardCheck,
  Droplets,
  Users,
  Search,
  Sparkles,
  ShieldCheck,
  ShieldOff,
  Eye,
  Loader2,
  Trash2,
  Download,
  Copy,
  Check,
  FileText,
  ChevronDown,
  ChevronRight,
  ArrowLeft,
  type LucideIcon,
} from 'lucide-react';
import { api, getToken } from '../api/client';
import { ExportDialog } from '../components/ExportDialog';
import { groupModelsByProvider, ANALYSIS_PROVIDER_ORDER } from '../lib/modelOrdering';

// ── Typen ────────────────────────────────────────────────
interface AnalysisType {
  id: string;
  title: string;
  description: string;
  icon: string;
  sections: string[];
  capabilities: string[];
  default_anonymize: boolean;
  default_model_hint: string;
}

interface ModelInfo {
  id: string;
  name: string;
  type: 'local' | 'cloud';
  provider: string;
  capabilities: string[];
}

interface DiffPair {
  original: string;
  fake: string;
  entity_type: string;
}

interface PrepareResult {
  analysis_type: string;
  title: string;
  model: string;
  system_prompt: string;
  prompt: string;
  anonymized: boolean;
  session_id: string;
  diff: DiffPair[];
  snapshot_meta: Record<string, unknown>;
}

interface HistoryItem {
  id: string;
  analysis_type: string;
  title: string;
  model: string;
  anonymized: boolean;
  status: string;
  tokens: number | null;
  cost_usd: number | null;
  created_at: string;
  preview: string;
}

interface FinanceDocMeta {
  id: string;
  label: string;
  filename: string | null;
  text_chars: number;
  created_at: string | null;
}

const ICON_MAP: Record<string, LucideIcon> = {
  Activity,
  ClipboardCheck,
  Droplets,
  Users,
  Search,
};

const CAP_LABELS: Record<string, string> = {
  thinking: 'Thinking',
  deep_research: 'Deep Research',
  web_search: 'Web-Suche',
};

type Phase = 'idle' | 'preparing' | 'review' | 'running' | 'done' | 'error';

export function AnalysisPage() {
  const [types, setTypes] = useState<AnalysisType[]>([]);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [selectedTypeId, setSelectedTypeId] = useState<string>('');
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [anonymize, setAnonymize] = useState<boolean>(true);
  const [availableDocs, setAvailableDocs] = useState<FinanceDocMeta[]>([]);
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([]);

  const [phase, setPhase] = useState<Phase>('idle');
  const [prepared, setPrepared] = useState<PrepareResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [thinking, setThinking] = useState('');
  const [report, setReport] = useState('');
  const [statusText, setStatusText] = useState('');
  const [meta, setMeta] = useState<{ tokens?: number; cost_usd?: number | null; model?: string } | null>(null);
  const [showThinking, setShowThinking] = useState(false);
  const [showPrompt, setShowPrompt] = useState(true);
  const [showExport, setShowExport] = useState(false);
  const [copied, setCopied] = useState(false);

  const [history, setHistory] = useState<HistoryItem[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const reportEndRef = useRef<HTMLDivElement | null>(null);

  const selectedType = useMemo(
    () => types.find(t => t.id === selectedTypeId) || null,
    [types, selectedTypeId],
  );

  // Cloud-Modelle müssen die Capability-Anforderung des Typs erfüllen.
  // Lokale Modelle werden bewusst immer angezeigt (zuunterst), damit sie testbar sind —
  // auch wenn ihr Thinking-/Research-Flag nicht erkannt wird.
  const eligibleModels = useMemo(() => {
    if (!selectedType) return models;
    const req = selectedType.capabilities || [];
    return models.filter(
      m => m.type === 'local' || req.every(c => (m.capabilities || []).includes(c)),
    );
  }, [models, selectedType]);

  const isLocal = useMemo(() => {
    const m = models.find(x => x.id === selectedModel);
    return m?.type === 'local' || selectedModel.startsWith('ollama/');
  }, [models, selectedModel]);

  useEffect(() => {
    api.get<{ types: AnalysisType[] }>('/api/analysis/types')
      .then(d => setTypes(d.types || []))
      .catch(() => setError('Analyse-Typen konnten nicht geladen werden.'));
    api.get<{ local: ModelInfo[]; cloud: ModelInfo[] }>('/api/models')
      .then(d => setModels([...(d.cloud || []), ...(d.local || [])]))
      .catch(() => {});
    api.get<{ documents: FinanceDocMeta[] }>('/api/analysis/documents')
      .then(d => setAvailableDocs(d.documents || []))
      .catch(() => {});
    loadHistory();
  }, []);

  // Bei Modellwechsel Anonymisierung sinnvoll vorbelegen (Cloud=an, lokal=aus)
  useEffect(() => {
    setAnonymize(!isLocal);
  }, [isLocal]);

  const loadHistory = () => {
    api.get<{ items: HistoryItem[] }>('/api/analysis/history')
      .then(d => setHistory(d.items || []))
      .catch(() => {});
  };

  const resetRun = () => {
    setPrepared(null);
    setThinking('');
    setReport('');
    setStatusText('');
    setMeta(null);
    setError(null);
  };

  const toggleDoc = (id: string) =>
    setSelectedDocIds(ids => (ids.includes(id) ? ids.filter(x => x !== id) : [...ids, id]));

  const handleSelectType = (id: string) => {
    setSelectedTypeId(id);
    resetRun();
    setPhase('idle');
    const t = types.find(x => x.id === id);
    // Erstes passendes Modell vorbelegen
    if (t) {
      const req = t.capabilities || [];
      const elig = models.filter(m => req.every(c => (m.capabilities || []).includes(c)));
      const cloudFirst = elig.find(m => m.type === 'cloud') || elig[0];
      if (cloudFirst) setSelectedModel(cloudFirst.id);
      else setSelectedModel('');
    }
  };

  const handlePrepare = async () => {
    if (!selectedTypeId || !selectedModel) return;
    setPhase('preparing');
    setError(null);
    resetRun();
    try {
      const result = await api.post<PrepareResult>('/api/analysis/prepare', {
        analysis_type: selectedTypeId,
        model: selectedModel,
        anonymize,
        document_ids: selectedDocIds,
      });
      setPrepared(result);
      setShowPrompt(true);
      setPhase('review');
    } catch (e) {
      setError((e as Error).message || 'Vorbereitung fehlgeschlagen');
      setPhase('error');
    }
  };

  const handleRun = async () => {
    if (!prepared) return;
    setPhase('running');
    setThinking('');
    setReport('');
    setStatusText('');
    setMeta(null);
    setError(null);

    const controller = new AbortController();
    abortRef.current = controller;
    const token = getToken();

    try {
      const resp = await fetch('/api/analysis/run', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          analysis_type: prepared.analysis_type,
          model: prepared.model,
          prompt: prepared.prompt,
          system_prompt: prepared.system_prompt,
          anonymized: prepared.anonymized,
          session_id: prepared.session_id,
          snapshot_meta: prepared.snapshot_meta,
        }),
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error((await resp.text()) || `HTTP ${resp.status}`);

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let evt = '';
      let acc = '';
      let thinkAcc = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('event: ')) { evt = line.slice(7).trim(); continue; }
          if (!line.startsWith('data: ')) continue;
          let data: any;
          try { data = JSON.parse(line.slice(6)); } catch { continue; }
          if (evt === 'thinking') {
            thinkAcc += data.content || '';
            setThinking(thinkAcc);
            setShowThinking(true);
          } else if (evt === 'status') {
            setStatusText(data.content || '');
          } else if (evt === 'chunk') {
            acc += data.content || '';
            setReport(acc);
          } else if (evt === 'done') {
            setReport(data.content || acc);
            setThinking(data.thinking || thinkAcc);
            setMeta({ tokens: data.tokens, cost_usd: data.cost_usd, model: data.model });
            setStatusText('');
            setPhase('done');
            loadHistory();
          } else if (evt === 'error') {
            setError(data.error || 'Analyse fehlgeschlagen');
            setPhase('error');
            loadHistory();
          }
        }
      }
      if (phaseRef.current === 'running') setPhase('done');
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setError((e as Error).message || 'Analyse fehlgeschlagen');
        setPhase('error');
      }
    } finally {
      abortRef.current = null;
    }
  };

  // phase ref, damit der Stream-Loop den aktuellen Wert sieht
  const phaseRef = useRef<Phase>('idle');
  useEffect(() => { phaseRef.current = phase; }, [phase]);

  useEffect(() => {
    reportEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [report]);

  const handleStop = () => {
    abortRef.current?.abort();
    setPhase(prepared ? 'review' : 'idle');
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(report);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* clipboard nicht verfügbar */ }
  };

  const handleDownloadMarkdown = () => {
    const slug = (prepared?.analysis_type || selectedType?.id || 'analyse')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-');
    const stamp = new Date().toISOString().slice(0, 10);
    const blob = new Blob([report], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${slug}-${stamp}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const loadHistoryItem = async (id: string) => {
    try {
      const d = await api.get<any>(`/api/analysis/${id}`);
      setSelectedTypeId(d.analysis_type);
      setSelectedModel(d.model);
      setPrepared({
        analysis_type: d.analysis_type,
        title: d.title,
        model: d.model,
        system_prompt: '',
        prompt: d.prompt || '',
        anonymized: d.anonymized,
        session_id: '',
        diff: [],
        snapshot_meta: d.snapshot_meta || {},
      });
      setReport(d.report || '');
      setThinking(d.thinking || '');
      setMeta({ tokens: d.tokens, cost_usd: d.cost_usd, model: d.model });
      setShowPrompt(false);
      setPhase('done');
    } catch {
      setError('Analyse konnte nicht geladen werden.');
    }
  };

  const deleteHistoryItem = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await api.delete(`/api/analysis/${id}`);
      setHistory(h => h.filter(x => x.id !== id));
    } catch { /* ignore */ }
  };

  return (
    <div className="flex h-full flex-col gap-6 overflow-y-auto p-4 lg:p-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link
            to="/finanzen"
            className="mb-1 inline-flex min-h-11 items-center gap-1 text-xs font-medium text-gray-500 hover:text-indigo-600 lg:min-h-0 dark:text-gray-400 dark:hover:text-indigo-400"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Zurück zu Finanzen
          </Link>
          <h1 className="hidden items-center gap-2 text-2xl font-semibold lg:flex dark:text-white text-gray-900">
            <Sparkles className="h-6 w-6 text-indigo-500" />
            Finanzanalysen
          </h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            KI-gestützte Treuhand- und Finanzanalysen auf Basis deiner Live-Daten — anonymisiert und mit Prompt-Review.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_300px]">
        {/* Hauptspalte */}
        <div className="space-y-6">
          {/* Galerie */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {types.map(t => {
              const Icon = ICON_MAP[t.icon] || Activity;
              const active = t.id === selectedTypeId;
              const isDeep = (t.capabilities || []).includes('deep_research');
              return (
                <button
                  key={t.id}
                  onClick={() => handleSelectType(t.id)}
                  className={`flex flex-col items-start gap-2 rounded-xl border p-4 text-left transition-all ${
                    active
                      ? 'border-indigo-500 bg-indigo-50 ring-1 ring-indigo-500 dark:border-indigo-400 dark:bg-indigo-950/40'
                      : 'border-gray-200 bg-white hover:border-gray-300 hover:shadow-sm dark:border-gray-700 dark:bg-gray-900 dark:hover:border-gray-600'
                  }`}
                >
                  <div className="flex w-full items-center justify-between">
                    <Icon className={`h-5 w-5 ${active ? 'text-indigo-600 dark:text-indigo-400' : 'text-gray-400'}`} />
                    {isDeep && (
                      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                        Deep Research
                      </span>
                    )}
                  </div>
                  <div className="text-sm font-semibold text-gray-900 dark:text-white">{t.title}</div>
                  <div className="text-xs leading-snug text-gray-500 dark:text-gray-400">{t.description}</div>
                </button>
              );
            })}
          </div>

          {/* Konfiguration */}
          {selectedType && (
            <div className="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-end">
                <div className="flex-1">
                  <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">
                    Modell {selectedType.capabilities.length > 0 && (
                      <span className="text-gray-400">
                        (benötigt: {selectedType.capabilities.map(c => CAP_LABELS[c] || c).join(', ')})
                      </span>
                    )}
                  </label>
                  <select
                    value={selectedModel}
                    onChange={e => setSelectedModel(e.target.value)}
                    className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  >
                    {eligibleModels.length === 0 && <option value="">Kein passendes Modell aktiviert</option>}
                    {groupModelsByProvider(eligibleModels, ANALYSIS_PROVIDER_ORDER).map(group => (
                      <optgroup key={group.provider} label={group.label}>
                        {group.items.map(m => (
                          <option key={m.id} value={m.id}>{m.name}</option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                </div>

                <button
                  onClick={() => setAnonymize(a => !a)}
                  disabled={isLocal}
                  title={isLocal ? 'Lokales Modell: Daten verlassen das System nicht' : 'Anonymisierung umschalten'}
                  className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-colors ${
                    anonymize
                      ? 'border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'
                      : 'border-gray-300 bg-white text-gray-600 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300'
                  } ${isLocal ? 'opacity-60' : ''}`}
                >
                  {anonymize ? <ShieldCheck className="h-4 w-4" /> : <ShieldOff className="h-4 w-4" />}
                  {anonymize ? 'Anonymisiert' : 'Roh (nicht anonym)'}
                </button>

                <button
                  onClick={handlePrepare}
                  disabled={!selectedModel || phase === 'preparing' || phase === 'running'}
                  className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  {phase === 'preparing' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Eye className="h-4 w-4" />}
                  Prompt vorbereiten & prüfen
                </button>
              </div>
              {isLocal && (
                <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                  Lokales Modell: Rohdaten bleiben auf dem System, daher keine Anonymisierung nötig.
                </p>
              )}

              {availableDocs.length > 0 && (
                <div className="mt-4 border-t border-gray-200 pt-3 dark:border-gray-700">
                  <div className="mb-2 flex items-center gap-2 text-xs font-medium text-gray-600 dark:text-gray-400">
                    <FileText className="h-3.5 w-3.5" />
                    Belege einbeziehen (Jahresrechnung)
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {availableDocs.map(d => {
                      const on = selectedDocIds.includes(d.id);
                      return (
                        <button
                          key={d.id}
                          type="button"
                          onClick={() => toggleDoc(d.id)}
                          title={`${d.filename || ''} · ${d.text_chars.toLocaleString('de-CH')} Zeichen`}
                          className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                            on
                              ? 'border-indigo-400 bg-indigo-50 text-indigo-700 dark:border-indigo-600 dark:bg-indigo-950/40 dark:text-indigo-300'
                              : 'border-gray-300 bg-white text-gray-600 hover:border-gray-400 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300'
                          }`}
                        >
                          {on ? <Check className="h-3.5 w-3.5" /> : <FileText className="h-3.5 w-3.5" />}
                          {d.label}
                        </button>
                      );
                    })}
                  </div>
                  <p className="mt-2 text-[11px] text-gray-400 dark:text-gray-500">
                    Ausgewählte Belege werden dem Prompt als Kontext beigefügt und vor dem Versand mit-anonymisiert.
                    Verwaltung unter Einstellungen &rarr; Finanzanalysen.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Prompt-Review */}
          {prepared && (phase === 'review' || phase === 'running' || phase === 'done' || phase === 'error') && (
            <div className="rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900">
              <button
                onClick={() => setShowPrompt(s => !s)}
                className="flex w-full items-center justify-between px-4 py-3 text-left"
              >
                <span className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-white">
                  {showPrompt ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                  Prompt-Review
                  {prepared.anonymized ? (
                    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                      <ShieldCheck className="h-3 w-3" /> anonymisiert
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">
                      Rohdaten
                    </span>
                  )}
                </span>
                <span className="text-xs text-gray-400">{prepared.model}</span>
              </button>

              {showPrompt && (
                <div className="space-y-4 border-t border-gray-200 p-4 dark:border-gray-700">
                  {prepared.diff.length > 0 && (
                    <div>
                      <h4 className="mb-2 text-xs font-medium text-gray-600 dark:text-gray-400">
                        Anonymisierte Ersetzungen ({prepared.diff.length})
                      </h4>
                      <div className="max-h-40 overflow-y-auto rounded-lg border border-gray-200 dark:border-gray-600">
                        <table className="w-full text-xs">
                          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                            {prepared.diff.map((d, i) => (
                              <tr key={i}>
                                <td className="px-2 py-1">
                                  <span className="rounded bg-red-100 px-1.5 py-0.5 text-red-800 dark:bg-red-900/30 dark:text-red-300">{d.original}</span>
                                </td>
                                <td className="px-1 py-1 text-center text-gray-400">&rarr;</td>
                                <td className="px-2 py-1">
                                  <span className="rounded bg-blue-100 px-1.5 py-0.5 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300">{d.fake}</span>
                                </td>
                                <td className="px-2 py-1 text-gray-400">{d.entity_type}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {prepared.system_prompt && (
                    <div>
                      <h4 className="mb-2 text-xs font-medium text-gray-600 dark:text-gray-400">
                        System-Prompt (Rolle &amp; Regeln, geht ebenfalls an das Modell)
                      </h4>
                      <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg border border-gray-200 bg-gray-50 p-3 font-mono text-xs text-gray-800 dark:border-gray-600 dark:bg-gray-800/50 dark:text-gray-200">
                        {prepared.system_prompt}
                      </pre>
                      <p className="mt-1 text-[11px] text-gray-400 dark:text-gray-500">
                        Der System-Prompt ist bewusst neutral formuliert und enthält keinen Firmennamen.
                      </p>
                    </div>
                  )}

                  <div>
                    <h4 className="mb-2 text-xs font-medium text-gray-600 dark:text-gray-400">
                      Finaler Prompt (so geht er an das Modell)
                    </h4>
                    <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded-lg border border-gray-200 bg-gray-50 p-3 font-mono text-xs text-gray-800 dark:border-gray-600 dark:bg-gray-800/50 dark:text-gray-200">
                      {prepared.prompt}
                    </pre>
                  </div>

                  {phase === 'review' && (
                    <div className="flex justify-end gap-2">
                      <button
                        onClick={() => { setPrepared(null); setPhase('idle'); }}
                        className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                      >
                        Verwerfen
                      </button>
                      <button
                        onClick={handleRun}
                        className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
                      >
                        <Sparkles className="h-4 w-4" />
                        Analyse starten
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Lauf-Status / Report */}
          {(phase === 'running' || phase === 'done' || (report && phase !== 'idle')) && (
            <div className="rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900">
              <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3 dark:border-gray-700">
                <span className="flex items-center gap-2 text-sm font-semibold text-gray-900 dark:text-white">
                  {phase === 'running' && <Loader2 className="h-4 w-4 animate-spin text-indigo-500" />}
                  {prepared?.title || selectedType?.title || 'Report'}
                </span>
                <div className="flex items-center gap-2">
                  {meta && (
                    <span className="text-xs text-gray-400">
                      {meta.tokens ? `${meta.tokens} Tokens` : ''}
                      {meta.cost_usd ? ` · $${meta.cost_usd.toFixed(4)}` : ''}
                    </span>
                  )}
                  {phase === 'running' && (
                    <button onClick={handleStop} className="rounded-lg border border-gray-300 px-3 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                      Stoppen
                    </button>
                  )}
                  {phase === 'done' && report && (
                    <>
                      <button onClick={handleCopy} title="In die Zwischenablage kopieren" className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-3 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                        {copied ? <Check className="h-3.5 w-3.5 text-green-500" /> : <Copy className="h-3.5 w-3.5" />}
                        {copied ? 'Kopiert' : 'Kopieren'}
                      </button>
                      <button onClick={handleDownloadMarkdown} title="Als Markdown-Datei herunterladen" className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-3 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                        <FileText className="h-3.5 w-3.5" /> Markdown
                      </button>
                      <button onClick={() => setShowExport(true)} className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-3 py-1 text-xs font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800">
                        <Download className="h-3.5 w-3.5" /> Export
                      </button>
                    </>
                  )}
                </div>
              </div>

              <div className="p-4">
                {statusText && phase === 'running' && (
                  <div className="mb-3 text-xs italic text-gray-500 dark:text-gray-400">{statusText}</div>
                )}

                {thinking && (
                  <div className="mb-3">
                    <button
                      onClick={() => setShowThinking(s => !s)}
                      className="flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-700 dark:text-gray-400"
                    >
                      {showThinking ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                      Denkprozess
                    </button>
                    {showThinking && (
                      <pre className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-xs text-gray-500 dark:bg-gray-800/50 dark:text-gray-400">
                        {thinking}
                      </pre>
                    )}
                  </div>
                )}

                {report ? (
                  <div className="prose prose-sm max-w-none dark:prose-invert">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{report}</ReactMarkdown>
                  </div>
                ) : phase === 'running' ? (
                  <div className="flex items-center gap-2 text-sm text-gray-400">
                    <Loader2 className="h-4 w-4 animate-spin" /> Modell arbeitet...
                  </div>
                ) : null}
                <div ref={reportEndRef} />
              </div>
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300">
              {error}
            </div>
          )}
        </div>

        {/* History-Spalte */}
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Verlauf</h3>
          {history.length === 0 && (
            <p className="text-xs text-gray-400">Noch keine Analysen.</p>
          )}
          {history.map(h => (
            <div
              key={h.id}
              onClick={() => loadHistoryItem(h.id)}
              className="group cursor-pointer rounded-lg border border-gray-200 bg-white p-3 transition-colors hover:border-indigo-300 dark:border-gray-700 dark:bg-gray-900 dark:hover:border-indigo-700"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="text-xs font-semibold text-gray-900 dark:text-white">{h.title}</div>
                <button
                  onClick={(e) => deleteHistoryItem(h.id, e)}
                  className="opacity-0 transition-opacity group-hover:opacity-100"
                  title="Löschen"
                >
                  <Trash2 className="h-3.5 w-3.5 text-gray-400 hover:text-red-500" />
                </button>
              </div>
              <div className="mt-1 line-clamp-2 text-[11px] text-gray-500 dark:text-gray-400">{h.preview}</div>
              <div className="mt-1.5 flex items-center gap-2 text-[10px] text-gray-400">
                <span>{new Date(h.created_at).toLocaleDateString('de-CH')}</span>
                {h.anonymized && <ShieldCheck className="h-3 w-3 text-emerald-500" />}
                {h.status === 'failed' && <span className="text-red-500">Fehler</span>}
              </div>
            </div>
          ))}
        </div>
      </div>

      {showExport && report && (
        <ExportDialog
          isOpen={showExport}
          onClose={() => setShowExport(false)}
          rawContent={report}
        />
      )}
    </div>
  );
}

export default AnalysisPage;
