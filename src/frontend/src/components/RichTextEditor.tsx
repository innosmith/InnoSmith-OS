import { useEditor, EditorContent, type Editor } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import Link from '@tiptap/extension-link';
import Underline from '@tiptap/extension-underline';
import { useEffect } from 'react';

interface RichTextEditorProps {
  content: string;
  onChange?: (html: string) => void;
  editable?: boolean;
  /** In der Leseansicht Links per Klick öffnen (Default: false). */
  openLinksOnClick?: boolean;
  placeholder?: string;
  className?: string;
  minHeight?: string;
  glassBg?: boolean;
}

export function RichTextEditor({
  content,
  onChange,
  editable = true,
  openLinksOnClick = false,
  placeholder,
  className = '',
  minHeight = '120px',
  glassBg = false,
}: RichTextEditorProps) {
  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [2, 3] },
        bulletList: { keepMarks: true, keepAttributes: false },
        orderedList: { keepMarks: true, keepAttributes: false },
      }),
      Link.configure({
        openOnClick: openLinksOnClick,
        HTMLAttributes: {
          class: 'text-indigo-500 underline cursor-pointer',
          target: '_blank',
          rel: 'noopener noreferrer',
        },
      }),
      Underline,
    ],
    content,
    editable,
    onUpdate: ({ editor: e }) => {
      onChange?.(e.getHTML());
    },
    editorProps: {
      attributes: {
        class: 'tiptap-content focus:outline-none',
        style: `min-height: ${minHeight}`,
        ...(placeholder ? { 'data-placeholder': placeholder } : {}),
      },
    },
  });

  useEffect(() => {
    if (editor && editor.getHTML() !== content) {
      editor.commands.setContent(content, { emitUpdate: false });
    }
  }, [content, editor]);

  useEffect(() => {
    if (editor) {
      editor.setEditable(editable);
    }
  }, [editable, editor]);

  if (!editor) return null;

  const baseBg = glassBg
    ? 'bg-white/10 backdrop-blur-md border-white/20'
    : 'bg-white border-gray-200 dark:bg-gray-900 dark:border-gray-700';

  const textColor = glassBg
    ? 'text-white'
    : 'text-gray-900 dark:text-gray-100';

  return (
    <div className={`rounded-xl border overflow-hidden ${baseBg} ${className}`}>
      {editable && <Toolbar editor={editor} glassBg={glassBg} />}
      <div className={`px-4 py-3 text-sm ${textColor}`}>
        <style>{tiptapStyles(glassBg)}</style>
        <EditorContent editor={editor} />
      </div>
    </div>
  );
}

const tiptapStyles = (glass: boolean) => `
.tiptap-content {
  overflow-wrap: anywhere;
  word-break: break-word;
}
.tiptap-content p {
  margin: 0.25em 0;
}
.tiptap-content h2 {
  font-size: 1.25em;
  font-weight: 700;
  margin: 0.75em 0 0.25em;
}
.tiptap-content h3 {
  font-size: 1.1em;
  font-weight: 600;
  margin: 0.5em 0 0.25em;
}
.tiptap-content ul {
  list-style-type: disc;
  padding-left: 1.5em;
  margin: 0.4em 0;
}
.tiptap-content ol {
  list-style-type: decimal;
  padding-left: 1.5em;
  margin: 0.4em 0;
}
.tiptap-content li {
  margin: 0.15em 0;
}
.tiptap-content li p {
  margin: 0;
}
.tiptap-content blockquote {
  border-left: 3px solid ${glass ? 'rgba(255,255,255,0.3)' : '#d1d5db'};
  padding-left: 1em;
  margin: 0.5em 0;
  color: ${glass ? 'rgba(255,255,255,0.7)' : '#6b7280'};
}
.tiptap-content code {
  background: ${glass ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.06)'};
  border-radius: 3px;
  padding: 0.15em 0.3em;
  font-size: 0.9em;
}
.tiptap-content pre {
  background: ${glass ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.06)'};
  border-radius: 0.5em;
  padding: 0.75em 1em;
  margin: 0.5em 0;
  overflow-x: auto;
}
.tiptap-content pre code {
  background: none;
  padding: 0;
}
.tiptap-content a {
  color: ${glass ? '#93c5fd' : '#6366f1'};
  text-decoration: underline;
  cursor: pointer;
}
.tiptap-content strong {
  font-weight: 700;
}
.tiptap-content em {
  font-style: italic;
}
.tiptap-content u {
  text-decoration: underline;
}
.tiptap-content hr {
  border: none;
  border-top: 1px solid ${glass ? 'rgba(255,255,255,0.2)' : '#e5e7eb'};
  margin: 0.75em 0;
}
`;

