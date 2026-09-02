import { useState, useEffect, useRef } from 'react';
import { getToken } from '../api/client';

type ExportFormat = 'markdown' | 'docx' | 'pdf' | 'pptx';

interface ExportDialogBaseProps {
  isOpen: boolean;
  onClose: () => void;
}

interface MessageModeProps extends ExportDialogBaseProps {
  messageId: string;
  messageContent: string;
  rawContent?: never;
  sourceFile?: never;
}

interface DirectTextModeProps extends ExportDialogBaseProps {
  rawContent: string;
  messageId?: never;
  messageContent?: never;
  sourceFile?: never;
}

interface DirectFileModeProps extends ExportDialogBaseProps {
  sourceFile: File;
  messageId?: never;
  messageContent?: never;
  rawContent?: never;
}

export type ExportDialogProps = MessageModeProps | DirectTextModeProps | DirectFileModeProps;

interface TemplateInfo {
  name: string;
  path: string;
}

const FORMAT_OPTIONS: { id: ExportFormat; label: string; ext: string; mime: string }[] = [
  { id: 'markdown', label: 'Markdown (.md)', ext: 'md', mime: 'text/markdown' },
  { id: 'docx', label: 'Word (.docx)', ext: 'docx', mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
  { id: 'pdf', label: 'PDF (.pdf)', ext: 'pdf', mime: 'application/pdf' },
  { id: 'pptx', label: 'PowerPoint (.pptx)', ext: 'pptx', mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' },
];

function getContentText(props: ExportDialogProps): string {
  if ('messageContent' in props && props.messageContent) return props.messageContent;
  if ('rawContent' in props && props.rawContent) return props.rawContent;
  return '';
}

/** Der Titel steht meistens schon im Text -- als erste Überschrift.
 *
 * Vorher stand in jedem Word-Dokument «Export» auf der Titelseite, weil das die
 * Voreinstellung war und niemand sie im Alltag überschreibt. Der Dialog bleibt
 * der Ort, an dem man es ändert; er soll nur nicht bei null anfangen.
 */
function titelAusInhalt(text: string): string {
  const ueberschrift = text.match(/^#{1,3}\s+(.+)$/m);
  const kandidat = ueberschrift
    ? ueberschrift[1]
    : (text.split('\n').find(z => z.trim().length > 0) ?? '');
  const sauber = kandidat.replace(/[*_`#]/g, '').trim();
  return sauber.length > 0 && sauber.length <= 120 ? sauber : '';
}

function dateinameAus(titel: string, ersatz: string): string {
  const slug = titel
    .toLowerCase()
    .replace(/[äàâ]/g, 'a')
    .replace(/[öô]/g, 'o')
    .replace(/[üû]/g, 'u')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
  return slug || ersatz;
}

/** Titelseite, Verzeichnis, Vorlage und Autor sind Hausstil, nicht Tagesfrage.
 *
 * Sie jedes Mal neu zu setzen ist der Grund, warum der Dialog als Umweg
 * empfunden wird. Gemerkt wird bewusst **nicht** der Titel: der gehört zum
 * einzelnen Dokument.
 */
const STIL_SCHLUESSEL = 'export_stil';

interface Exportstil {
  author: string;
  template: string;
  titlePage: boolean;
  toc: boolean;
}

function leseStil(): Partial<Exportstil> {
  try {
    return JSON.parse(localStorage.getItem(STIL_SCHLUESSEL) || '{}') as Partial<Exportstil>;
  } catch {
    return {};
  }
}

function schreibeStil(stil: Exportstil): void {
  try {
    localStorage.setItem(STIL_SCHLUESSEL, JSON.stringify(stil));
  } catch {
    /* localStorage evtl. nicht verfügbar */
  }
}

export function ExportDialog(props: ExportDialogProps) {
  const { isOpen, onClose } = props;

  const isMessageMode = 'messageId' in props && !!props.messageId;
  const isFileMode = 'sourceFile' in props && !!props.sourceFile;
  const contentText = getContentText(props);

  const [format, setFormat] = useState<ExportFormat>('docx');
  const [titlePage, setTitlePage] = useState(true);
  const [toc, setToc] = useState(true);
  const [template, setTemplate] = useState<string>('');
  const [title, setTitle] = useState('Export');
  const [author, setAuthor] = useState('InnoSmith');
  const [filename, setFilename] = useState('');
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);

  // Merkt sich, ob eine Maus-Interaktion auf dem Backdrop (und nicht im Dialog)
  // begonnen hat. Verhindert, dass Text-Selektion in einem Eingabefeld, die
  // ausserhalb endet, den Dialog schliesst.
  const backdropMouseDown = useRef(false);

  const slideCount = format === 'pptx' && contentText
    ? (contentText.split(/\n---\n/).length)
    : null;

  const supportsFilePicker = typeof window !== 'undefined' && 'showSaveFilePicker' in window;

  useEffect(() => {
    if (isOpen) {
      setError(null);
      setExporting(false);
      const dateStr = new Date().toISOString().slice(0, 10);

      const stil = leseStil();
      if (stil.author) setAuthor(stil.author);
      if (stil.template !== undefined) setTemplate(stil.template);
      if (stil.titlePage !== undefined) setTitlePage(stil.titlePage);
      if (stil.toc !== undefined) setToc(stil.toc);

      if (isFileMode) {
        const stem = (props as DirectFileModeProps).sourceFile.name.replace(/\.[^.]+$/, '');
        setFilename(stem);
        setTitle(stem);
      } else {
        const abgeleitet = titelAusInhalt(contentText);
        setTitle(abgeleitet || 'Export');
        setFilename(dateinameAus(abgeleitet, `export-${dateStr}`));
      }

      if (!isMessageMode) {
        setFormat('docx');
      }

      loadTemplates();
    }
  }, [isOpen]);

  const loadTemplates = async () => {
    setTemplatesLoading(true);
    try {
      const token = getToken();
      const resp = await fetch('/api/content/templates', {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (resp.ok) {
        setTemplates(await resp.json());
      } else {
        setTemplates([]);
      }
    } catch {
      setTemplates([]);
    } finally {
      setTemplatesLoading(false);
    }
  };

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const dialogTitle = isFileMode
    ? `Datei konvertieren: ${(props as DirectFileModeProps).sourceFile.name}`
    : isMessageMode
      ? 'Herunterladen als...'
      : 'Text konvertieren';

  const formatOptions = isMessageMode
    ? FORMAT_OPTIONS
    : FORMAT_OPTIONS.filter(f => f.id !== 'markdown');

  const handleExport = async () => {
    setExporting(true);
    setError(null);
    schreibeStil({ author, template, titlePage, toc });

    try {
      const token = getToken();
      let resp: Response;

      if (isFileMode) {
        const formData = new FormData();
        formData.append('file', (props as DirectFileModeProps).sourceFile);
        formData.append('format', format);
        formData.append('title', title);
        formData.append('author', author);
        formData.append('title_page', String(titlePage));
        formData.append('toc', String(toc));
        if (template) formData.append('template', template);
        if (filename) formData.append('filename', filename);

        resp = await fetch('/api/content/convert/file', {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          body: formData,
        });
      } else if (isMessageMode) {
        resp = await fetch(`/api/chat/messages/${(props as MessageModeProps).messageId}/export`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            format,
            title,
            author,
            title_page: titlePage,
            toc,
            template: template || null,
            filename: filename || null,
          }),
        });
      } else {
        resp = await fetch('/api/content/convert', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            text: (props as DirectTextModeProps).rawContent,
            format,
            title,
            author,
            title_page: titlePage,
            toc,
            template: template || null,
            filename: filename || null,
          }),
        });
      }

      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `HTTP ${resp.status}`);
      }

      const blob = await resp.blob();
      const opt = FORMAT_OPTIONS.find(f => f.id === format)!;
      const defaultName = `${filename || 'export'}.${opt.ext}`;

      if (supportsFilePicker) {
        try {
          const handle = await (window as any).showSaveFilePicker({
            suggestedName: defaultName,
            types: [{
              description: opt.label,
              accept: { [opt.mime]: [`.${opt.ext}`] },
            }],
          });
          const writable = await handle.createWritable();
          await writable.write(blob);
          await writable.close();
          onClose();
          return;
        } catch (pickerErr: any) {
          if (pickerErr?.name === 'AbortError') {
            setExporting(false);
            return;
          }
        }
      }

      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = defaultName;
      a.click();
      URL.revokeObjectURL(url);
      onClose();
    } catch (e) {
      setError((e as Error).message || 'Export fehlgeschlagen');
    } finally {
      setExporting(false);
    }
  };

  const showDocOptions = format === 'docx' || format === 'pdf';
  const showPptxOptions = format === 'pptx';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 modal-safe"
      onMouseDown={e => { backdropMouseDown.current = e.target === e.currentTarget; }}
      onMouseUp={e => {
        // Nur schliessen, wenn Maus-Down UND Maus-Up direkt auf dem Backdrop
        // erfolgten (echter Klick daneben) – nicht bei Selektion, die im Dialog
        // startet und ausserhalb endet.
        if (backdropMouseDown.current && e.target === e.currentTarget) onClose();
        backdropMouseDown.current = false;
      }}
    >
      <div
        className="w-full max-w-md rounded-xl border border-gray-200 bg-white p-6 shadow-xl dark:border-gray-700 dark:bg-gray-800"
        onMouseDown={e => e.stopPropagation()}
      >
        <h3 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">{dialogTitle}</h3>

        <div className="mb-4 space-y-2">
          {formatOptions.map(opt => (
            <label
              key={opt.id}
              className={`flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 transition-colors ${
                format === opt.id
                  ? 'border-indigo-500 bg-indigo-50 dark:border-indigo-400 dark:bg-indigo-900/20'
                  : 'border-gray-200 hover:border-gray-300 dark:border-gray-600 dark:hover:border-gray-500'
              }`}
            >
              <input
                type="radio"
                name="format"
                value={opt.id}
                checked={format === opt.id}
                onChange={() => setFormat(opt.id)}
                className="h-4 w-4 text-indigo-600"
              />
              <FormatIcon format={opt.id} className="h-4 w-4 text-gray-500 dark:text-gray-400" />
              <span className="text-sm text-gray-700 dark:text-gray-300">{opt.label}</span>
            </label>
          ))}
        </div>

        <div className="mb-4">
          <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Dateiname</label>
          <input
            type="text"
            value={filename}
            onChange={e => setFilename(e.target.value)}
            className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-white"
            placeholder="export"
          />
        </div>

        {showDocOptions && (
          <div className="mb-4 rounded-lg border border-gray-200 p-3 dark:border-gray-600">
            <div className="mb-2 text-xs font-medium text-gray-500 dark:text-gray-400">Dokument-Optionen</div>
            <div className="space-y-2">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={titlePage} onChange={e => setTitlePage(e.target.checked)} className="h-4 w-4 rounded text-indigo-600" />
                <span className="text-sm text-gray-700 dark:text-gray-300">Titelseite</span>
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={toc} onChange={e => setToc(e.target.checked)} className="h-4 w-4 rounded text-indigo-600" />
                <span className="text-sm text-gray-700 dark:text-gray-300">Inhaltsverzeichnis</span>
              </label>
              <div>
                <label className="mb-1 block text-xs text-gray-500 dark:text-gray-400">Template</label>
                <select
                  value={template}
                  onChange={e => setTemplate(e.target.value)}
                  className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                >
                  <option value="">Standard (InnoSmith)</option>
                  {templates.map(t => (
                    <option key={t.name} value={t.name}>{t.name}</option>
                  ))}
                  {templatesLoading && <option disabled>Laden...</option>}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs text-gray-500 dark:text-gray-400">Titel</label>
                <input type="text" value={title} onChange={e => setTitle(e.target.value)} className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-white" />
              </div>
              <div>
                <label className="mb-1 block text-xs text-gray-500 dark:text-gray-400">Autor</label>
                <input type="text" value={author} onChange={e => setAuthor(e.target.value)} className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-white" />
              </div>
            </div>
          </div>
        )}

        {showPptxOptions && (
          <div className="mb-4 rounded-lg border border-gray-200 p-3 dark:border-gray-600">
            <div className="mb-2 text-xs font-medium text-gray-500 dark:text-gray-400">PowerPoint-Optionen</div>
            <div className="space-y-2">
              <div>
                <label className="mb-1 block text-xs text-gray-500 dark:text-gray-400">Template</label>
                <select value={template} onChange={e => setTemplate(e.target.value)} className="w-full rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-white">
                  <option value="">InnoSmith</option>
                  {templates.filter(t => t.path?.endsWith('.pptx')).map(t => (
                    <option key={t.name} value={t.name}>{t.name}</option>
                  ))}
                </select>
              </div>
              <div className="flex items-start gap-2 rounded-lg bg-blue-50 px-3 py-2 dark:bg-blue-900/20">
                <InfoIcon className="mt-0.5 h-4 w-4 flex-shrink-0 text-blue-500" />
                <span className="text-xs text-blue-700 dark:text-blue-300">
                  Der Inhalt wird als Slide-Script interpretiert (Slides durch --- getrennt).
                  {slideCount !== null && ` ${slideCount} Folie${slideCount !== 1 ? 'n' : ''} erkannt.`}
                </span>
              </div>
            </div>
          </div>
        )}

        {!supportsFilePicker && (
          <div className="mb-4 flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 dark:bg-amber-900/20">
            <InfoIcon className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" />
            <span className="text-xs text-amber-700 dark:text-amber-300">
              Tipp: In Chrome oder Edge kannst du das Zielverzeichnis und den Dateinamen frei wählen. In diesem Browser wird die Datei im Standard-Download-Ordner gespeichert.
            </span>
          </div>
        )}

        {error && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-400">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700">
            Abbrechen
          </button>
          <button onClick={handleExport} disabled={exporting} className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50">
            {exporting ? 'Konvertiert...' : 'Speichern unter...'}
          </button>
        </div>
      </div>
    </div>
  );
}

function FormatIcon({ format, className }: { format: ExportFormat; className?: string }) {
  switch (format) {
    case 'markdown':
      return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>;
    case 'docx':
      return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>;
    case 'pdf':
      return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m.75 12 3 3m0 0 3-3m-3 3v-6m-1.5-9H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>;
    case 'pptx':
      return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 0 0 6 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0 1 18 16.5h-2.25m-7.5 0h7.5m-7.5 0-1 3m8.5-3 1 3m0 0 .5 1.5m-.5-1.5h-9.5m0 0-.5 1.5M9 11.25v1.5M12 9v3.75m3-6v6" /></svg>;
  }
}

function InfoIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z" />
    </svg>
  );
}
