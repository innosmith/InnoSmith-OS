import { useEffect, useRef, useState } from 'react';
import {
  Check,
  CheckSquare,
  Code2,
  Copy,
  FileText,
  MoreHorizontal,
  ShieldCheck,
} from 'lucide-react';

interface AntwortleisteProps {
  kopiert: boolean;
  onKopieren: () => void;
  onKopierenHtml: () => void;
  onExport: () => void;
  onAufgabe: () => void;
  onAnonymisieren: () => void;
  /** Fehlt der Rückruf, gab es keine maskierte Vorlage — dann gibt es nichts zurückzuübersetzen. */
  onZurueckuebersetzen?: () => void;
  laeuftZurueck?: boolean;
}

/** Was man mit einer Antwort tun kann — beschriftet und immer sichtbar.
 *
 * Vorher lag hier eine Leiste mit `opacity-0 group-hover/msg:opacity-100`: fünf
 * unbeschriftete Symbole, die erst erschienen, wenn die Maus zufällig darüber
 * fuhr. Auf einem Tablet gab es sie gar nicht, in einer Vorführung sah sie
 * niemand, und der meistgenutzte Weg des Hauses — Antwort nach Word — war ein
 * 14-Pixel-Pfeil ohne Wort daneben.
 *
 * Vier Wege stehen aussen, weil sie täglich gebraucht werden. Alles Weitere
 * liegt hinter `⋯`, damit die Zeile kurz bleibt.
 */
export function Antwortleiste({
  kopiert,
  onKopieren,
  onKopierenHtml,
  onExport,
  onAufgabe,
  onAnonymisieren,
  onZurueckuebersetzen,
  laeuftZurueck = false,
}: AntwortleisteProps) {
  const [menueOffen, setMenueOffen] = useState(false);
  const menue = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menueOffen) return;
    const zu = (e: MouseEvent) => {
      if (menue.current && !menue.current.contains(e.target as Node)) setMenueOffen(false);
    };
    document.addEventListener('mousedown', zu);
    return () => document.removeEventListener('mousedown', zu);
  }, [menueOffen]);

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1">
      <Knopf onKlick={onKopieren} icon={kopiert ? Check : Copy} betont={kopiert}>
        {kopiert ? 'Kopiert' : 'Kopieren'}
      </Knopf>

      <Knopf onKlick={onExport} icon={FileText}>
        Als Word …
      </Knopf>

      <Knopf onKlick={onAufgabe} icon={CheckSquare}>
        Aufgabe
      </Knopf>

      {onZurueckuebersetzen && (
        <Knopf onKlick={onZurueckuebersetzen} icon={ShieldCheck} warm disabled={laeuftZurueck}>
          {laeuftZurueck ? 'Übersetzt …' : 'Zurückübersetzen'}
        </Knopf>
      )}

      <div className="relative" ref={menue}>
        <button
          onClick={() => setMenueOffen(o => !o)}
          className="flex items-center rounded-lg border border-transparent px-1.5 py-1 text-gray-400 transition-colors hover:border-gray-200 hover:bg-white hover:text-gray-600 dark:hover:border-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-300"
          title="Weitere Aktionen"
        >
          <MoreHorizontal className="h-4 w-4" />
        </button>
        {menueOffen && (
          <div className="absolute bottom-full left-0 z-50 mb-1 w-52 overflow-hidden rounded-lg border border-gray-200 bg-white py-1 shadow-xl dark:border-gray-700 dark:bg-gray-800">
            <Eintrag
              onKlick={() => {
                onKopierenHtml();
                setMenueOffen(false);
              }}
              icon={Code2}
            >
              Als HTML kopieren
            </Eintrag>
            <Eintrag
              onKlick={() => {
                onExport();
                setMenueOffen(false);
              }}
              icon={FileText}
            >
              Als PDF, PPTX oder MD …
            </Eintrag>
            <Eintrag
              onKlick={() => {
                onAnonymisieren();
                setMenueOffen(false);
              }}
              icon={ShieldCheck}
            >
              Text anonymisieren
            </Eintrag>
          </div>
        )}
      </div>
    </div>
  );
}

function Knopf({
  onKlick,
  icon: Icon,
  children,
  betont = false,
  warm = false,
  disabled = false,
}: {
  onKlick: () => void;
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
  betont?: boolean;
  warm?: boolean;
  disabled?: boolean;
}) {
  const ton = betont
    ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300'
    : warm
      ? 'border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-300 dark:hover:bg-amber-900/50'
      : 'border-gray-200 bg-white/70 text-gray-600 hover:bg-white hover:text-gray-900 dark:border-gray-700 dark:bg-gray-800/60 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-white';
  return (
    <button
      onClick={onKlick}
      disabled={disabled}
      className={`inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-50 ${ton}`}
    >
      <Icon className="h-3.5 w-3.5" />
      {children}
    </button>
  );
}

function Eintrag({
  onKlick,
  icon: Icon,
  children,
}: {
  onKlick: () => void;
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onKlick}
      className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-gray-700 transition-colors hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
    >
      <Icon className="h-3.5 w-3.5 shrink-0" />
      {children}
    </button>
  );
}
