import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, getToken } from '../api/client';
import ReactMarkdown from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/tokyo-night-dark.css';
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkRehype from 'remark-rehype';
import rehypeStringify from 'rehype-stringify';
import { TaskDetailDialog } from '../components/TaskDetailDialog';
import { BackgroundPicker } from '../components/BackgroundPicker';
import { ExportDialog } from '../components/ExportDialog';
import { AnonymizePanel } from '../components/AnonymizePanel';
import { OneDrivePicker, type ContextSource } from '../components/OneDrivePicker';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { useSSE } from '../hooks/useSSE';
import { DEFAULT_PROVIDER_ORDER as PROVIDER_ORDER, PROVIDER_LABELS } from '../lib/modelOrdering';

let mermaidReady: Promise<typeof import('mermaid')> | null = null;
function getMermaid() {
  if (!mermaidReady) {
    mermaidReady = import('mermaid').then(m => {
      m.default.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' });
      return m;
    });
  }
  return mermaidReady;
}

function uuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

async function markdownToHtml(markdown: string): Promise<string> {
  const result = await unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkRehype)
    .use(rehypeStringify)
    .process(markdown);
  return String(result);
}

interface LlmModel {
  id: string;
  name: string;
  type: 'local' | 'cloud';
  provider: string;
  capabilities: string[];
}

interface Conversation {
  id: string;
  title: string | null;
  model: string;
  mode?: string;
  temperature?: number;
  total_tokens: number;
  total_cost_usd: number;
  created_at: string;
  updated_at: string;
  message_count?: number;
  last_message_preview?: string | null;
}

interface ToolTraceEntry {
  type: 'tool_start' | 'tool_event' | 'status';
  content: string;
  ts: number;
}

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  model?: string | null;
  tokens: number | null;
  cost_usd: number | null;
  reasoning_tokens?: number | null;
  thinking?: string | null;
  tool_trace?: ToolTraceEntry[] | null;
  tools_used?: string[] | null;
  elapsed_s?: number | null;
  citations: unknown[] | null;
  attachments?: { name: string; type: string }[];
  created_at: string;
  /** Agent-Job hinter dieser Antwort (Trace/Streaming). */
  job_id?: string | null;
}

type ChatMode = 'agent' | 'deep_research';

/** Angepinntes Kontext-Dokument der Konversation (bleibt über alle Turns sichtbar). */
interface ContextItem {
  id: string;
  name: string;
  source_type: string;
  char_count: number;
  pinned: boolean;
  created_at: string;
}

// Zwei Modi: der vereinheitlichte Agent (InnoPilot) deckt Chat, Tool-Nutzung,
// Web-Recherche und Code-Ausführung ab — lokal oder Cloud. Deep Research bleibt
// eigenständig (nur öffentliche Modelle, mehrstufige externe Recherche-API).
const MODES: { id: ChatMode; label: string; tooltip: string }[] = [
  { id: 'agent', label: 'Agent', tooltip: 'InnoPilot: plaudern, Aktionen (Kalender, E-Mail, CRM, Aufgaben), Web-Recherche und Code — lokal oder Cloud. /code für Sandbox-Code, /suche für Web.' },
  { id: 'deep_research', label: 'Deep Research', tooltip: 'Mehrstufige Recherche mit vielen Quellen (öffentliche Modelle) — dauert länger, geht tiefer' },
];

function MermaidBlock({ code }: { code: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string>('');
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const m = await getMermaid();
        const mermaid = m.default;
        const valid = await mermaid.parse(code, { suppressErrors: true });
        if (!valid || cancelled) { if (!valid) setError('Ungültige Mermaid-Syntax'); return; }
        const id = `mermaid-${Math.random().toString(36).slice(2, 9)}`;
        const { svg: rendered } = await mermaid.render(id, code);
        if (!cancelled) setSvg(rendered);
      } catch (err) {
        if (!cancelled) setError(String((err as Error)?.message || err));
      }
    })();
    return () => { cancelled = true; };
  }, [code]);
  if (error) return (
    <div className="my-3 rounded-lg border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/40">
      <div className="flex items-center gap-2 border-b border-amber-200 px-3 py-1.5 text-xs font-medium text-amber-700 dark:border-amber-800 dark:text-amber-400">
        <span>Diagramm konnte nicht gerendert werden</span>
      </div>
      <pre className="overflow-x-auto p-3 text-xs text-gray-700 dark:text-gray-300"><code>{code}</code></pre>
    </div>
  );
  return <div ref={containerRef} className="my-3 flex justify-center overflow-x-auto rounded-lg bg-gray-900/50 p-4" dangerouslySetInnerHTML={{ __html: svg }} />;
}

function CodeCopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="code-copy-btn"
      onClick={() => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); }}
      title="Kopieren"
    >
      {copied ? (
        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>
      ) : (
        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M15.666 3.888A2.25 2.25 0 0 0 13.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 0 1-.75.75H9.75a.75.75 0 0 1-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 0 1-2.25 2.25H6.75A2.25 2.25 0 0 1 4.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 0 1 1.927-.184" /></svg>
      )}
    </button>
  );
}

// Marker aus Code-/Agent-Sandbox-Ergebnissen:
// <!--tp-artifacts:<scope>:name1|name2--> mit scope = conv-<uuid> (persistent),
// run-<id> oder script-<id> (ephemer). Der volle Scope wird erfasst und direkt
// gegen den scope-basierten Artefakt-Endpoint geladen.
const ARTIFACT_MARKER_RE = /\n*<!--tp-artifacts:((?:conv|run|script)-[A-Za-z0-9_-]+):([^>]*)-->/;

function parseArtifacts(content: string): { scope: string | null; names: string[]; cleaned: string } {
  const m = content.match(ARTIFACT_MARKER_RE);
  if (!m) return { scope: null, names: [], cleaned: content };
  const scope = m[1];
  const names = m[2].split('|').map(s => s.trim()).filter(Boolean);
  const cleaned = content.replace(m[0], '').trimEnd();
  return { scope, names, cleaned };
}

const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
const HTML_EXT = ['html', 'htm'];

function ext(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

/** Rendert im Sandbox-Workspace erzeugte Artefakte: Bilder inline, HTML als
 * spielbare/interaktive Vorschau in einem sandboxed <iframe> (Null-Origin via
 * Blob-URL), alles andere als Download. Auth läuft per Bearer-Header, daher
 * werden die Dateien als Blob geladen (kein direkter <img src>/<iframe src>). */
const ARTIFACT_IFRAME_SANDBOX = 'allow-scripts allow-modals allow-popups allow-pointer-lock allow-downloads allow-forms';

function ArtifactViewer({ scope, names }: { scope: string; names: string[] }) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Name des im Vollbild-Overlay vergrösserten Artefakts (null = keins).
  const [expanded, setExpanded] = useState<string | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    const created: string[] = [];
    (async () => {
      for (const name of names) {
        try {
          const blob = await api.blob(`/api/code/artifacts/${encodeURIComponent(scope)}/${encodeURIComponent(name)}`);
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          created.push(url);
          setUrls(prev => ({ ...prev, [name]: url }));
        } catch (e) {
          if (!cancelled) setErrors(prev => ({ ...prev, [name]: (e as Error).message || 'Fehler' }));
        }
      }
    })();
    return () => { cancelled = true; created.forEach(u => URL.revokeObjectURL(u)); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, names.join('|')]);

  // Esc schliesst das vergrösserte Overlay (sofern nicht gerade echtes Vollbild aktiv).
  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.fullscreenElement) setExpanded(null);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [expanded]);

  // Echtes Browser-Vollbild der Artefakt-Bühne (für spielbare HTML-Artefakte/Games).
  const enterFullscreen = () => {
    const el = stageRef.current;
    if (el?.requestFullscreen) el.requestFullscreen().catch(() => { /* vom Browser abgelehnt */ });
  };

  if (!names.length) return null;

  const renderArtifact = (name: string, url: string, large: boolean) => {
    const e = ext(name);
    if (IMAGE_EXT.includes(e)) {
      return <img src={url} alt={name} className={large ? 'mx-auto max-h-full max-w-full rounded object-contain' : 'mx-auto max-h-[520px] max-w-full rounded'} />;
    }
    if (HTML_EXT.includes(e)) {
      return (
        <iframe
          src={url}
          title={name}
          sandbox={ARTIFACT_IFRAME_SANDBOX}
          allow="fullscreen; gamepad; autoplay"
          className={large ? 'h-full w-full border-0 bg-white' : 'h-[520px] w-full rounded border-0 bg-white'}
        />
      );
    }
    return (
      <p className="px-2 py-3 text-xs text-gray-500 dark:text-gray-400">
        Keine Inline-Vorschau — bitte herunterladen.
      </p>
    );
  };

  return (
    <div className="mt-3 space-y-3">
      {names.map(name => {
        const url = urls[name];
        const err = errors[name];
        const e = ext(name);
        const canExpand = IMAGE_EXT.includes(e) || HTML_EXT.includes(e);
        return (
          <div key={name} className="overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
            <div className="flex items-center justify-between gap-2 border-b border-gray-200 bg-gray-50 px-3 py-1.5 text-xs font-medium text-gray-600 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-300">
              <span className="flex items-center gap-1.5 truncate"><FileIcon className="h-3.5 w-3.5 shrink-0" />{name}</span>
              {url && (
                <div className="flex shrink-0 items-center gap-2">
                  {canExpand && (
                    <button onClick={() => setExpanded(name)} className="flex items-center gap-1 text-indigo-600 hover:underline dark:text-indigo-400" title="Vergrössern / Vollbild">
                      <ExpandIcon className="h-3.5 w-3.5" /> Vergrössern
                    </button>
                  )}
                  {HTML_EXT.includes(e) && (
                    <a href={url} target="_blank" rel="noopener noreferrer" className="text-indigo-600 hover:underline dark:text-indigo-400">Neuer Tab</a>
                  )}
                  <a href={url} download={name} className="text-indigo-600 hover:underline dark:text-indigo-400">Download</a>
                </div>
              )}
            </div>
            <div className="bg-white p-2 dark:bg-gray-900">
              {err ? (
                <p className="px-2 py-3 text-xs text-red-500">Artefakt konnte nicht geladen werden: {err}</p>
              ) : !url ? (
                <p className="px-2 py-3 text-xs text-gray-400">Lädt…</p>
              ) : (
                renderArtifact(name, url, false)
              )}
            </div>
          </div>
        );
      })}

      {/* Vergrössertes Overlay: deckt die ganze Hauptfläche; «Vollbild» geht in
          echtes Browser-Fullscreen (z. B. um ein generiertes Game zu spielen). */}
      {expanded && urls[expanded] && (
        <div className="fixed inset-0 z-[60] flex flex-col bg-black/85 p-3 backdrop-blur-sm sm:p-4">
          <div className="mb-2 flex items-center justify-between gap-3 text-white">
            <span className="flex min-w-0 items-center gap-1.5 truncate text-sm font-medium">
              <FileIcon className="h-4 w-4 shrink-0" />{expanded}
            </span>
            <div className="flex shrink-0 items-center gap-3 text-xs sm:text-sm">
              <button onClick={enterFullscreen} className="flex items-center gap-1 rounded-md bg-white/10 px-2.5 py-1 font-medium hover:bg-white/20" title="Echtes Browser-Vollbild">
                <FullscreenIcon className="h-4 w-4" /> Vollbild
              </button>
              <a href={urls[expanded]} target="_blank" rel="noopener noreferrer" className="hover:underline">Neuer Tab</a>
              <a href={urls[expanded]} download={expanded} className="hover:underline">Download</a>
              <button onClick={() => setExpanded(null)} className="flex items-center gap-1 rounded-md bg-white/10 px-2.5 py-1 font-medium hover:bg-white/20" title="Schliessen (Esc)">
                <XIcon className="h-4 w-4" /> Schliessen
              </button>
            </div>
          </div>
          <div ref={stageRef} className="min-h-0 flex-1 overflow-auto rounded-lg bg-white">
            {renderArtifact(expanded, urls[expanded], true)}
          </div>
        </div>
      )}
    </div>
  );
}

const chatMdComponents: Partial<Components> = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
  ),
  table: ({ children }) => (
    <div className="table-wrapper">
      <table>{children}</table>
    </div>
  ),
  pre: ({ children, ...props }) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const codeEl = (children as any)?.props;
    const className = codeEl?.className || '';
    const langMatch = className.match(/language-(\S+)/);
    const lang = langMatch ? langMatch[1] : '';
    const text = typeof codeEl?.children === 'string' ? codeEl.children : '';
    if (lang === 'mermaid' && text) return <MermaidBlock code={text.trim()} />;
    return (
      <div className="code-block-wrapper">
        {lang && <span className="code-lang-label">{lang}</span>}
        <pre {...props}>{children}</pre>
        {text && <CodeCopyButton text={text} />}
      </div>
    );
  },
};

// Spiegelt extract_teach_intent (Backend): erkennt "merk dir ..."-Lehr-Absichten,
// damit das UI sofort einen dezenten Lern-Hinweis anzeigen kann.
const TEACH_TRIGGERS = [
  /\bmerk(?:e)?\s+dir\b/i,
  /\bmerk(?:e)?\s*:\s*/i,
  /\bnotier(?:e)?\s+dir\b/i,
  /\bpräg(?:e)?\s+dir\b.*?\bein\b/i,
  /\bvergiss\s+nicht\b/i,
  /\bbehalte\b.*?\bim\s+hinterkopf\b/i,
  /\b(?:für\s+die\s+zukunft|ab\s+jetzt|künftig|in\s+zukunft)\b/i,
  /\blern(?:e)?(?:\s+bitte)?[:,]?\s+\S/i,
];

function detectTeachIntent(text: string): boolean {
  if (!text || !text.trim()) return false;
  return TEACH_TRIGGERS.some((re) => re.test(text));
}

// Laufende Chat-Agent-Jobs überleben einen Reload: wir merken {jobId, offset}
// pro Konversation in localStorage und binden den SSE-Stream danach wieder an.
const AGENT_JOB_KEY = (convId: string) => `tp_chat_agent_${convId}`;

function persistAgentJob(convId: string, jobId: string, offset: number) {
  try {
    localStorage.setItem(AGENT_JOB_KEY(convId), JSON.stringify({ jobId, offset }));
  } catch { /* localStorage evtl. nicht verfügbar */ }
}