function Toolbar({ editor, glassBg }: { editor: Editor; glassBg: boolean }) {
  const toolbarBg = glassBg
    ? 'bg-white/5 border-white/10'
    : 'bg-gray-50 border-gray-200 dark:bg-gray-800 dark:border-gray-700';

  const btnBase = 'rounded p-1.5 transition-colors flex items-center justify-center';
  const btnActive = glassBg
    ? 'bg-white/20 text-white'
    : 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900 dark:text-indigo-300';
  const btnInactive = glassBg
    ? 'text-white/60 hover:bg-white/10 hover:text-white'
    : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200';

  const btn = (active: boolean) => `${btnBase} ${active ? btnActive : btnInactive}`;

  const handleLink = () => {
    if (editor.isActive('link')) {
      editor.chain().focus().unsetLink().run();
      return;
    }
    const url = window.prompt('URL eingeben:');
    if (url) {
      editor.chain().focus().setLink({ href: url }).run();
    }
  };

  return (
    <div className={`flex flex-wrap items-center gap-0.5 border-b px-2 py-1.5 ${toolbarBg}`}>
      <ToolbarBtn
        onClick={() => editor.chain().focus().toggleBold().run()}
        active={editor.isActive('bold')}
        className={btn(editor.isActive('bold'))}
        title="Fett (Ctrl+B)"
      >
        <span className="text-xs font-extrabold leading-none" style={{ fontFamily: 'Georgia, serif' }}>B</span>
      </ToolbarBtn>

      <ToolbarBtn
        onClick={() => editor.chain().focus().toggleItalic().run()}
        active={editor.isActive('italic')}
        className={btn(editor.isActive('italic'))}
        title="Kursiv (Ctrl+I)"
      >
        <span className="text-xs font-semibold italic leading-none" style={{ fontFamily: 'Georgia, serif' }}>I</span>
      </ToolbarBtn>

      <ToolbarBtn
        onClick={() => editor.chain().focus().toggleUnderline().run()}
        active={editor.isActive('underline')}
        className={btn(editor.isActive('underline'))}
        title="Unterstrichen (Ctrl+U)"
      >
        <span className="text-xs font-semibold underline leading-none" style={{ fontFamily: 'Georgia, serif' }}>U</span>
      </ToolbarBtn>

      <Divider glassBg={glassBg} />

      <ToolbarBtn
        onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
        active={editor.isActive('heading', { level: 2 })}
        className={btn(editor.isActive('heading', { level: 2 }))}
        title="Überschrift"
      >
        <span className="text-[11px] font-bold leading-none">H</span>
      </ToolbarBtn>

      <ToolbarBtn
        onClick={() => editor.chain().focus().toggleBlockquote().run()}
        active={editor.isActive('blockquote')}
        className={btn(editor.isActive('blockquote'))}
        title="Zitat"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="currentColor">
          <path d="M4.583 17.321C3.553 16.227 3 15 3 13.011c0-3.5 2.457-6.637 6.03-8.188l.893 1.378c-3.335 1.804-3.987 4.145-4.247 5.621.537-.278 1.24-.375 1.929-.311C9.591 11.69 11 13.166 11 15c0 1.933-1.567 3.5-3.5 3.5-1.171 0-2.274-.548-2.917-1.179zm10 0C13.553 16.227 13 15 13 13.011c0-3.5 2.457-6.637 6.03-8.188l.893 1.378c-3.335 1.804-3.987 4.145-4.247 5.621.537-.278 1.24-.375 1.929-.311C19.591 11.69 21 13.166 21 15c0 1.933-1.567 3.5-3.5 3.5-1.171 0-2.274-.548-2.917-1.179z" />
        </svg>
      </ToolbarBtn>

      <Divider glassBg={glassBg} />

      <ToolbarBtn
        onClick={() => editor.chain().focus().toggleBulletList().run()}
        active={editor.isActive('bulletList')}
        className={btn(editor.isActive('bulletList'))}
        title="Aufzählung"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
          <line x1="9" y1="6" x2="20" y2="6" />
          <line x1="9" y1="12" x2="20" y2="12" />
          <line x1="9" y1="18" x2="20" y2="18" />
          <circle cx="4" cy="6" r="1.5" fill="currentColor" stroke="none" />
          <circle cx="4" cy="12" r="1.5" fill="currentColor" stroke="none" />
          <circle cx="4" cy="18" r="1.5" fill="currentColor" stroke="none" />
        </svg>
      </ToolbarBtn>

      <ToolbarBtn
        onClick={() => editor.chain().focus().toggleOrderedList().run()}
        active={editor.isActive('orderedList')}
        className={btn(editor.isActive('orderedList'))}
        title="Nummerierte Liste"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
          <line x1="10" y1="6" x2="20" y2="6" />
          <line x1="10" y1="12" x2="20" y2="12" />
          <line x1="10" y1="18" x2="20" y2="18" />
          <text x="2.5" y="8" fill="currentColor" stroke="none" fontSize="7" fontWeight="600" fontFamily="system-ui">1</text>
          <text x="2.5" y="14" fill="currentColor" stroke="none" fontSize="7" fontWeight="600" fontFamily="system-ui">2</text>
          <text x="2.5" y="20" fill="currentColor" stroke="none" fontSize="7" fontWeight="600" fontFamily="system-ui">3</text>
        </svg>
      </ToolbarBtn>

      <Divider glassBg={glassBg} />

      <ToolbarBtn
        onClick={handleLink}
        active={editor.isActive('link')}
        className={btn(editor.isActive('link'))}
        title="Link einfügen"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
          <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
          <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
        </svg>
      </ToolbarBtn>

      <ToolbarBtn
        onClick={() => editor.chain().focus().setHorizontalRule().run()}
        active={false}
        className={btn(false)}
        title="Trennlinie"
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
          <line x1="3" y1="12" x2="21" y2="12" />
        </svg>
      </ToolbarBtn>

      <Divider glassBg={glassBg} />

      <ToolbarBtn
        onClick={() => editor.chain().focus().undo().run()}
        active={false}
        className={`${btnBase} ${btnInactive} disabled:opacity-30`}
        title="Rückgängig (Ctrl+Z)"
        disabled={!editor.can().undo()}
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
          <polyline points="1 4 1 10 7 10" />
          <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
        </svg>
      </ToolbarBtn>

      <ToolbarBtn
        onClick={() => editor.chain().focus().redo().run()}
        active={false}
        className={`${btnBase} ${btnInactive} disabled:opacity-30`}
        title="Wiederholen (Ctrl+Shift+Z)"
        disabled={!editor.can().redo()}
      >
        <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
          <polyline points="23 4 23 10 17 10" />
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
      </ToolbarBtn>
    </div>
  );
}

function ToolbarBtn({
  onClick,
  active: _active,
  className,
  title,
  disabled,
  children,
}: {
  onClick: () => void;
  active: boolean;
  className: string;
  title: string;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={className}
      title={title}
      disabled={disabled}
      style={{ width: 28, height: 28 }}
    >
      {children}
    </button>
  );
}

function Divider({ glassBg }: { glassBg: boolean }) {
  return (
    <div className={`mx-1 h-5 w-px ${glassBg ? 'bg-white/20' : 'bg-gray-300 dark:bg-gray-600'}`} />
  );
}
