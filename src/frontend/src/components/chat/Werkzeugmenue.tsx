import { useEffect, useRef, useState } from 'react';
import { Cloud, FileText, Paperclip, Plus, ShieldCheck } from 'lucide-react';

interface WerkzeugmenueProps {
  onDatei: () => void;
  onOneDrive: () => void;
  onAnonymisieren: () => void;
  onKonvertieren: () => void;
  konvertierenMoeglich: boolean;
}

/** Vier Wege unter einem Zeichen, jeder mit einem Wort daneben.
 *
 * Vorher standen hier vier gleich graue Symbole nebeneinander — Büroklammer,
 * Wolke, Schild, Pfeil — und keines trug eine Beschriftung. Wer nicht wusste,
 * dass das Schild anonymisiert, fand es nie; wer es einmal traf, fand es beim
 * nächsten Mal nicht wieder. Ein Menü kostet einen Klick und spart das Raten.
 */
export function Werkzeugmenue({
  onDatei,
  onOneDrive,
  onAnonymisieren,
  onKonvertieren,
  konvertierenMoeglich,
}: WerkzeugmenueProps) {
  const [offen, setOffen] = useState(false);
  const feld = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!offen) return;
    const zu = (e: MouseEvent) => {
      if (feld.current && !feld.current.contains(e.target as Node)) setOffen(false);
    };
    document.addEventListener('mousedown', zu);
    return () => document.removeEventListener('mousedown', zu);
  }, [offen]);

  const waehle = (fn: () => void) => {
    setOffen(false);
    fn();
  };

  return (
    <div className="relative" ref={feld}>
      <button
        onClick={() => setOffen(o => !o)}
        className="flex h-10 w-10 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 lg:h-7 lg:w-7 dark:hover:bg-gray-700"
        title="Anhängen, anonymisieren, konvertieren"
        aria-label="Werkzeuge"
      >
        <Plus className="h-5 w-5" />
      </button>
      {offen && (
        <div className="absolute bottom-full right-0 z-50 mb-1.5 w-60 overflow-hidden rounded-lg border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-800">
          <Eintrag icon={Paperclip} onKlick={() => waehle(onDatei)}>
            Datei anhängen
          </Eintrag>
          <Eintrag icon={Cloud} onKlick={() => waehle(onOneDrive)}>
            OneDrive-Dateien
          </Eintrag>
          <Eintrag icon={ShieldCheck} ton="emerald" onKlick={() => waehle(onAnonymisieren)}>
            Text anonymisieren
          </Eintrag>
          <Eintrag
            icon={FileText}
            ton="orange"
            disabled={!konvertierenMoeglich}
            onKlick={() => waehle(onKonvertieren)}
            hinweis={konvertierenMoeglich ? undefined : 'Text eingeben oder .md anhängen'}
          >
            Als Word, PDF, PPTX …
          </Eintrag>
        </div>
      )}
    </div>
  );
}

function Eintrag({
  icon: Icon,
  children,
  onKlick,
  ton,
  disabled = false,
  hinweis,
}: {
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
  onKlick: () => void;
  ton?: 'emerald' | 'orange';
  disabled?: boolean;
  hinweis?: string;
}) {
  const farbe =
    ton === 'emerald'
      ? 'text-emerald-600 dark:text-emerald-400'
      : ton === 'orange'
        ? 'text-orange-500 dark:text-orange-400'
        : 'text-gray-400';
  return (
    <button
      onClick={onKlick}
      disabled={disabled}
      title={hinweis}
      className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-xs text-gray-700 transition-colors hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40 dark:text-gray-300 dark:hover:bg-gray-700"
    >
      <Icon className={`h-4 w-4 shrink-0 ${farbe}`} />
      <span className="flex-1">{children}</span>
    </button>
  );
}