function readAgentJob(convId: string): { jobId: string; offset: number } | null {
  try {
    const raw = localStorage.getItem(AGENT_JOB_KEY(convId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.jobId === 'string') {
      return { jobId: parsed.jobId, offset: typeof parsed.offset === 'number' ? parsed.offset : 0 };
    }
  } catch { /* ignore */ }
  return null;
}

function clearAgentJob(convId: string) {
  try { localStorage.removeItem(AGENT_JOB_KEY(convId)); } catch { /* ignore */ }
}

export function ChatPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [models, setModels] = useState<LlmModel[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [temperature, setTemperature] = useState(0.7);
  const [mode, setMode] = useState<ChatMode>('agent');
  const [input, setInput] = useState('');
  // Optionale Standard-Eingabe (stdin) für den Code-Modus.
  const [codeStdin, setCodeStdin] = useState('');
  const [codeStdinOpen, setCodeStdinOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const isChatMobile = useMediaQuery('(max-width: 1023px)');
  // Verlauf-Sidebar: auf dem Desktop standardmässig aufgeklappt (wie Claude/
  // ChatGPT/Gemini), auf Mobile zugeklappt. Jederzeit manuell umschaltbar.
  const [showSidebar, setShowSidebar] = useState(() => !isChatMobile);
  const [modelOpen, setModelOpen] = useState(false);
  const [tempOpen, setTempOpen] = useState(false);
  const [attachments, setAttachments] = useState<File[]>([]);
  const [expandedThinking, setExpandedThinking] = useState<Set<string>>(new Set());
  const [mcpServers, setMcpServers] = useState<{ key: string; label: string; description: string }[]>([]);
  const [mcpOpen, setMcpOpen] = useState(false);
  // Grounding fuer Cloud-Modelle: Default-Deny. Nur explizit aktivierte
  // MCP-Server und optional Memory/USER-Profil werden an Cloud-Modelle gesendet.
  const [enabledServers, setEnabledServers] = useState<Set<string>>(new Set());
  const [includeMemory, setIncludeMemory] = useState(false);
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [copyMenuId, setCopyMenuId] = useState<string | null>(null);
  const [exportMsgId, setExportMsgId] = useState<string | null>(null);
  const [exportMsgContent, setExportMsgContent] = useState('');
  const [convertRawContent, setConvertRawContent] = useState<string | null>(null);
  const [convertSourceFile, setConvertSourceFile] = useState<File | null>(null);
  const [anonymizeOpen, setAnonymizeOpen] = useState(false);
  const [anonymizeInitialText, setAnonymizeInitialText] = useState('');
  const [anonymizeSessionIds, setAnonymizeSessionIds] = useState<Record<string, string>>({});
  const [deanonymizing, setDeanonymizing] = useState<string | null>(null);
  const [onedriveOpen, setOnedriveOpen] = useState(false);
  const [contextSources, setContextSources] = useState<ContextSource[]>([]);
  // Angepinnte Dokumente der aktiven Konversation (persistenter Kontext).
  const [contextItems, setContextItems] = useState<ContextItem[]>([]);

  // Per-Konversation Agent-Stream-State
  interface ClarifyRequest {
    clarify_id: string;
    question: string;
    choices: string[];
  }
  interface AgentStreamState {
    isStreaming: boolean;
    streamingContent: string;
    thinkingContent: string;
    toolTrace: ToolTraceEntry[];
    jobId: string | null;
    status: 'idle' | 'running' | 'done' | 'error';
    clarify?: ClarifyRequest | null;
  }
  const [agentStates, setAgentStates] = useState<Record<string, AgentStreamState>>({});

  const updateAgentState = useCallback((convId: string, patch: Partial<AgentStreamState>) => {
    setAgentStates(prev => ({
      ...prev,
      [convId]: { ...(prev[convId] || { isStreaming: false, streamingContent: '', thinkingContent: '', toolTrace: [], jobId: null, status: 'idle' }), ...patch },
    }));
  }, []);

  const activeAgent = activeId ? agentStates[activeId] : undefined;
  const isStreaming = activeAgent?.isStreaming ?? false;
  const streamingContent = activeAgent?.streamingContent ?? '';
  const thinkingContent = activeAgent?.thinkingContent ?? '';
  const toolTrace = activeAgent?.toolTrace ?? [];
  const clarifyRequest = activeAgent?.clarify ?? null;

  const [clarifyDraft, setClarifyDraft] = useState('');
  const answerClarify = useCallback(async (convId: string, clarifyId: string, answer: string) => {
    if (!answer.trim()) return;
    updateAgentState(convId, { clarify: null });
    setClarifyDraft('');
    try {
      await api.post(`/api/chat/conversations/${convId}/agent/clarify`, { clarify_id: clarifyId, answer });
    } catch {
      // Antwort konnte nicht zugestellt werden — Agent läuft nach Timeout eigenständig weiter.
    }
  }, [updateAgentState]);

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const modelDropdownRef = useRef<HTMLDivElement>(null);
  const mcpPopoverRef = useRef<HTMLDivElement>(null);
  const copyMenuRef = useRef<HTMLDivElement>(null);
  const skipNextFetchRef = useRef(false);
  const settingsInitRef = useRef(false);
  const sendingRef = useRef(false);

  const loadConversations = useCallback(async () => {
    try {
      const data = await api.get<{ items: Conversation[] }>('/api/chat/conversations');
      setConversations(data.items);
    } catch { /* */ }
    finally { setLoading(false); }
  }, []);

  const loadSettings = useCallback(() => {
    api.get<{ local: LlmModel[]; cloud: LlmModel[] }>('/api/models')
      .then(d => setModels([...d.local, ...d.cloud]))
      .catch(() => {});
    api.get<{ llm_default_model: string | null; llm_default_local_model: string | null; llm_default_temperature: number | null }>('/api/settings/llm')
      .then(s => {
        // Default-Modell/-Temperatur nur einmalig beim ersten Laden anwenden.
        // Der Focus-Refresh darf eine bewusste Modellwahl (z. B. Cloud-Modell)
        // NICHT ueberschreiben, sonst springt es nach jedem Task auf den Default.
        if (settingsInitRef.current) return;
        settingsInitRef.current = true;
        const defaultModel = s.llm_default_model || s.llm_default_local_model;
        if (defaultModel) setSelectedModel(defaultModel);
        if (s.llm_default_temperature !== null) setTemperature(s.llm_default_temperature);
      })
      .catch(() => {});
    api.get<{ servers: { key: string; label: string; description: string }[] }>('/api/chat/agent-tools')
      .then(d => setMcpServers(d.servers))
      .catch(() => {});
    api.get<{ chat_background_url: string | null }>('/api/settings')
      .then(s => setBgUrl(s.chat_background_url))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadConversations();
    loadSettings();
  }, [loadConversations, loadSettings]);

  useEffect(() => {
    const onFocus = () => loadSettings();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [loadSettings]);

  useEffect(() => {
    const convParam = searchParams.get('conv');
    if (convParam && !activeId) {
      setActiveId(convParam);
      setSearchParams({}, { replace: true });
    }
  }, [searchParams, activeId, setSearchParams]);

  const loadContextItems = useCallback(async (convId: string) => {
    try {
      const d = await api.get<{ items: ContextItem[] }>(`/api/chat/conversations/${convId}/context-items`);
      setContextItems(d.items || []);
    } catch { setContextItems([]); }
  }, []);

  const toggleContextItemPin = async (item: ContextItem) => {
    if (!activeId) return;
    try {
      await api.patch(`/api/chat/conversations/${activeId}/context-items/${item.id}`, { pinned: !item.pinned });
      setContextItems(prev => prev.map(ci => ci.id === item.id ? { ...ci, pinned: !item.pinned } : ci));
    } catch { /* */ }
  };

  const removeContextItem = async (item: ContextItem) => {
    if (!activeId) return;
    try {
      await api.delete(`/api/chat/conversations/${activeId}/context-items/${item.id}`);
      setContextItems(prev => prev.filter(ci => ci.id !== item.id));
    } catch { /* */ }
  };

  useEffect(() => {
    if (!activeId) { setMessages([]); setContextItems([]); return; }
    loadContextItems(activeId);
    if (skipNextFetchRef.current) { skipNextFetchRef.current = false; return; }
    api.get<{ messages: ChatMessage[]; mode?: string; model?: string; grounding?: { enabled_servers?: string[]; include_memory?: boolean } }>(`/api/chat/conversations/${activeId}`)
      .then(d => {
        setMessages(d.messages || []);
        // Nur noch zwei Modi: alles ausser Deep Research öffnet als Agent.
        setMode(d.mode === 'deep_research' ? 'deep_research' : 'agent');
        setEnabledServers(new Set(d.grounding?.enabled_servers || []));
        setIncludeMemory(Boolean(d.grounding?.include_memory));
        // Modell der Konversation wiederherstellen. Legacy-Platzhalter
        // ('hermes'/'nanobot') ignorieren, damit nicht 'InnoPilot' als Modell erscheint.
        if (d.model && d.model !== 'hermes' && d.model !== 'nanobot') {
          setSelectedModel(d.model);
        }
      })
      .catch(() => {});
  }, [activeId, loadContextItems]);

  const prevMsgCountRef = useRef(0);
  useEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) {
      bottomRef.current?.scrollIntoView({ behavior: 'instant' });
      return;
    }
    const threshold = 150;
    const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
    if (!isNearBottom) return;

    const isNewMessage = messages.length !== prevMsgCountRef.current;
    prevMsgCountRef.current = messages.length;

    bottomRef.current?.scrollIntoView({ behavior: isNewMessage ? 'smooth' : 'instant' });
  }, [messages, streamingContent, thinkingContent]);

  // Beim Wechsel des Breakpoints (Resize über die Grenze) den Default wieder
  // herstellen: Desktop aufgeklappt, Mobile zugeklappt. Manuelle Toggles
  // innerhalb desselben Breakpoints bleiben erhalten (Effekt feuert nur bei
  // tatsächlicher Änderung von isChatMobile).
  useEffect(() => {
    setShowSidebar(!isChatMobile);
  }, [isChatMobile]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (modelDropdownRef.current && !modelDropdownRef.current.contains(e.target as Node)) setModelOpen(false);
      if (mcpPopoverRef.current && !mcpPopoverRef.current.contains(e.target as Node)) setMcpOpen(false);
      if (copyMenuRef.current && !copyMenuRef.current.contains(e.target as Node)) setCopyMenuId(null);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const modelLabel = (id: string) => {
    if (id === 'nanobot' || id === 'hermes') return 'InnoPilot';
    const m = models.find(x => x.id === id);
    return m?.name || id.split('/').pop() || id;
  };

  const modelInfo = (id: string) => models.find(x => x.id === id);

  const filteredModels = () => {
    if (mode === 'deep_research') return models.filter(m => m.capabilities.includes('deep_research'));
    return models;
  };

  const groupedModels = () => {
    const source = filteredModels();
    const groups: { label: string; items: LlmModel[] }[] = [];
    for (const p of PROVIDER_ORDER) {
      const items = source.filter(m => m.provider === p);
      if (items.length > 0) groups.push({ label: PROVIDER_LABELS[p] || p, items });
    }
    const rest = source.filter(m => !PROVIDER_ORDER.includes(m.provider));
    if (rest.length > 0) groups.push({ label: 'Andere', items: rest });
    return groups;
  };

  // Früher wurden die Modell-Controls im Tavily-Websuche-Modus versteckt;
  // seit dessen Entfernung sind sie in allen Modi sichtbar.
  const showModelControls = true;

  const handleCodeExecute = async (taskDescription: string) => {
    let convId = activeId;

    if (!convId) {
      try {
        const conv = await api.post<Conversation>('/api/chat/conversations', {
          model: selectedModel, mode: 'agent',
        });
        setConversations(prev => [conv, ...prev]);
        convId = conv.id;
        skipNextFetchRef.current = true;
        setActiveId(conv.id);
      } catch (err) {
        setMessages(prev => [...prev, {
          id: uuid(), role: 'assistant',
          content: `Konversation konnte nicht erstellt werden: ${(err as Error).message}`,
          tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
        }]);
        return;
      }
    }

    updateAgentState(convId, { isStreaming: true, status: 'running', streamingContent: '*Verbinde mit LLM...*', thinkingContent: '' });
    const userMsg: ChatMessage = {
      id: uuid(), role: 'user', content: taskDescription,
      tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 900_000);

    try {
      const token = getToken();
      const url = `/api/code/conversations/${convId}/generate-and-execute`;
      console.log('[code-execute] fetch start', { url, convId, model: selectedModel, hasToken: !!token });
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({
          content: taskDescription,
          model: selectedModel,
          ...(codeStdin.trim() ? { stdin_data: codeStdin } : {}),
        }),
        signal: controller.signal,
      });
      console.log('[code-execute] fetch response', { status: resp.status, ok: resp.ok });
      if (!resp.ok) {
        const errText = await resp.text().catch(() => `HTTP ${resp.status}`);
        throw new Error(errText);
      }

      const reader = resp.body?.getReader();
      if (!reader) throw new Error('No response body');

      const decoder = new TextDecoder();
      let buffer = '';
      let generatedCode = '';
      let thinkAcc = '';
      let tokenAcc = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        let currentEvent = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim(); continue; }
          if (!line.startsWith('data: ')) { if (line === '') currentEvent = ''; continue; }
          const data = JSON.parse(line.slice(6));
          const evt = currentEvent;
          currentEvent = '';

          if (evt === 'thinking' || (data.content !== undefined && evt === 'thinking')) {
            thinkAcc += data.content || '';
            updateAgentState(convId!, {
              thinkingContent: thinkAcc,
              streamingContent: tokenAcc ? `*Generiert...*\n\n\`\`\`python\n${tokenAcc}\n\`\`\`` : '*Modell überlegt...*',
            });
          } else if (evt === 'token' || (data.content !== undefined && evt === 'token')) {
            tokenAcc += data.content || '';
            updateAgentState(convId!, {
              streamingContent: `*Generiert...*\n\n\`\`\`python\n${tokenAcc}\n\`\`\``,
              thinkingContent: thinkAcc,
            });
          } else if (data.code) {
            generatedCode = data.code;
            updateAgentState(convId!, {
              streamingContent: `**Generierter Code:**\n\`\`\`python\n${data.code}\n\`\`\`\n\n*Wird ausgeführt...*`,
              thinkingContent: thinkAcc,
            });
          } else if (data.phase === 'generating') {
            updateAgentState(convId!, { streamingContent: '*Code wird generiert...*' });
          } else if (data.phase === 'executing') {
            updateAgentState(convId!, {
              streamingContent: `**Generierter Code:**\n\`\`\`python\n${generatedCode}\n\`\`\`\n\n*Sandbox-Ausführung läuft...*`,
            });
          } else if (data.success !== undefined) {
            let content = `**Generierter Code:**\n\`\`\`python\n${generatedCode}\n\`\`\`\n\n`;
            if (data.success) {
              content += `**Ergebnis (${data.duration_seconds}s):**\n`;
              if (data.stdout) content += `\`\`\`\n${data.stdout}\n\`\`\`\n`;
              if (data.generated_files?.length > 0) {
                content += `\n**Erzeugte Dateien:** ${data.generated_files.map((f: { name: string }) => f.name).join(', ')}`;
              }
              if (data.warning) content += `\n\n_Hinweis: ${data.warning}_`;
              // Marker anhängen, damit die Artefakt-Vorschau (identisch zu neu geladenen
              // Nachrichten) gerendert werden kann.
              if (data.scope && data.generated_files?.length > 0) {
                const names = data.generated_files.map((f: { name: string }) => f.name).join('|');
                content += `\n\n<!--tp-artifacts:${data.scope}:${names}-->`;
              }
            } else {
              content += `**Fehler:**\n\`\`\`\n${data.stderr || data.error || 'Unbekannter Fehler'}\n\`\`\``;
            }
            const assistantMsg: ChatMessage = {
              id: uuid(), role: 'assistant', content,
              tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
              thinking: thinkAcc || null,
            };
            setMessages(prev => [...prev, assistantMsg]);
            updateAgentState(convId!, { isStreaming: false, status: 'done', streamingContent: '', thinkingContent: '' });
          } else if (data.error) {
            setMessages(prev => [...prev, {
              id: uuid(), role: 'assistant',
              content: `**Fehler:** ${data.error}`,
              tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
            }]);
            updateAgentState(convId!, { isStreaming: false, status: 'done', streamingContent: '', thinkingContent: '' });
          }
        }
      }
    } catch (err) {
      console.error('[code-execute] error', err);
      const msg = (err as Error).name === 'AbortError'
        ? 'Timeout: Keine Antwort innerhalb von 15 Minuten. Ist das LLM erreichbar bzw. blockiert ein Proxy den Stream (SSE)?'
        : `Code-Execution Fehler: ${(err as Error).message}`;
      setMessages(prev => [...prev, {
        id: uuid(), role: 'assistant',
        content: msg,
        tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
      }]);
    } finally {
      clearTimeout(timeoutId);
      updateAgentState(convId!, { isStreaming: false, status: 'done', streamingContent: '', thinkingContent: '' });
    }
  };

  // Legacy refs removed — agent state is now per-conversation in agentStates

  const processSSE = async (resp: Response, targetConvId?: string) => {
    const cid = targetConvId || activeId || '_sse';
    const reader = resp.body!.getReader();
    const decoder = new TextDecoder();
    let buf = '', acc = '', thinkAcc = '', evt = '';
    const traceAcc: ToolTraceEntry[] = [];

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      for (const line of lines) {
        if (line.startsWith('event: ')) { evt = line.slice(7).trim(); continue; }
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (evt === 'thinking') {
            thinkAcc += data.content || '';
            updateAgentState(cid, { thinkingContent: thinkAcc });
          } else if (evt === 'status') {
            traceAcc.push({ type: 'status', content: data.content || '', ts: Date.now() });
            updateAgentState(cid, { thinkingContent: data.content || '', toolTrace: [...traceAcc] });
          } else if (evt === 'tool_start') {
            traceAcc.push({ type: 'tool_start', content: data.tools || '', ts: Date.now() });
            updateAgentState(cid, { thinkingContent: `Tool: ${data.tools}`, toolTrace: [...traceAcc] });
          } else if (evt === 'tool_event') {
            traceAcc.push({ type: 'tool_event', content: typeof data === 'string' ? data : JSON.stringify(data), ts: Date.now() });
            updateAgentState(cid, { toolTrace: [...traceAcc] });
          } else if (evt === 'clarify') {
            updateAgentState(cid, { clarify: { clarify_id: data.clarify_id, question: data.question || '', choices: Array.isArray(data.choices) ? data.choices : [] } });
          } else if (evt === 'chunk') {
            acc += data.content || '';
            updateAgentState(cid, { streamingContent: acc });
          } else if (evt === 'done') {
            setMessages(prev => [...prev, {
              id: data.message_id || uuid(), role: 'assistant',
              content: data.content || acc, model: data.model || null,
              tokens: data.tokens, cost_usd: data.cost_usd || null,
              reasoning_tokens: data.reasoning_tokens || null,
              thinking: data.thinking || thinkAcc || null,
              tool_trace: traceAcc.length > 0 ? traceAcc : null,
              tools_used: data.tools_used || null,
              elapsed_s: data.elapsed_s || null,
              citations: data.citations || null, created_at: new Date().toISOString(),
            }]);
            updateAgentState(cid, { streamingContent: '', thinkingContent: '', toolTrace: [] });
          } else if (evt === 'error') {
            setMessages(prev => [...prev, {
              id: uuid(), role: 'assistant',
              content: `Fehler: ${data.error}`, tokens: null, cost_usd: null,
              tool_trace: traceAcc.length > 0 ? traceAcc : null,
              citations: null, created_at: new Date().toISOString(),
            }]);
            updateAgentState(cid, { streamingContent: '', thinkingContent: '', toolTrace: [] });
          } else if (evt === 'ping') {
            // Keepalive
          }
        } catch { /* */ }
      }
    }
  };

  const agentAbortControllers = useRef<Record<string, AbortController>>({});
  const agentOffsets = useRef<Record<string, number>>({});
  const agentAccumulators = useRef<Record<string, { stream: string; think: string; trace: ToolTraceEntry[] }>>({});
  // Infrastruktur fuer parallele, kontextwechsel-feste Agent-Laeufe.
  const retryTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const retryCounts = useRef<Record<string, number>>({});
  const activeReaders = useRef<Set<string>>(new Set());
  const agentStatesRef = useRef<Record<string, AgentStreamState>>({});
  const activeIdRef = useRef<string | null>(activeId);
  const MAX_STREAM_RETRIES = 5;

  // agentStates/activeId spiegeln, damit langlebige Callbacks nicht auf veraltete
  // Closures zugreifen (Ursache fuer Endlos-Retry mit stale State).
  useEffect(() => { agentStatesRef.current = agentStates; }, [agentStates]);
  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);

  // Prueft den Job-Status. Bei terminalem Zustand (completed/failed/verschwunden)
  // werden die Nachrichten aus der DB nachgeladen (nur wenn die Konversation sichtbar
  // ist) und der lokale Zustand + localStorage-Anker bereinigt.
  // Rueckgabe: true = terminal behandelt (kein Reconnect noetig).
  const reconcileAgentJob = useCallback(async (convId: string, jobId: string): Promise<boolean> => {
    let failed = false;
    try {
      const job = await api.get<{ status: string; output: string | null; error_message: string | null }>(`/api/agent-jobs/${jobId}`);
      if (job.status !== 'completed' && job.status !== 'failed') {
        return false; // laeuft noch -> Reconnect erlaubt
      }
      failed = job.status === 'failed';
    } catch (err) {
      const status = (err as { status?: number }).status;
      // Netzwerk-/Serverfehler (kein 404): nicht terminal, Reconnect weiter erlauben.
      if (status && status !== 404) return false;
      // 404: Job nicht mehr auffindbar -> wie terminal behandeln und aufraeumen.
    }
    // Nachrichten neu laden, aber nur wenn die Konversation gerade sichtbar ist,
    // damit wir nicht die Ansicht einer anderen Konversation ueberschreiben.
    if (activeIdRef.current === convId) {
      try {
        const d = await api.get<{ messages: ChatMessage[] }>(`/api/chat/conversations/${convId}`);
        setMessages(d.messages || []);
      } catch { /* ignore */ }
    }
    updateAgentState(convId, { isStreaming: false, streamingContent: '', thinkingContent: '', toolTrace: [], jobId: null, status: failed ? 'error' : 'done' });
    agentAbortControllers.current[convId]?.abort();
    delete agentAbortControllers.current[convId];
    delete agentAccumulators.current[convId];
    delete agentOffsets.current[convId];
    retryCounts.current[convId] = 0;
    const timer = retryTimers.current[convId];
    if (timer) { clearTimeout(timer); delete retryTimers.current[convId]; }
    clearAgentJob(convId);
    loadConversations();
    return true;
  }, [updateAgentState, loadConversations]);

  // Ref-Indirektion, um die zyklische Abhaengigkeit zwischen scheduleReconnect
  // und connectAgentStream aufzuloesen.
  const connectAgentStreamRef = useRef<(convId: string, jobId: string, offset: number) => void>(() => {});

  // Geplanter Reconnect mit Job-Status-Check und Max-Retry-Grenze. Registriert den
  // Timer in retryTimers, damit er beim Unmount sauber gecleart werden kann.
  const scheduleReconnect = useCallback((convId: string, jobId: string, delay: number) => {
    const prev = retryTimers.current[convId];
    if (prev) clearTimeout(prev);
    const count = retryCounts.current[convId] || 0;
    if (count >= MAX_STREAM_RETRIES) {
      // Zu viele Fehlversuche: ueber den Job-Status final aufraeumen.
      reconcileAgentJob(convId, jobId).then(handled => {
        if (!handled) updateAgentState(convId, { isStreaming: false, thinkingContent: '', status: 'idle' });
      });
      return;
    }
    retryCounts.current[convId] = count + 1;
    const t = setTimeout(async () => {
      delete retryTimers.current[convId];
      // Vor dem Reconnect Job-Status pruefen: ist er fertig, direkt aufraeumen.
      const handled = await reconcileAgentJob(convId, jobId);
      if (handled) return;
      if (agentStatesRef.current[convId]?.jobId) {
        connectAgentStreamRef.current(convId, jobId, agentOffsets.current[convId] || 0);
      }
    }, delay);
    retryTimers.current[convId] = t;
  }, [reconcileAgentJob, updateAgentState]);

  const connectAgentStream = useCallback(async (convId: string, jobId: string, offset: number) => {
    const token = getToken();
    const url = `/api/chat/conversations/${convId}/agent-stream?job_id=${encodeURIComponent(jobId)}&offset=${offset}`;

    agentAbortControllers.current[convId]?.abort();
    const controller = new AbortController();
    agentAbortControllers.current[convId] = controller;

    if (!agentAccumulators.current[convId]) {
      agentAccumulators.current[convId] = { stream: '', think: '', trace: [] };
    }
    const acc = agentAccumulators.current[convId];

    try {
      const resp = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: controller.signal,
      });
      if (resp.status === 404) {
        // Job abgelaufen/nicht mehr im Speicher: Reconnect aufgeben, Zustand säubern.
        clearAgentJob(convId);
        updateAgentState(convId, { isStreaming: false, streamingContent: '', thinkingContent: '', toolTrace: [], jobId: null, status: 'idle' });
        delete agentAccumulators.current[convId];
        delete agentAbortControllers.current[convId];
        return;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      // Erfolgreich verbunden: als lebender Reader markieren, Retry-Zaehler zuruecksetzen.
      activeReaders.current.add(convId);
      retryCounts.current[convId] = 0;

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buf = '', evt = '';

      while (true) {
        const { done, value } = await reader.read();
        if (value) buf += decoder.decode(value, { stream: true });
        let lines: string[];
        if (done) {
          // Reader-Flush: letzten Rest dekodieren, damit ein finales Event
          // ohne abschliessenden Zeilenumbruch nicht verloren geht.
          buf += decoder.decode();
          lines = buf.split('\n');
          buf = '';
        } else {
          lines = buf.split('\n');
          buf = lines.pop() || '';
        }

        for (const line of lines) {
          if (line.startsWith('event: ')) { evt = line.slice(7).trim(); continue; }
          if (!line.startsWith('data: ')) continue;
          try {
            const data = JSON.parse(line.slice(6));
            const idx = data._idx;
            if (typeof idx === 'number') {
              agentOffsets.current[convId] = idx + 1;
              persistAgentJob(convId, jobId, idx + 1);
            }

            if (evt === 'status') {
              acc.trace.push({ type: 'status', content: data.content || '', ts: Date.now() });
              updateAgentState(convId, { thinkingContent: data.content || '', toolTrace: [...acc.trace] });
            } else if (evt === 'tool_start') {
              acc.trace.push({ type: 'tool_start', content: data.tools || '', ts: Date.now() });
              updateAgentState(convId, { thinkingContent: `Tool: ${data.tools}`, toolTrace: [...acc.trace] });
            } else if (evt === 'tool_event') {
              acc.trace.push({ type: 'tool_event', content: typeof data === 'string' ? data : JSON.stringify(data), ts: Date.now() });
              updateAgentState(convId, { toolTrace: [...acc.trace] });
            } else if (evt === 'clarify') {
              updateAgentState(convId, { clarify: { clarify_id: data.clarify_id, question: data.question || '', choices: Array.isArray(data.choices) ? data.choices : [] } });
            } else if (evt === 'chunk') {
              acc.stream += data.content || '';
              updateAgentState(convId, { streamingContent: acc.stream });
            } else if (evt === 'thinking') {
              acc.think += data.content || '';
              updateAgentState(convId, { thinkingContent: acc.think });
            } else if (evt === 'done') {
              setMessages(prev => [...prev, {
                id: data.message_id || uuid(), role: 'assistant',
                content: data.content || acc.stream, tokens: data.tokens, cost_usd: data.cost_usd || null,
                tool_trace: acc.trace.length > 0 ? [...acc.trace] : null,
                tools_used: data.tools_used || null,
                elapsed_s: data.elapsed_s || null,
                citations: null, created_at: new Date().toISOString(),
                reasoning_tokens: null, thinking: acc.think || null,
                job_id: jobId,
              }]);
              updateAgentState(convId, { isStreaming: false, streamingContent: '', thinkingContent: '', toolTrace: [], jobId: null, status: 'done' });
              delete agentAccumulators.current[convId];
              delete agentAbortControllers.current[convId];
              clearAgentJob(convId);
              loadConversations();
              return;
            } else if (evt === 'stopped') {
              // Vom Nutzer gestoppt: bereits Gestreamtes als Antwort sichern.
              const partial = (acc.stream || '').trim();
              setMessages(prev => [...prev, {
                id: uuid(), role: 'assistant',
                content: partial ? `${partial}\n\n*(Vom Nutzer gestoppt)*` : '*(Vom Nutzer gestoppt)*',
                tokens: null, cost_usd: null,
                tool_trace: acc.trace.length > 0 ? [...acc.trace] : null,
                citations: null, created_at: new Date().toISOString(),
              }]);
              updateAgentState(convId, { isStreaming: false, streamingContent: '', thinkingContent: '', toolTrace: [], jobId: null, status: 'idle' });
              delete agentAccumulators.current[convId];
              delete agentAbortControllers.current[convId];
              clearAgentJob(convId);
              loadConversations();
              return;
            } else if (evt === 'error') {
              setMessages(prev => [...prev, {
                id: uuid(), role: 'assistant',
                content: `Fehler: ${data.error}`, tokens: null, cost_usd: null,
                tool_trace: acc.trace.length > 0 ? [...acc.trace] : null,
                citations: null, created_at: new Date().toISOString(),
              }]);
              updateAgentState(convId, { isStreaming: false, streamingContent: '', thinkingContent: '', toolTrace: [], jobId: null, status: 'error' });
              delete agentAccumulators.current[convId];
              delete agentAbortControllers.current[convId];
              clearAgentJob(convId);
              loadConversations();
              return;
            }
          } catch { /* */ }
        }
        if (done) break;
      }

      // Stream endete ohne terminales Event: geregelter Reconnect mit Status-Check.
      if (agentStatesRef.current[convId]?.jobId) {
        scheduleReconnect(convId, jobId, 2000);
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      if (agentStatesRef.current[convId]?.jobId) {
        scheduleReconnect(convId, jobId, 3000);
      }
    } finally {
      activeReaders.current.delete(convId);
    }
  }, [loadConversations, updateAgentState, scheduleReconnect]);

  // connectAgentStream in die Ref spiegeln (fuer scheduleReconnect).
  connectAgentStreamRef.current = connectAgentStream;

  // Reconnect nach Reload/Kontextwechsel: lief in dieser Konversation ein Agent-Job,
  // pruefen wir zuerst den Job-Status. Ist er fertig, laden wir die Nachrichten direkt;
  // sonst binden wir den Stream ab dem zuletzt gesehenen Offset wieder an.
  useEffect(() => {
    if (!activeId) return;
    const stored = readAgentJob(activeId);
    if (!stored) return;
    // Liveness ueber activeReaders statt blosser Controller-Existenz: verhindert,
    // dass ein Zombie-Controller den Reconnect dauerhaft blockiert.
    if (activeReaders.current.has(activeId)) return;
    const convId = activeId;
    void (async () => {
      const handled = await reconcileAgentJob(convId, stored.jobId);
      if (handled) return;
      agentOffsets.current[convId] = stored.offset;
      if (!agentAccumulators.current[convId]) {
        agentAccumulators.current[convId] = { stream: '', think: '', trace: [] };
      }
      updateAgentState(convId, { isStreaming: true, jobId: stored.jobId, status: 'running', thinkingContent: 'InnoPilot wird wieder verbunden...' });
      connectAgentStream(convId, stored.jobId, stored.offset);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // Unmount-Cleanup: beim Verlassen der ChatPage alle offenen Agent-Streams schliessen,
  // pending Retry-Timer stoppen und den Deep-Research-Stream abbrechen. Der Backend-Job
  // laeuft bewusst weiter; nur die HTTP-Verbindungen werden freigegeben (verhindert das
  // Verbindungs-Leck, das den Browser-Pool von ~6 Verbindungen/Host erschoepfte).
  // Der localStorage-Anker {jobId, offset} bleibt fuer den Reconnect erhalten.
  useEffect(() => {
    const controllers = agentAbortControllers.current;
    const timers = retryTimers.current;
    const readers = activeReaders.current;
    const drAbort = abortControllerRef;
    return () => {
      Object.values(controllers).forEach(c => { try { c.abort(); } catch { /* */ } });
      Object.keys(controllers).forEach(k => { delete controllers[k]; });
      Object.values(timers).forEach(t => clearTimeout(t));
      Object.keys(timers).forEach(k => { delete timers[k]; });
      readers.clear();
      try { drAbort.current?.abort(); } catch { /* */ }
    };
  }, []);

  // Globales agent_jobs_changed-Event: erfaehrt vom Job-Ende auch ohne offenen Stream
  // (z. B. nach Kontextwechsel). Fuer die aktive Konversation mit gespeichertem Job wird
  // der Status abgeglichen und ggf. terminal aufgeraeumt.
  useSSE(useCallback((event: string) => {
    if (event !== 'agent_jobs_changed') return;
    const cid = activeIdRef.current;
    if (!cid) return;
    if (activeReaders.current.has(cid)) return; // laufender Stream liefert das Ende selbst
    const stored = readAgentJob(cid);
    if (!stored) return;
    // Nur reagieren, wenn die aktive Konversation einen Job ohne offenen Stream hat:
    // Status abgleichen und ggf. terminal aufraeumen (Sidebar wird dort aktualisiert).
    void reconcileAgentJob(cid, stored.jobId);
  }, [reconcileAgentJob]));

  const handleSend = async () => {
    let content = input.trim();
    if (!content || sendingRef.current) return;
    // Stop-and-Redirect: läuft gerade ein Agent, brechen wir ihn ab und senden
    // die korrigierte Anweisung sofort hinterher, statt das Eingabefeld zu sperren.
    if (isStreaming) {
      if (mode === 'agent' && activeAgent?.jobId) {
        handleStop();
        await new Promise(r => setTimeout(r, 200));
      } else {
        return;
      }
    }
    sendingRef.current = true;
    setInput('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    try {

    // Quick-Capture: /suche schickt die Anfrage als Web-Recherche an den
    // Agent-Modus (Hermes-native web_search). setMode wirkt erst beim nächsten
    // Render, daher entscheidet effMode über den Versandpfad dieses Sends.
    let effMode = mode;
    if (content.startsWith('/suche ')) {
      content = `Recherchiere im Web: ${content.slice(7).trim()}`;
      effMode = 'agent';
      setMode('agent');
    }

    // /code ist der deterministische Sandbox-Shortcut (Code generieren +
    // ausführen mit Artefakt-Vorschau). Der Agent kann Code auch natürlich über
    // sein Sandbox-Tool ausführen; /code liefert den fokussierten Zwei-Schritt-Flow.
    if (content.startsWith('/code ')) {
      await handleCodeExecute(content.slice(6).trim());
      return;
    }

    let convId = activeId;
    if (!convId) {
      try {
        const conv = await api.post<Conversation>('/api/chat/conversations', {
          model: selectedModel,
          temperature,
          mode: effMode,
        });
        setConversations(prev => [conv, ...prev]);
        convId = conv.id;
        skipNextFetchRef.current = true;
        setActiveId(conv.id);
      } catch (err) {
        setMessages(prev => [...prev, {
          id: uuid(), role: 'assistant',
          content: `Konversation konnte nicht erstellt werden: ${(err as Error).message || 'Unbekannter Fehler'}`,
          tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
        }]);
        return;
      }
    }

    // Anhänge erfassen, bevor der UI-State zurückgesetzt wird
    const filesToUpload = attachments;
    const odSources = contextSources;

    // User-Nachricht sofort anzeigen
    const attachmentMeta = [
      ...filesToUpload.map(f => ({ name: f.name, type: f.type })),
      ...odSources.map(cs => ({ name: cs.name, type: 'onedrive' })),
    ];
    const userMsg: ChatMessage = {
      id: uuid(), role: 'user', content,
      tokens: null, cost_usd: null, citations: null,
      attachments: attachmentMeta.length > 0 ? attachmentMeta : undefined,
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);
    updateAgentState(convId, {
      isStreaming: true, streamingContent: '', toolTrace: [], jobId: null, status: 'running',
      thinkingContent: effMode === 'agent' ? 'InnoPilot wird gestartet...' : '',
    });
    setAttachments([]);
    setContextSources([]);

    const token = getToken();

    // Dokument-Kontext-Brücke: lokale Dateien hochladen und mit OneDrive-Quellen
    // zu einem context_sources-Array kombinieren (Backend extrahiert die Inhalte).
    const contextSourcesPayload: Array<Record<string, unknown>> = [];
    for (const file of filesToUpload) {
      try {
        const fd = new FormData();
        fd.append('file', file);
        const res = await api.upload<{ upload_id: string; name: string }>('/api/chat/uploads', fd);
        contextSourcesPayload.push({ type: 'local_upload', upload_id: res.upload_id, name: res.name });
      } catch (err) {
        setMessages(prev => [...prev, {
          id: uuid(), role: 'assistant',
          content: `Datei "${file.name}" konnte nicht als Kontext verarbeitet werden: ${(err as Error).message}`,
          tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
        }]);
      }
    }
    for (const cs of odSources) {
      contextSourcesPayload.push({
        type: cs.type,
        item_id: cs.item_id,
        path: cs.path,
        name: cs.name,
        recursive: cs.recursive,
      });
    }

    if (effMode === 'agent') {
      try {
        const resp = await fetch(`/api/chat/conversations/${convId}/agent`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          body: JSON.stringify({
            content,
            model: selectedModel,
            temperature,
            enabled_servers: Array.from(enabledServers),
            include_memory: includeMemory,
            context_sources: contextSourcesPayload,
            attachments: attachmentMeta,
          }),
        });
        if (!resp.ok) {
          const errorText = await resp.text().catch(() => `HTTP ${resp.status}`);
          throw new Error(errorText);
        }
        const { job_id } = await resp.json();
        updateAgentState(convId, { jobId: job_id });
        agentOffsets.current[convId] = 0;
        agentAccumulators.current[convId] = { stream: '', think: '', trace: [] };
        persistAgentJob(convId, job_id, 0);
        if (contextSourcesPayload.length > 0) loadContextItems(convId);

        connectAgentStream(convId, job_id, 0);
      } catch (err) {
        updateAgentState(convId, { isStreaming: false, streamingContent: '', thinkingContent: '', status: 'error' });
        setMessages(prev => [...prev, {
          id: uuid(), role: 'assistant',
          content: `Fehler: ${(err as Error).message}`,
          tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
        }]);
        loadConversations();
      } finally {
        loadConversations();
      }
    } else {
      abortControllerRef.current?.abort();
      const controller = new AbortController();
      abortControllerRef.current = controller;

      try {
        const resp = await fetch(`/api/chat/conversations/${convId}/messages`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          body: JSON.stringify({ content, model: selectedModel, temperature, context_sources: contextSourcesPayload, attachments: attachmentMeta }),
          signal: controller.signal,
        });
        if (!resp.ok) {
          const errorText = await resp.text().catch(() => `HTTP ${resp.status}`);
          throw new Error(errorText);
        }
        if (contextSourcesPayload.length > 0) loadContextItems(convId);
        await processSSE(resp, convId);
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setMessages(prev => [...prev, {
            id: uuid(), role: 'assistant',
            content: `Fehler: ${(err as Error).message}`,
            tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
          }]);
        }
      } finally {
        updateAgentState(convId, { isStreaming: false, streamingContent: '', thinkingContent: '', status: 'done' });
        loadConversations();
      }
    }

    } catch (err) {
      console.error('[handleSend] Unerwarteter Fehler:', err);
      setMessages(prev => [...prev, {
        id: uuid(), role: 'assistant',
        content: `Unerwarteter Fehler: ${(err as Error).message}`,
        tokens: null, cost_usd: null, citations: null, created_at: new Date().toISOString(),
      }]);
    } finally {
      sendingRef.current = false;
    }
  };

  const handleStop = () => {
    const cid = activeId;
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    if (cid) {
      // Echter Backend-Stopp: der Agent-Thread bricht kooperativ ab, statt
      // unbemerkt im Hintergrund weiterzulaufen (nur der Client-Stream gekappt).
      const jobId = agentStates[cid]?.jobId;
      if (jobId) {
        api.post(`/api/chat/conversations/${cid}/agent/cancel`, { job_id: jobId }).catch(() => {});
      }
      agentAbortControllers.current[cid]?.abort();
      const currentContent = agentStates[cid]?.streamingContent || '';
      const currentTrace = agentStates[cid]?.toolTrace || [];
      if (currentContent) {
        setMessages(prev => [...prev, {
          id: uuid(), role: 'assistant',
          content: currentContent + '\n\n*(Abgebrochen)*',
          tokens: null, cost_usd: null,
          tool_trace: currentTrace.length > 0 ? currentTrace : null,
          citations: null, created_at: new Date().toISOString(),
        }]);
      }
      updateAgentState(cid, { isStreaming: false, streamingContent: '', thinkingContent: '', toolTrace: [], jobId: null, status: 'idle' });
      delete agentAccumulators.current[cid];
      delete agentAbortControllers.current[cid];
      // Pending Reconnect abbrechen, damit der gestoppte Job nicht wieder verbunden wird.
      const timer = retryTimers.current[cid];
      if (timer) { clearTimeout(timer); delete retryTimers.current[cid]; }
      retryCounts.current[cid] = 0;
      activeReaders.current.delete(cid);
      clearAgentJob(cid);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) setAttachments(prev => [...prev, ...Array.from(e.target.files!)]);
    e.target.value = '';
  };

  const removeAttachment = (idx: number) => setAttachments(prev => prev.filter((_, i) => i !== idx));

  const createTaskFromMessage = async (msgId: string, content: string) => {
    try {
      const result = await api.post<{ task_id: string }>(`/api/chat/messages/${msgId}/create-task`, {
        title: content.slice(0, 80),
      });
      setSelectedTaskId(result.task_id);
    } catch { /* */ }
  };

  const copyAsMarkdown = async (id: string, content: string) => {
    await navigator.clipboard.writeText(content);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };

  const copyAsHtml = async (id: string, content: string) => {
    const html = await markdownToHtml(content);
    const htmlBlob = new Blob([html], { type: 'text/html' });
    const textBlob = new Blob([content], { type: 'text/plain' });
    await navigator.clipboard.write([
      new ClipboardItem({
        'text/html': htmlBlob,
        'text/plain': textBlob,
      }),
    ]);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 2000);
  };


  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    await api.delete(`/api/chat/conversations/${id}`);
    // Laufende Verbindung/Reconnect-Timer der geloeschten Konversation freigeben.
    agentAbortControllers.current[id]?.abort();
    delete agentAbortControllers.current[id];
    delete agentAccumulators.current[id];
    delete agentOffsets.current[id];
    const timer = retryTimers.current[id];
    if (timer) { clearTimeout(timer); delete retryTimers.current[id]; }
    delete retryCounts.current[id];
    activeReaders.current.delete(id);
    clearAgentJob(id);
    setConversations(prev => prev.filter(c => c.id !== id));
    if (activeId === id) { setActiveId(null); setMessages([]); }
  };

  const handleDeleteAllChats = async () => {
    if (conversations.length === 0) return;
    if (!window.confirm('Alle Chat-Konversationen unwiderruflich löschen? Nachrichten und Verlauf gehen verloren.')) return;
    try {
      await api.delete<{ ok: boolean; deleted: number }>('/api/chat/conversations');
      setConversations([]);
      setActiveId(null);
      setMessages([]);
      await loadConversations();
    } catch (err) {
      console.error('Alle Chats löschen fehlgeschlagen:', err);
    }
  };

  const toggleThinking = (id: string) => {
    setExpandedThinking(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const resizeTA = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 144)}px`;
  };

  const modeIcon = (m: string) => {
    if (m === 'deep_research') return <ResearchIcon className="h-4 w-4" />;
    return <SparkleIcon className="h-4 w-4" />;
  };

  const selectedModelInfo = modelInfo(selectedModel);

  // Der /code-Shortcut blendet das optionale stdin-Feld ein (nur im Agent-Modus).
  const showCodeStdin = mode === 'agent' && input.trimStart().startsWith('/code');

  const handleBgSelect = async (url: string | null) => {
    await api.patch('/api/settings', { chat_background_url: url });
    setBgUrl(url);
  };

  const bgStyle: React.CSSProperties = bgUrl
    ? bgUrl.startsWith('gradient:')
      ? { background: bgUrl.slice('gradient:'.length) }
      : { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover', backgroundPosition: 'center' }
    : {};

  return (
    <div className="flex h-full overflow-hidden">
      {/* Sidebar -- Mobile: fullscreen overlay; Desktop: inline */}
      {isChatMobile && showSidebar && (
        <div className="fixed inset-0 z-30 bg-black/30 backdrop-blur-sm" onClick={() => setShowSidebar(false)} />
      )}
      <div className={
        isChatMobile
          ? `fixed inset-y-0 left-0 z-40 w-72 border-r border-gray-200 bg-white/95 backdrop-blur-xl transition-[transform,visibility] duration-200 dark:border-gray-800 dark:bg-gray-950/95 ${showSidebar ? 'visible translate-x-0' : 'invisible -translate-x-full'}`
          : `${showSidebar ? 'w-72' : 'w-0'} shrink-0 overflow-hidden border-r border-gray-200 bg-white/60 backdrop-blur-sm transition-all dark:border-gray-800 dark:bg-gray-900/60`
      }>
        <div className="flex h-full w-72 flex-col">
          <div className="border-b border-gray-200 p-3 dark:border-gray-800">
            <div className="flex gap-2">
              <button onClick={() => { setActiveId(null); setMessages([]); if (isChatMobile) setShowSidebar(false); }} className="flex min-w-0 flex-1 items-center justify-center gap-2 rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700">
                <PlusIcon className="h-4 w-4 shrink-0" />
                Neuer Chat
              </button>
              {conversations.length > 0 && (
                <button
                  type="button"
                  onClick={handleDeleteAllChats}
                  className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-gray-400 transition-colors hover:bg-red-50 hover:text-red-500 lg:h-auto lg:w-auto lg:p-2 dark:hover:bg-red-950/30 dark:hover:text-red-400"
                  title="Alle Konversationen löschen"
                >
                  <TrashIcon className="h-4 w-4" />
                </button>
              )}
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex justify-center p-6"><div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" /></div>
            ) : conversations.length === 0 ? (
              <p className="p-4 text-center text-sm text-gray-500 dark:text-gray-400">Noch keine Konversationen</p>
            ) : (
              <ul className="space-y-0.5 p-2">
                {conversations.map(c => (
                  <li key={c.id} onClick={() => { setActiveId(c.id); if (isChatMobile) setShowSidebar(false); }} className={`group relative cursor-pointer rounded-lg px-3 py-2 pr-9 text-sm transition-colors ${activeId === c.id ? 'bg-indigo-50 dark:bg-indigo-900/30' : 'hover:bg-gray-100 dark:hover:bg-gray-800'}`}>
                    <p className="truncate font-medium text-gray-900 dark:text-gray-100">{c.title || c.last_message_preview || 'Neuer Chat'}</p>
                    <div className="mt-0.5 flex items-center gap-1.5">
                      <span
                        title={
                          agentStates[c.id]?.isStreaming
                            ? 'Agent läuft...'
                            : agentStates[c.id]?.status === 'error'
                              ? 'Agent-Fehler'
                              : c.mode === 'deep_research'
                                ? 'Modus: Deep Research'
                                : 'Modus: Agent (InnoPilot)'
                        }
                        className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                          agentStates[c.id]?.status === 'error'
                            ? 'bg-red-500'
                            : agentStates[c.id]?.isStreaming
                              ? 'bg-violet-500 animate-pulse'
                              : c.mode === 'deep_research'
                                ? 'bg-amber-500'
                                : 'bg-violet-500'
                        }`}
                      />
                      <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600 dark:bg-gray-700 dark:text-gray-400">{modelLabel(c.model)}</span>
                      {c.total_cost_usd > 0 && (
                        <span className="text-[10px] text-gray-400">${c.total_cost_usd.toFixed(4)}</span>
                      )}
                    </div>
                    <button onClick={(e) => handleDelete(c.id, e)} className="absolute right-1 top-1 flex h-9 w-9 items-center justify-center rounded text-gray-500 opacity-50 transition-colors hover:bg-red-100 hover:text-red-600 hover:!opacity-100 group-hover:opacity-80 lg:right-2 lg:top-2 lg:h-auto lg:w-auto lg:p-1 dark:text-gray-400 dark:hover:bg-red-900/40 dark:hover:text-red-400" title="Konversation löschen">
                      <TrashIcon className="h-3.5 w-3.5" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col" style={bgStyle}>
        {/* Top bar — schlank, nur Sidebar-Toggle + Einstellungen */}
        <div className="flex items-center gap-2 border-b border-gray-200 bg-white/50 px-3 py-2 backdrop-blur-sm dark:border-gray-800 dark:bg-gray-900/50">
          <button
            onClick={() => setShowSidebar(!showSidebar)}
            className="flex min-h-11 shrink-0 items-center gap-1.5 rounded-lg p-1.5 text-gray-500 hover:bg-gray-100 lg:min-h-0 dark:text-gray-400 dark:hover:bg-gray-800"
            title={showSidebar ? 'Verlauf ausblenden' : 'Verlauf einblenden'}
            aria-label={showSidebar ? 'Verlauf ausblenden' : 'Chat-Verlauf'}
          >
            <SidebarIcon className="h-5 w-5" />
            <span className="text-xs font-medium lg:hidden">Verlauf</span>
          </button>

          <div className="flex-1" />

          {mode === 'agent' && (() => {
            const isCloud = !!selectedModelInfo && selectedModelInfo.type !== 'local';
            const provider = (selectedModel.split('/')[0] || 'Cloud');
            const activeCount = enabledServers.size;
            const toggleServer = (key: string) => setEnabledServers(prev => {
              const next = new Set(prev);
              if (next.has(key)) next.delete(key); else next.add(key);
              return next;
            });
            // Lokal: leeres Set = voller Zugriff (Default). Beim ersten Abwählen
            // materialisieren wir die volle Liste minus dem abgewählten Server,
            // damit die Auswahl explizit wird. Backend interpretiert leer = alles.
            const localAllOn = enabledServers.size === 0;
            const localServerOn = (key: string) => localAllOn || enabledServers.has(key);
            const toggleLocalServer = (key: string) => setEnabledServers(prev => {
              const base = prev.size === 0 ? new Set(mcpServers.map(s => s.key)) : new Set(prev);
              if (base.has(key)) base.delete(key); else base.add(key);
              return base;
            });
            const badgeClass = isCloud
              ? 'flex items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-700 transition-colors hover:bg-amber-100 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-300 dark:hover:bg-amber-900/50'
              : 'flex items-center gap-1.5 rounded-lg border border-violet-200 bg-violet-50 px-2.5 py-1 text-xs font-medium text-violet-700 transition-colors hover:bg-violet-100 dark:border-violet-800 dark:bg-violet-900/30 dark:text-violet-300 dark:hover:bg-violet-900/50';
            return (
            <div className="relative" ref={mcpPopoverRef}>
              <button onClick={() => setMcpOpen(!mcpOpen)} className={badgeClass}>
                {isCloud ? (
                  <>
                    <CloudIcon className="h-3 w-3" />
                    Cloud · {activeCount} aktiv{includeMemory ? ' · Profil' : ''}
                  </>
                ) : (
                  <>
                    <span className="h-1.5 w-1.5 rounded-full bg-green-400 animate-pulse" />
                    {localAllOn ? 'InnoPilot · Voller Zugriff (lokal)' : `InnoPilot · ${activeCount} Server (lokal)`}
                  </>
                )}
                <svg className={`h-3 w-3 transition-transform ${mcpOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" /></svg>
              </button>
              {mcpOpen && (
                <div className="absolute right-0 top-full z-50 mt-2 w-80 rounded-xl border border-gray-200/80 bg-white p-4 shadow-2xl dark:border-gray-700/80 dark:bg-gray-800">
                  {isCloud ? (
                    <>
                      <div className="mb-2 flex items-center gap-2">
                        <div className="flex h-6 w-6 items-center justify-center rounded-md bg-amber-100 dark:bg-amber-900/40">
                          <CloudIcon className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" />
                        </div>
                        <span className="text-xs font-semibold text-gray-800 dark:text-gray-100">Grounding für Cloud-Modell</span>
                      </div>
                      <div className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-[10px] leading-snug text-amber-700 dark:bg-amber-900/20 dark:text-amber-300">
                        Standardmässig ist alles deaktiviert. Nur aktivierte Inhalte werden an <span className="font-semibold">{provider}</span> gesendet.
                      </div>
                      <div className="max-h-[260px] space-y-1.5 overflow-y-auto">
                        {mcpServers.map(s => {
                          const on = enabledServers.has(s.key);
                          return (
                            <button key={s.key} onClick={() => toggleServer(s.key)} className={`flex w-full items-start gap-2.5 rounded-lg px-3 py-2 text-left transition-colors ${on ? 'bg-indigo-50 dark:bg-indigo-900/30' : 'bg-gray-50 hover:bg-gray-100 dark:bg-gray-900/40 dark:hover:bg-gray-900/70'}`}>
                              <span className={`mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border ${on ? 'border-indigo-500 bg-indigo-500' : 'border-gray-300 dark:border-gray-600'}`}>
                                {on && <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" strokeWidth={3} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>}
                              </span>
                              <div className="min-w-0">
                                <div className="text-[11px] font-semibold text-gray-800 dark:text-gray-200">{s.label}</div>
                                {s.description && <div className="mt-0.5 text-[10px] leading-snug text-gray-500 dark:text-gray-400">{s.description}</div>}
                              </div>
                            </button>
                          );
                        })}
                        {mcpServers.length === 0 && <div className="py-2 text-center text-[11px] italic text-gray-400">Keine Server konfiguriert</div>}
                      </div>
                      <button onClick={() => setIncludeMemory(v => !v)} className={`mt-2 flex w-full items-start gap-2.5 rounded-lg px-3 py-2 text-left transition-colors ${includeMemory ? 'bg-indigo-50 dark:bg-indigo-900/30' : 'bg-gray-50 hover:bg-gray-100 dark:bg-gray-900/40 dark:hover:bg-gray-900/70'}`}>
                        <span className={`mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border ${includeMemory ? 'border-indigo-500 bg-indigo-500' : 'border-gray-300 dark:border-gray-600'}`}>
                          {includeMemory && <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" strokeWidth={3} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>}
                        </span>
                        <div className="min-w-0">
                          <div className="text-[11px] font-semibold text-gray-800 dark:text-gray-200">Memory & USER-Profil</div>
                          <div className="mt-0.5 text-[10px] leading-snug text-gray-500 dark:text-gray-400">Persönliche Präferenzen und gelerntes Wissen einbeziehen</div>
                        </div>
                      </button>
                    </>
                  ) : (
                    <>
                      <div className="mb-2 flex items-center gap-2">
                        <div className="flex h-6 w-6 items-center justify-center rounded-md bg-violet-100 dark:bg-violet-900/40">
                          <SparkleIcon className="h-3.5 w-3.5 text-violet-600 dark:text-violet-400" />
                        </div>
                        <span className="text-xs font-semibold text-gray-800 dark:text-gray-100">Tools & Grounding (lokal)</span>
                      </div>
                      <div className="mb-3 rounded-lg bg-green-50 px-3 py-2 text-[10px] leading-snug text-green-700 dark:bg-green-900/20 dark:text-green-300">
                        Lokales Modell — Daten bleiben auf dem Server. Standardmässig sind alle MCP-Server aktiv; du kannst einzelne für dieses Gespräch abwählen.
                      </div>
                      <div className="max-h-[320px] space-y-1.5 overflow-y-auto">
                        {mcpServers.map(s => {
                          const on = localServerOn(s.key);
                          return (
                            <button key={s.key} onClick={() => toggleLocalServer(s.key)} className={`flex w-full items-start gap-2.5 rounded-lg px-3 py-2 text-left transition-colors ${on ? 'bg-indigo-50 dark:bg-indigo-900/30' : 'bg-gray-50 hover:bg-gray-100 dark:bg-gray-900/40 dark:hover:bg-gray-900/70'}`}>
                              <span className={`mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border ${on ? 'border-indigo-500 bg-indigo-500' : 'border-gray-300 dark:border-gray-600'}`}>
                                {on && <svg className="h-2.5 w-2.5 text-white" fill="none" viewBox="0 0 24 24" strokeWidth={3} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>}
                              </span>
                              <div className="min-w-0">
                                <div className="text-[11px] font-semibold text-gray-800 dark:text-gray-200">{s.label}</div>
                                {s.description && <div className="mt-0.5 text-[10px] leading-snug text-gray-500 dark:text-gray-400">{s.description}</div>}
                              </div>
                            </button>
                          );
                        })}
                        {mcpServers.length === 0 && <div className="py-2 text-center text-[11px] italic text-gray-400">Keine Server konfiguriert</div>}
                      </div>
                      <div className="mt-2 rounded-lg bg-gray-50 px-3 py-2 text-[10px] leading-snug text-gray-500 dark:bg-gray-900/40 dark:text-gray-400">
                        Memory & USER-Profil sind lokal immer aktiv.
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
            );
          })()}

          <button onClick={() => setBgPickerOpen(true)} className="flex h-11 w-11 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-600 lg:h-auto lg:w-auto lg:p-1.5 dark:hover:bg-gray-700 dark:hover:text-gray-300" title="Hintergrund ändern">
            <ImageIcon className="h-5 w-5" />
          </button>
          <button onClick={() => navigate('/settings?tab=llm')} className="flex h-11 w-11 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-600 lg:h-auto lg:w-auto lg:p-1.5 dark:hover:bg-gray-700 dark:hover:text-gray-300" title="LLM-Einstellungen">
            <SettingsIcon className="h-5 w-5" />
          </button>
        </div>

        {/* Nachrichten oder Leerzustand */}
        {!activeId && messages.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center px-4 pb-[env(safe-area-inset-bottom,0px)]">
            <div className="mb-8 text-center">
              <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-indigo-50 dark:bg-indigo-900/30">
                {modeIcon(mode)}
              </div>
              <p className="text-lg font-medium text-gray-600 dark:text-gray-300">
                {mode === 'deep_research' ? 'Welches Thema vertiefen?' : 'Was soll InnoPilot tun?'}
              </p>
            </div>

            {/* Prominente Eingabekarte im Leerzustand */}
            <div className="w-full max-w-4xl px-4">
              {(attachments.length > 0 || contextSources.length > 0) && (
                <div className="mb-2 flex flex-wrap gap-2">
                  {attachments.map((f, i) => (
                    <div key={i} className="flex items-center gap-1.5 rounded-lg bg-white px-2.5 py-1.5 text-xs text-gray-700 shadow-sm dark:bg-gray-800 dark:text-gray-300">
                      <FileIcon className="h-3.5 w-3.5 text-gray-400" />
                      <span className="max-w-[120px] truncate">{f.name}</span>
                      <button onClick={() => removeAttachment(i)} className="ml-1 text-gray-400 hover:text-red-500"><XIcon className="h-3 w-3" /></button>
                    </div>
                  ))}
                  {contextSources.map((cs, i) => (
                    <div key={`ctx-${i}`} className="flex items-center gap-1.5 rounded-lg bg-blue-50 px-2.5 py-1.5 text-xs text-blue-700 shadow-sm dark:bg-blue-900/30 dark:text-blue-300">
                      <OneDriveIcon className="h-3.5 w-3.5" />
                      <span className="max-w-[160px] truncate">{cs.name}</span>
                      {cs.type === 'onedrive_folder' && cs.fileCount && (
                        <span className="text-[10px] opacity-70">({cs.fileCount})</span>
                      )}
                      <button onClick={() => setContextSources(prev => prev.filter((_, j) => j !== i))} className="ml-1 text-blue-400 hover:text-red-500"><XIcon className="h-3 w-3" /></button>
                    </div>
                  ))}
                </div>
              )}
              <div className="rounded-2xl border border-gray-300 bg-white shadow-sm transition-colors focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500 dark:border-gray-600 dark:bg-gray-800">
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={e => { setInput(e.target.value); resizeTA(); }}
                  onKeyDown={handleKeyDown}
                  placeholder={
                    mode === 'deep_research' ? 'Frage für Deep Research eingeben...'
                      : (isChatMobile ? 'Nachricht an InnoPilot…' : 'Nachricht an InnoPilot... (/code für Sandbox-Code, /suche für Web-Recherche)')
                  }
                  rows={4}
                  className="max-h-48 min-h-[96px] w-full resize-none border-0 bg-transparent px-4 pt-3 pb-1 text-sm outline-none placeholder:text-gray-400 dark:text-gray-100 dark:placeholder:text-gray-500"
                />
                {showCodeStdin && (
                  <div className="px-3 pb-1">
                    <button type="button" onClick={() => setCodeStdinOpen(o => !o)} className="text-[11px] font-medium text-gray-500 hover:text-indigo-600 dark:text-gray-400 dark:hover:text-indigo-400">
                      {codeStdinOpen ? '− Eingabedaten (stdin)' : '+ Eingabedaten (stdin)'}
                    </button>
                    {codeStdinOpen && (
                      <textarea
                        value={codeStdin}
                        onChange={e => setCodeStdin(e.target.value)}
                        placeholder="Optionale Standard-Eingabe, die dem Programm zugeführt wird (input()/sys.stdin)…"
                        rows={3}
                        className="mt-1 max-h-40 w-full resize-none rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 font-mono text-xs outline-none focus:border-indigo-500 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-100"
                      />
                    )}
                  </div>
                )}
                <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2.5">
                  <div className="flex shrink-0 rounded-lg bg-gray-100 p-0.5 dark:bg-gray-700">
                    {MODES.map(m => (
                      <button key={m.id} onClick={() => setMode(m.id)} title={m.tooltip} className={`flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium whitespace-nowrap transition-all ${mode === m.id ? 'bg-white text-indigo-700 shadow-sm dark:bg-gray-600 dark:text-indigo-300' : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'}`}>
                        {modeIcon(m.id)}
                        <span className="hidden sm:inline">{m.label}</span>
                      </button>
                    ))}
                  </div>
                  {showModelControls && <div className="hidden h-4 w-px bg-gray-200 sm:block dark:bg-gray-600" />}
                  {showModelControls && (
                    <>
                      <div className="relative" ref={modelDropdownRef}>
                        <button onClick={() => setModelOpen(!modelOpen)} className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-gray-600 transition-colors hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700">
                          {selectedModelInfo?.type === 'local' ? <LockIcon className="h-3 w-3 text-green-500" /> : <CloudIcon className="h-3 w-3 text-blue-400" />}
                          <span className="max-w-[100px] truncate sm:max-w-[180px]">{modelLabel(selectedModel)}</span>
                          {selectedModelInfo?.capabilities.includes('thinking') && <span className="rounded bg-violet-100 px-1 py-px text-[9px] font-semibold text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">T</span>}
                          <ChevronIcon className="h-3 w-3" />
                        </button>
                        {modelOpen && (
                          <div className="absolute bottom-full left-0 z-50 mb-1 max-h-96 w-80 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-800">
                            {groupedModels().map(g => (
                              <div key={g.label}>
                                <p className="sticky top-0 bg-gray-50 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500 dark:bg-gray-900 dark:text-gray-400">{g.label}</p>
                                {g.items.map(m => (
                                  <button key={m.id} onClick={() => { setSelectedModel(m.id); setModelOpen(false); }} className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-indigo-50 dark:hover:bg-indigo-900/30 ${selectedModel === m.id ? 'bg-indigo-50 font-medium text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300' : 'text-gray-700 dark:text-gray-300'}`}>
                                    {m.type === 'local' ? <LockIcon className="h-3 w-3 shrink-0 text-green-500" /> : <CloudIcon className="h-3 w-3 shrink-0 text-blue-400" />}
                                    <span className="flex-1 truncate">{m.name}</span>
                                    {m.capabilities.includes('thinking') && <span className="rounded bg-violet-100 px-1 py-px text-[9px] font-semibold text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">Thinking</span>}
                                    {m.capabilities.includes('web_search') && <span className="rounded bg-emerald-100 px-1 py-px text-[9px] font-semibold text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">Web</span>}
                                    {m.capabilities.includes('deep_research') && <span className="rounded bg-amber-100 px-1 py-px text-[9px] font-semibold text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">Deep</span>}
                                  </button>
                                ))}
                              </div>
                            ))}
                            {groupedModels().length === 0 && (
                              <p className="px-3 py-4 text-center text-xs text-gray-400">Keine Modelle für diesen Modus verfügbar</p>
                            )}
                          </div>
                        )}
                      </div>
                      <div className="relative">
                        <button onClick={() => setTempOpen(!tempOpen)} className="rounded-md px-1.5 py-1 text-[11px] font-medium text-gray-500 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700">T&nbsp;{temperature.toFixed(1)}</button>
                        {tempOpen && (
                          <div className="absolute bottom-full left-0 z-50 mb-1 w-48 rounded-lg border border-gray-200 bg-white p-3 shadow-xl dark:border-gray-700 dark:bg-gray-800">
                            <input type="range" min="0" max="1.5" step="0.1" value={temperature} onChange={e => setTemperature(parseFloat(e.target.value))} className="w-full accent-indigo-600" />
                            <div className="mt-1 flex justify-between text-[10px] text-gray-400"><span>Präzise</span><span>Kreativ</span></div>
                          </div>
                        )}
                      </div>
                    </>
                  )}
                  <div className="ml-auto flex shrink-0 items-center gap-2">
                    <input ref={fileInputRef} type="file" multiple className="hidden" onChange={handleFileSelect} />
                    <button onClick={() => fileInputRef.current?.click()} className="flex h-10 w-10 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 lg:h-auto lg:w-auto lg:p-1 dark:hover:bg-gray-700" title="Datei anhängen"><AttachIcon className="h-4 w-4" /></button>
                    <button onClick={() => setOnedriveOpen(true)} className="flex h-10 w-10 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 lg:h-auto lg:w-auto lg:p-1 dark:hover:bg-gray-700" title="OneDrive-Dateien anhängen"><OneDriveIcon className="h-4 w-4" /></button>
                    <button onClick={() => { setAnonymizeInitialText(input); setAnonymizeOpen(true); }} className="rounded-md p-1 text-emerald-500 transition-colors hover:bg-emerald-50 hover:text-emerald-600 dark:hover:bg-emerald-900/30" title="Text anonymisieren">
                      <ShieldIcon className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => {
                        const mdFile = attachments.find(f => f.name.toLowerCase().endsWith('.md'));
                        if (mdFile) {
                          setConvertSourceFile(mdFile);
                        } else if (input.trim()) {
                          setConvertRawContent(input.trim());
                        }
                      }}
                      disabled={!input.trim() && !attachments.some(f => f.name.toLowerCase().endsWith('.md'))}
                      className="rounded-md p-1 text-orange-500 transition-colors hover:bg-orange-50 hover:text-orange-600 disabled:opacity-40 dark:hover:bg-orange-900/30"
                      title="Text/Datei als Word, PDF oder PPTX konvertieren"
                    >
                      <FileOutputIcon className="h-4 w-4" />
                    </button>
                    <button onClick={handleSend} disabled={!input.trim() || isStreaming} className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 text-white transition-colors hover:bg-indigo-700 disabled:opacity-40"><SendIcon className="h-3.5 w-3.5" /></button>
                  </div>
                </div>
              </div>
              {detectTeachIntent(input) ? (
                <p className="mt-1.5 px-1 text-[11px] text-indigo-600 dark:text-indigo-400">
                  Wird als Lern-Vorschlag erfasst — Freigabe später im Intelligenz-Tab.
                </p>
              ) : (
                <p className="mt-1.5 px-1 text-[11px] text-gray-400 dark:text-gray-500">
                  Tipp: «Merk dir …» bringt InnoPilot etwas dauerhaft bei.
                </p>
              )}
            </div>
          </div>
        ) : (
          <div ref={scrollContainerRef} className="flex-1 overflow-y-auto overscroll-contain px-3 py-4 sm:px-4 sm:py-6">
            <div className="mx-auto max-w-4xl space-y-4">
              {contextItems.length > 0 && (
                <div className="rounded-xl border border-indigo-200/70 bg-indigo-50/60 px-3 py-2 dark:border-indigo-800/50 dark:bg-indigo-900/15">
                  <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-indigo-700 dark:text-indigo-300">
                    <PinIcon className="h-3 w-3" />
                    Angepinnte Dokumente — bleiben der ganzen Konversation als Kontext erhalten
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {contextItems.map(item => (
                      <span
                        key={item.id}
                        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${item.pinned
                          ? 'border-indigo-300 bg-white text-indigo-800 dark:border-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-200'
                          : 'border-gray-300 bg-gray-100 text-gray-400 line-through dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-500'}`}
                      >
                        <FileIcon className="h-3 w-3 shrink-0" />
                        <span className="max-w-[220px] truncate" title={item.name}>{item.name}</span>
                        <span className="text-[9px] opacity-60">{Math.round(item.char_count / 1000)}k</span>
                        <button
                          onClick={() => toggleContextItemPin(item)}
                          title={item.pinned ? 'Aus dem Kontext nehmen (bleibt gespeichert)' : 'Wieder in den Kontext aufnehmen'}
                          className="ml-0.5 opacity-60 transition-opacity hover:opacity-100"
                        >
                          <PinIcon className={`h-3 w-3 ${item.pinned ? '' : 'rotate-45'}`} />
                        </button>
                        <button
                          onClick={() => removeContextItem(item)}
                          title="Dokument endgültig aus der Konversation entfernen"
                          className="opacity-60 transition-opacity hover:text-red-500 hover:opacity-100"
                        >
                          <XIcon className="h-3 w-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {messages.map(msg => (
                <div key={msg.id} className={`group/msg flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div className={`relative ${msg.role === 'user' ? 'chat-bubble-user max-w-[80%] break-words rounded-2xl bg-indigo-600 px-4 py-3 text-white shadow-md' : 'chat-bubble-assistant max-w-[92%] rounded-xl px-5 py-4 text-gray-900 dark:text-gray-100'}`}>
                    {/* Thinking + Tool-Trace Block (standardmässig eingeklappt) */}
                    {(msg.thinking || (msg.tool_trace && msg.tool_trace.length > 0)) && (
                      <div className="mb-3">
                        <button onClick={() => toggleThinking(msg.id)} className="flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium text-violet-600 transition-colors hover:bg-violet-50 hover:text-violet-800 dark:text-violet-400 dark:hover:bg-violet-900/20 dark:hover:text-violet-300">
                          <BrainIcon className="h-3.5 w-3.5" />
                          <span>{msg.tool_trace ? 'Agent-Verlauf' : 'Überlegungen'}</span>
                          {msg.elapsed_s && <span className="ml-1 text-[10px] font-normal opacity-60">({msg.elapsed_s.toFixed(1)}s)</span>}
                          <svg className={`h-3 w-3 transition-transform ${expandedThinking.has(msg.id) ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" /></svg>
                        </button>
                        {expandedThinking.has(msg.id) && (
                          <div className="mt-1.5 space-y-1 rounded-lg border border-violet-200 bg-violet-50 p-3 text-xs dark:border-violet-800 dark:bg-violet-900/20">
                            {msg.tool_trace?.map((te, i) => (
                              <div key={i} className={`flex items-start gap-2 ${te.type === 'tool_start' ? 'font-medium text-indigo-700 dark:text-indigo-400' : 'text-violet-700 dark:text-violet-400'}`}>
                                <span className="mt-0.5 flex-shrink-0 text-[10px] opacity-50">{new Date(te.ts).toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
                                {te.type === 'tool_start' && <span>🔧 {te.content}</span>}
                                {te.type === 'tool_event' && <span className="whitespace-pre-wrap break-all">{te.content}</span>}
                                {te.type === 'status' && <span className="italic">{te.content}</span>}
                              </div>
                            ))}
                            {msg.thinking && (
                              <div className="mt-2 max-h-[400px] overflow-y-auto text-violet-800 dark:text-violet-300">
                                <div className="prose prose-xs prose-violet dark:prose-invert max-w-none [&_p]:my-1 [&_li]:my-0.5 [&_h1]:text-sm [&_h2]:text-xs [&_h3]:text-xs [&_code]:text-[10px]">
                                  <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{msg.thinking}</ReactMarkdown>
                                </div>
                              </div>
                            )}
                            {msg.tools_used && msg.tools_used.length > 0 && (
                              <div className="mt-2 flex flex-wrap gap-1 border-t border-violet-200 pt-2 dark:border-violet-700">
                                <span className="text-[10px] text-violet-600 dark:text-violet-400">Verwendete Tools:</span>
                                {msg.tools_used.map((t, i) => (
                                  <span key={i} className="rounded bg-violet-200 px-1.5 py-0.5 text-[10px] font-medium text-violet-800 dark:bg-violet-800 dark:text-violet-300">{t}</span>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}

                    {msg.role === 'assistant' ? (
                      (() => {
                        const { scope: artScope, names: artNames, cleaned } = parseArtifacts(msg.content);
                        return (
                          <div className="chat-prose prose prose-sm dark:prose-invert max-w-none">
                            <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} rehypePlugins={[rehypeHighlight]} components={chatMdComponents}>{cleaned}</ReactMarkdown>
                            {artScope && artNames.length > 0 && <ArtifactViewer scope={artScope} names={artNames} />}
                          </div>
                        );
                      })()
                    ) : (
                      <div className="whitespace-pre-wrap" style={{ fontSize: '0.9375rem', lineHeight: 1.7 }}>{msg.content}</div>
                    )}

                    {msg.attachments && msg.attachments.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {msg.attachments.map((a, i) => (
                          <span key={i} className="inline-flex items-center gap-1 rounded bg-white/20 px-2 py-0.5 text-[10px]">
                            <FileIcon className="h-3 w-3" />{a.name}
                          </span>
                        ))}
                      </div>
                    )}

                    {msg.role === 'assistant' && (msg.tokens || msg.cost_usd || msg.elapsed_s || msg.model) && (
                      <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-gray-200/60 pt-2 text-[10px] text-gray-400 dark:border-gray-700/40 dark:text-gray-500">
                        {msg.model && (
                          <span className="flex items-center gap-1 rounded bg-gray-100 px-1.5 py-0.5 font-medium text-gray-500 dark:bg-gray-700/50 dark:text-gray-400" title="Modell">
                            {msg.model.split('/').pop()}
                          </span>
                        )}
                        {msg.elapsed_s && (
                          <span className="flex items-center gap-1" title="Dauer">
                            <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
                            {msg.elapsed_s.toFixed(1)}s
                          </span>
                        )}
                        {msg.tokens && (
                          <span className="flex items-center gap-1" title="Tokens">
                            <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25H12" /></svg>
                            {msg.tokens.toLocaleString('de-CH')}
                          </span>
                        )}
                        {msg.reasoning_tokens ? (
                          <span className="flex items-center gap-1" title="Reasoning-Tokens">
                            <BrainIcon className="h-3 w-3" />
                            {msg.reasoning_tokens.toLocaleString('de-CH')}
                          </span>
                        ) : null}
                        {msg.cost_usd ? (
                          <span className="flex items-center gap-1" title="Kosten">
                            <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m-3-2.818.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
                            ${msg.cost_usd.toFixed(4)}
                          </span>
                        ) : null}
                      </div>
                    )}

                    {msg.role === 'assistant' ? (
                      <div className="absolute -bottom-3 right-2 flex items-center gap-0.5 rounded-lg border border-gray-200 bg-white px-1 py-0.5 opacity-0 shadow-sm transition-opacity group-hover/msg:opacity-100 dark:border-gray-700 dark:bg-gray-800">
                        <div className="relative" ref={copyMenuId === msg.id ? copyMenuRef : undefined}>
                          <div className="flex items-center">
                            <button onClick={() => copyAsMarkdown(msg.id, msg.content)} className="rounded-l p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" title="Als Markdown kopieren">
                              {copiedId === msg.id ? <CheckIcon className="h-3.5 w-3.5 text-green-500" /> : <CopyIcon className="h-3.5 w-3.5" />}
                            </button>
                            <button onClick={() => setCopyMenuId(copyMenuId === msg.id ? null : msg.id)} className="rounded-r p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" title="Kopier-Optionen">
                              <ChevronIcon className="h-2.5 w-2.5" />
                            </button>
                          </div>
                          {copyMenuId === msg.id && (
                            <div className="absolute bottom-full right-0 mb-1 w-44 rounded-lg border border-gray-200 bg-white py-1 shadow-lg dark:border-gray-700 dark:bg-gray-800 z-50">
                              <button onClick={() => { copyAsMarkdown(msg.id, msg.content); setCopyMenuId(null); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700">
                                <CopyIcon className="h-3.5 w-3.5" />
                                Als Markdown
                              </button>
                              <button onClick={() => { copyAsHtml(msg.id, msg.content); setCopyMenuId(null); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700">
                                <HtmlIcon className="h-3.5 w-3.5" />
                                Als HTML
                              </button>
                            </div>
                          )}
                        </div>
                        <button onClick={() => { setExportMsgId(msg.id); setExportMsgContent(msg.content); }} className="rounded p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" title="Herunterladen als...">
                          <DownloadIcon className="h-3.5 w-3.5" />
                        </button>
                        <button onClick={() => { setAnonymizeInitialText(msg.content); setAnonymizeOpen(true); }} className="rounded p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" title="Text anonymisieren">
                          <ShieldIcon className="h-3.5 w-3.5" />
                        </button>
                        {anonymizeSessionIds[msg.id] && (
                          <button
                            onClick={async () => {
                              setDeanonymizing(msg.id);
                              try {
                                const res = await api.post<{ original_text: string }>('/api/content/deanonymize', {
                                  text: msg.content,
                                  session_id: anonymizeSessionIds[msg.id],
                                });
                                if (res.original_text) {
                                  setMessages(prev => prev.map(m => m.id === msg.id ? { ...m, content: res.original_text } : m));
                                  setAnonymizeSessionIds(prev => { const n = { ...prev }; delete n[msg.id]; return n; });
                                }
                              } catch { /* ignore */ }
                              setDeanonymizing(null);
                            }}
                            disabled={deanonymizing === msg.id}
                            className="rounded p-1 text-amber-500 hover:text-amber-600 dark:text-amber-400 dark:hover:text-amber-300"
                            title="De-anonymisieren"
                          >
                            <ShieldCheckIcon className="h-3.5 w-3.5" />
                          </button>
                        )}
                        <button onClick={() => createTaskFromMessage(msg.id, msg.content)} className="rounded p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" title="Aufgabe erstellen">
                          <TaskIcon className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    ) : (
                      <div className="absolute -bottom-3 left-2 flex items-center gap-0.5 rounded-lg border border-indigo-400/30 bg-indigo-700 px-1 py-0.5 opacity-0 shadow-sm transition-opacity group-hover/msg:opacity-100">
                        <button onClick={() => copyAsMarkdown(msg.id, msg.content)} className="rounded p-1 text-indigo-200 hover:text-white" title="Prompt kopieren">
                          {copiedId === msg.id ? <CheckIcon className="h-3.5 w-3.5 text-green-300" /> : <CopyIcon className="h-3.5 w-3.5" />}
                        </button>
                        <button onClick={() => { setInput(msg.content); textareaRef.current?.focus(); }} className="rounded p-1 text-indigo-200 hover:text-white" title="Prompt erneut verwenden">
                          <RefreshIcon className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* Streaming / Agent-Verarbeitung */}
              {isStreaming && (
                <div className="flex justify-start">
                  <div className="chat-bubble-assistant max-w-[92%] rounded-xl px-5 py-4 text-gray-900 dark:text-gray-100">
                    {/* Live Tool-Trace (zuklappbar, standardmässig offen) */}
                    {(toolTrace.length > 0 || thinkingContent) && (
                      <details open className="mb-2">
                        <summary className="flex cursor-pointer items-center gap-1.5 text-[11px] font-medium text-violet-600 dark:text-violet-400">
                          <BrainIcon className="h-3.5 w-3.5 animate-pulse" />
                          <span>{mode === 'agent' ? 'InnoPilot arbeitet...' : 'Modell überlegt...'}</span>
                        </summary>
                        <div className="mt-1.5 space-y-1 rounded-lg border border-violet-200 bg-violet-50 p-3 text-xs dark:border-violet-800 dark:bg-violet-900/20">
                          {toolTrace.map((te, i) => (
                            <div key={i} className={`flex items-start gap-2 ${te.type === 'tool_start' ? 'font-medium text-indigo-700 dark:text-indigo-400' : 'text-violet-700 dark:text-violet-400'}`}>
                              <span className="mt-0.5 flex-shrink-0 text-[10px] opacity-50">{new Date(te.ts).toLocaleTimeString('de-CH', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
                              {te.type === 'tool_start' && <span>🔧 {te.content}</span>}
                              {te.type === 'tool_event' && <span className="whitespace-pre-wrap break-all">{te.content}</span>}
                              {te.type === 'status' && <span className="italic">{te.content}</span>}
                            </div>
                          ))}
                          {thinkingContent && !toolTrace.some(t => t.content === thinkingContent) && (
                            <div className="max-h-[300px] overflow-y-auto text-violet-700 dark:text-violet-400">
                              <div className="prose prose-xs prose-violet dark:prose-invert max-w-none [&_p]:my-1 [&_li]:my-0.5 [&_h1]:text-sm [&_h2]:text-xs [&_h3]:text-xs [&_code]:text-[10px]">
                                <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{thinkingContent}</ReactMarkdown>
                              </div>
                            </div>
                          )}
                        </div>
                      </details>
                    )}
                    {/* HITL: strukturierte Rückfrage des Agenten (clarify) */}
                    {clarifyRequest && activeId && (
                      <div className="mb-3 rounded-lg border border-amber-300 bg-amber-50 p-3 dark:border-amber-700 dark:bg-amber-900/20">
                        <p className="mb-2 flex items-center gap-1.5 text-sm font-medium text-amber-900 dark:text-amber-200">
                          <span>❓</span> {clarifyRequest.question}
                        </p>
                        {clarifyRequest.choices.length > 0 && (
                          <div className="mb-2 flex flex-wrap gap-2">
                            {clarifyRequest.choices.map((choice, i) => (
                              <button
                                key={i}
                                onClick={() => answerClarify(activeId, clarifyRequest.clarify_id, choice)}
                                className="rounded-md border border-amber-400 bg-white px-3 py-1.5 text-xs font-medium text-amber-800 transition-colors hover:bg-amber-100 dark:border-amber-600 dark:bg-gray-800 dark:text-amber-200 dark:hover:bg-amber-900/40"
                              >
                                {choice}
                              </button>
                            ))}
                          </div>
                        )}
                        <div className="flex items-center gap-2">
                          <input
                            type="text"
                            value={clarifyDraft}
                            onChange={(e) => setClarifyDraft(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') answerClarify(activeId, clarifyRequest.clarify_id, clarifyDraft); }}
                            placeholder="Eigene Antwort…"
                            className="flex-1 rounded-md border border-amber-300 bg-white px-2.5 py-1.5 text-xs text-gray-900 placeholder-gray-400 focus:border-amber-500 focus:outline-none dark:border-amber-700 dark:bg-gray-800 dark:text-gray-100"
                          />
                          <button
                            onClick={() => answerClarify(activeId, clarifyRequest.clarify_id, clarifyDraft)}
                            disabled={!clarifyDraft.trim()}
                            className="rounded-md bg-amber-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-amber-700 disabled:opacity-40"
                          >
                            Senden
                          </button>
                        </div>
                      </div>
                    )}
                    {streamingContent ? (
                      <div className="chat-prose streaming-cursor prose prose-sm dark:prose-invert max-w-none">
                        <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} rehypePlugins={[rehypeHighlight]} components={chatMdComponents}>{streamingContent}</ReactMarkdown>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2">
                        <div className="flex gap-1">
                          <span className="h-2 w-2 animate-pulse rounded-full bg-gray-400" />
                          <span className="h-2 w-2 animate-pulse rounded-full bg-gray-400 [animation-delay:200ms]" />
                          <span className="h-2 w-2 animate-pulse rounded-full bg-gray-400 [animation-delay:400ms]" />
                        </div>
                      </div>
                    )}
                    <button onClick={handleStop} className="mt-2 flex items-center gap-1.5 rounded-md bg-red-50 px-2.5 py-1 text-[11px] font-medium text-red-600 transition-colors hover:bg-red-100 dark:bg-red-900/20 dark:text-red-400 dark:hover:bg-red-900/30" title="Verarbeitung abbrechen">
                      <svg className="h-3 w-3" fill="currentColor" viewBox="0 0 20 20"><rect x="4" y="4" width="12" height="12" rx="2" /></svg>
                      Stopp
                    </button>
                  </div>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          </div>
        )}

        {/* Anhänge + Eingabe nur wenn Chat aktiv (Leerzustand hat eigene Eingabe) */}
        {(activeId || messages.length > 0) && (attachments.length > 0 || contextSources.length > 0) && (
          <div className="border-t border-gray-200 bg-gray-50 px-4 py-2 dark:border-gray-800 dark:bg-gray-900/50">
            <div className="mx-auto flex max-w-4xl flex-wrap gap-2">
              {attachments.map((f, i) => (
                <div key={i} className="flex items-center gap-1.5 rounded-lg bg-white px-2.5 py-1.5 text-xs text-gray-700 shadow-sm dark:bg-gray-800 dark:text-gray-300">
                  <FileIcon className="h-3.5 w-3.5 text-gray-400" />
                  <span className="max-w-[120px] truncate">{f.name}</span>
                  <button onClick={() => removeAttachment(i)} className="ml-1 text-gray-400 hover:text-red-500">
                    <XIcon className="h-3 w-3" />
                  </button>
                </div>
              ))}
              {contextSources.map((cs, i) => (
                <div key={`ctx-${i}`} className="flex items-center gap-1.5 rounded-lg bg-blue-50 px-2.5 py-1.5 text-xs text-blue-700 shadow-sm dark:bg-blue-900/30 dark:text-blue-300">
                  <OneDriveIcon className="h-3.5 w-3.5" />
                  <span className="max-w-[160px] truncate">{cs.name}</span>
                  {cs.type === 'onedrive_folder' && cs.fileCount && (
                    <span className="text-[10px] opacity-70">({cs.fileCount})</span>
                  )}
                  <button onClick={() => setContextSources(prev => prev.filter((_, j) => j !== i))} className="ml-1 text-blue-400 hover:text-red-500">
                    <XIcon className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Eingabebereich am unteren Rand — nur bei aktiver Konversation */}
        {(activeId || messages.length > 0) && <div className="border-t border-gray-200 bg-white/80 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom,0px))] backdrop-blur-sm dark:border-gray-800 dark:bg-gray-900/80">
          <div className="mx-auto max-w-4xl">
            <div className="rounded-2xl border border-gray-300 bg-white shadow-sm transition-colors focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500 dark:border-gray-600 dark:bg-gray-800">
              <textarea
                ref={textareaRef}
                value={input}
                onChange={e => { setInput(e.target.value); resizeTA(); }}
                onKeyDown={handleKeyDown}
                placeholder={
                  mode === 'deep_research' ? 'Frage für Deep Research eingeben...'
                    : (isStreaming
                      ? 'Korrigieren & neu lenken…'
                      : (isChatMobile ? 'Nachricht an InnoPilot…' : 'Nachricht an InnoPilot... (/code für Sandbox-Code, /suche für Web)'))
                }
                rows={1}
                className="max-h-36 min-h-[44px] w-full resize-none rounded-t-2xl border-0 bg-transparent px-4 pt-3 pb-1 text-sm outline-none placeholder:text-gray-400 dark:text-gray-100 dark:placeholder:text-gray-500"
              />

              {showCodeStdin && (
                <div className="px-3 pb-1">
                  <button type="button" onClick={() => setCodeStdinOpen(o => !o)} className="text-[11px] font-medium text-gray-500 hover:text-indigo-600 dark:text-gray-400 dark:hover:text-indigo-400">
                    {codeStdinOpen ? '− Eingabedaten (stdin)' : '+ Eingabedaten (stdin)'}
                  </button>
                  {codeStdinOpen && (
                    <textarea
                      value={codeStdin}
                      onChange={e => setCodeStdin(e.target.value)}
                      placeholder="Optionale Standard-Eingabe, die dem Programm zugeführt wird (input()/sys.stdin)…"
                      rows={3}
                      className="mt-1 max-h-40 w-full resize-none rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 font-mono text-xs outline-none focus:border-indigo-500 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-100"
                    />
                  )}
                </div>
              )}

              {/* Untere Toolbar: Modi + Modell + Temperatur + Buttons */}
              <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2.5">
                {/* Segmented Mode Control */}
                <div className="flex shrink-0 rounded-lg bg-gray-100 p-0.5 dark:bg-gray-700">
                  {MODES.map(m => (
                    <button
                      key={m.id}
                      onClick={() => setMode(m.id)}
                      title={m.tooltip}
                      className={`flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium whitespace-nowrap transition-all ${
                        mode === m.id
                          ? 'bg-white text-indigo-700 shadow-sm dark:bg-gray-600 dark:text-indigo-300'
                          : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
                      }`}
                    >
                      {modeIcon(m.id)}
                      <span className="hidden sm:inline">{m.label}</span>
                    </button>
                  ))}
                </div>

                {/* Trennlinie */}
                {showModelControls && <div className="hidden h-4 w-px bg-gray-200 sm:block dark:bg-gray-600" />}

                {/* Modell (nicht bei Websuche/Agent) */}
                {showModelControls && (
                  <>
                    <div className="relative" ref={modelDropdownRef}>
                      <button onClick={() => setModelOpen(!modelOpen)} className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-gray-600 transition-colors hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700">
                        {selectedModelInfo?.type === 'local' ? <LockIcon className="h-3 w-3 text-green-500" /> : <CloudIcon className="h-3 w-3 text-blue-400" />}
                        <span className="max-w-[100px] truncate sm:max-w-[180px]">{modelLabel(selectedModel)}</span>
                        {selectedModelInfo?.capabilities.includes('thinking') && <span className="rounded bg-violet-100 px-1 py-px text-[9px] font-semibold text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">T</span>}
                        <ChevronIcon className="h-3 w-3" />
                      </button>
                      {modelOpen && (
                        <div className="absolute bottom-full left-0 z-50 mb-1 max-h-96 w-80 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-800">
                          {groupedModels().map(g => (
                            <div key={g.label}>
                              <p className="sticky top-0 bg-gray-50 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-gray-500 dark:bg-gray-900 dark:text-gray-400">{g.label}</p>
                              {g.items.map(m => (
                                <button key={m.id} onClick={() => { setSelectedModel(m.id); setModelOpen(false); }} className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-indigo-50 dark:hover:bg-indigo-900/30 ${selectedModel === m.id ? 'bg-indigo-50 font-medium text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300' : 'text-gray-700 dark:text-gray-300'}`}>
                                  {m.type === 'local' ? <LockIcon className="h-3 w-3 shrink-0 text-green-500" /> : <CloudIcon className="h-3 w-3 shrink-0 text-blue-400" />}
                                  <span className="flex-1 truncate">{m.name}</span>
                                  {m.capabilities.includes('thinking') && <span className="rounded bg-violet-100 px-1 py-px text-[9px] font-semibold text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">Thinking</span>}
                                  {m.capabilities.includes('web_search') && <span className="rounded bg-emerald-100 px-1 py-px text-[9px] font-semibold text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">Web</span>}
                                  {m.capabilities.includes('deep_research') && <span className="rounded bg-amber-100 px-1 py-px text-[9px] font-semibold text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">Deep</span>}
                                </button>
                              ))}
                            </div>
                          ))}
                          {groupedModels().length === 0 && (
                            <p className="px-3 py-4 text-center text-xs text-gray-400">Keine Modelle für diesen Modus verfügbar</p>
                          )}
                        </div>
                      )}
                    </div>

                    <div className="relative">
                      <button onClick={() => setTempOpen(!tempOpen)} className="rounded-md px-1.5 py-1 text-[11px] font-medium text-gray-500 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700">
                        T&nbsp;{temperature.toFixed(1)}
                      </button>
                      {tempOpen && (
                        <div className="absolute bottom-full left-0 z-50 mb-1 w-48 rounded-lg border border-gray-200 bg-white p-3 shadow-xl dark:border-gray-700 dark:bg-gray-800">
                          <input type="range" min="0" max="1.5" step="0.1" value={temperature} onChange={e => setTemperature(parseFloat(e.target.value))} className="w-full accent-indigo-600" />
                          <div className="mt-1 flex justify-between text-[10px] text-gray-400"><span>Präzise</span><span>Kreativ</span></div>
                        </div>
                      )}
                    </div>
                  </>
                )}

                {/* Modus-Hinweis + Nachrichtenanzahl */}
                <div className="ml-auto flex shrink-0 items-center gap-2">
                  {messages.length > 0 && (
                    <span className="text-[10px] text-gray-400 dark:text-gray-500">{messages.length} Nachrichten</span>
                  )}
                  <input ref={fileInputRef} type="file" multiple className="hidden" onChange={handleFileSelect} />
                  <button onClick={() => fileInputRef.current?.click()} className="flex h-10 w-10 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 lg:h-auto lg:w-auto lg:p-1 dark:hover:bg-gray-700" title="Datei anhängen">
                    <AttachIcon className="h-4 w-4" />
                  </button>
                  <button onClick={() => setOnedriveOpen(true)} className="flex h-10 w-10 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 lg:h-auto lg:w-auto lg:p-1 dark:hover:bg-gray-700" title="OneDrive-Dateien anhängen">
                    <OneDriveIcon className="h-4 w-4" />
                  </button>
                  <button onClick={() => { setAnonymizeInitialText(input); setAnonymizeOpen(true); }} className="rounded-md p-1 text-emerald-500 transition-colors hover:bg-emerald-50 hover:text-emerald-600 dark:hover:bg-emerald-900/30" title="Text anonymisieren">
                    <ShieldIcon className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => {
                      const mdFile = attachments.find(f => f.name.toLowerCase().endsWith('.md'));
                      if (mdFile) {
                        setConvertSourceFile(mdFile);
                      } else if (input.trim()) {
                        setConvertRawContent(input.trim());
                      }
                    }}
                    disabled={!input.trim() && !attachments.some(f => f.name.toLowerCase().endsWith('.md'))}
                    className="rounded-md p-1 text-orange-500 transition-colors hover:bg-orange-50 hover:text-orange-600 disabled:opacity-40 dark:hover:bg-orange-900/30"
                    title="Text/Datei als Word, PDF oder PPTX konvertieren"
                  >
                    <FileOutputIcon className="h-4 w-4" />
                  </button>
                  <button onClick={handleSend} disabled={!input.trim() || (isStreaming && !(mode === 'agent' && !!activeAgent?.jobId))} className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 text-white transition-colors hover:bg-indigo-700 disabled:opacity-40">
                    <SendIcon className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              {detectTeachIntent(input) && (
                <p className="mt-1.5 px-1 text-[11px] text-indigo-600 dark:text-indigo-400">
                  Wird als Lern-Vorschlag erfasst — Freigabe später im Intelligenz-Tab.
                </p>
              )}
            </div>
          </div>
        </div>}
      </div>

      {/* Standard Task-Detail-Dialog */}
      {selectedTaskId && (
        <TaskDetailDialog
          taskId={selectedTaskId}
          onClose={() => setSelectedTaskId(null)}
          onUpdated={() => setSelectedTaskId(null)}
          onOpenTask={setSelectedTaskId}
        />
      )}

      <BackgroundPicker
        isOpen={bgPickerOpen}
        onClose={() => setBgPickerOpen(false)}
        currentUrl={bgUrl}
        onSelect={(url) => handleBgSelect(url)}
      />

      {exportMsgId && (
        <ExportDialog
          isOpen={true}
          onClose={() => setExportMsgId(null)}
          messageId={exportMsgId}
          messageContent={exportMsgContent}
        />
      )}

      {convertRawContent && (
        <ExportDialog
          isOpen={true}
          onClose={() => setConvertRawContent(null)}
          rawContent={convertRawContent}
        />
      )}

      {convertSourceFile && (
        <ExportDialog
          isOpen={true}
          onClose={() => setConvertSourceFile(null)}
          sourceFile={convertSourceFile}
        />
      )}

      <AnonymizePanel
        isOpen={anonymizeOpen}
        onClose={() => setAnonymizeOpen(false)}
        initialText={anonymizeInitialText}
        onInsertText={(text, sessionId) => {
          setInput(text);
          if (sessionId) {
            setAnonymizeSessionIds(prev => ({ ...prev, _pending: sessionId }));
          }
          textareaRef.current?.focus();
        }}
      />

      <OneDrivePicker
        isOpen={onedriveOpen}
        onClose={() => setOnedriveOpen(false)}
        onSelect={(sources) => setContextSources(prev => [...prev, ...sources])}
      />
    </div>
  );
}


/* ── Icons ── */

function PlusIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" /></svg>;
}
function TrashIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0" /></svg>;
}
function SidebarIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5M12 17.25h8.25" /></svg>;
}
function ChevronIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" /></svg>;
}
function CheckIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg>;
}
function CopyIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M15.666 3.888A2.25 2.25 0 0 0 13.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 0 1-.75.75H9.75a.75.75 0 0 1-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 0 1-2.25 2.25H6.75A2.25 2.25 0 0 1 4.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 0 1 1.927-.184" /></svg>;
}
function DownloadIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" /></svg>;
}
function SendIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 12 3.269 3.125A59.769 59.769 0 0 1 21.485 12 59.768 59.768 0 0 1 3.27 20.875L5.999 12Zm0 0h7.5" /></svg>;
}
function AttachIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m18.375 12.739-7.693 7.693a4.5 4.5 0 0 1-6.364-6.364l10.94-10.94A3 3 0 1 1 19.5 7.372L8.552 18.32m.009-.01-.01.01m5.699-9.941-7.81 7.81a1.5 1.5 0 0 0 2.112 2.13" /></svg>;
}
function FileIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>;
}
function FileOutputIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h4.5m-4.5 3h4.5m2.25 0 2.25 2.25 2.25-2.25m-2.25-4.5v6.75M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>;
}
function XIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>;
}
function ExpandIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.8} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M20.25 20.25v-4.5m0 4.5h-4.5m4.5 0L15 15M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15m11.25-11.25v4.5m0-4.5h-4.5m4.5 0L15 9" /></svg>;
}
function FullscreenIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.8} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 8.25V4.5h3.75M20.25 8.25V4.5h-3.75M3.75 15.75v3.75h3.75M20.25 15.75v3.75h-3.75" /></svg>;
}
function PinIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 17v5M9 4h6l-1 7 3 2v2H7v-2l3-2-1-7Z" /></svg>;
}
function SettingsIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.431.992a7.723 7.723 0 0 1 0 .255c-.007.378.138.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" /><path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" /></svg>;
}
function LockIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 1 0-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 0 0 2.25-2.25v-6.75a2.25 2.25 0 0 0-2.25-2.25H6.75a2.25 2.25 0 0 0-2.25 2.25v6.75a2.25 2.25 0 0 0 2.25 2.25Z" /></svg>;
}
function CloudIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15a4.5 4.5 0 0 0 4.5 4.5H18a3.75 3.75 0 0 0 1.332-7.257 3 3 0 0 0-3.758-3.848 5.25 5.25 0 0 0-10.233 2.33A4.502 4.502 0 0 0 2.25 15Z" /></svg>;
}
function BrainIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456Z" /></svg>;
}
function TaskIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" /></svg>;
}
function HtmlIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M17.25 6.75 22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3-4.5 16.5" /></svg>;
}
function OneDriveIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15a4.5 4.5 0 0 0 4.5 4.5H18a3.75 3.75 0 0 0 1.332-7.257 3 3 0 0 0-3.758-3.848 5.25 5.25 0 0 0-10.233 2.33A4.502 4.502 0 0 0 2.25 15Z" /></svg>;
}
function RefreshIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182" /></svg>;
}
function ResearchIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 0 0 1.5-.189m-1.5.189a6.01 6.01 0 0 1-1.5-.189m3.75 7.478a12.06 12.06 0 0 1-4.5 0m3.75 2.383a14.406 14.406 0 0 1-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 1 0-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" /></svg>;
}
function ImageIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0 0 22.5 18.75V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Zm16.5-13.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z" /></svg>;
}
function SparkleIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" /></svg>;
}
function ShieldIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z" /></svg>;
}
function ShieldCheckIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z" /></svg>;
}
