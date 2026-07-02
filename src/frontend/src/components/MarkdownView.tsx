import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';

/**
 * Einheitliche Markdown-Anzeige mit demselben Renderer und Styling wie der
 * Chat (geteilte ``chat-prose``-Styles, ``remarkGfm`` + ``remarkBreaks``).
 * Verhindert, dass rohe Markdown-Notation (``##``, ``**`` usw.) sichtbar bleibt.
 * Farben erben via ``currentColor`` vom umgebenden Container.
 */
export function MarkdownView({ text, className = '' }: { text: string; className?: string }) {
  if (!text) return null;
  return (
    <div className={`chat-prose prose prose-sm max-w-none dark:prose-invert ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{text}</ReactMarkdown>
    </div>
  );
}
