import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Cloud, HardDrive, ShieldCheck } from 'lucide-react';
import { Entitaetenlegende } from '../anon/Warnungen';

interface DatenraumleisteProps {
  /** Läuft das gewählte Modell auf dieser Maschine? */
  lokal: boolean;
  /** Lesbarer Name des Modells, an das der Text ginge. */
  modell: string;
}

/** Wohin dieser Text geht — dauerhaft sichtbar, nicht als Popup.
 *
 * Der Zustand stand bisher nirgends. Die Maskierung lief, sie wirkte, und
 * niemand erfuhr davon: kein Hinweis vor dem Senden, keiner danach. Damit war
 * die stärkste Eigenschaft des Systems die einzige unsichtbare.
 *
 * Bewusst **kein** Schalter «Klartext». Für ein auswärtiges Modell ist die
 * Maskierung Hauspolitik (`app/services/anon_politik.py`), und ein Schalter,
 * der sie aufhebt, wäre genau die Wahl, die still schiefgeht — angeklickt für
 * einen Sonderfall, vergessen für alle folgenden. Die Leiste zeigt den Zustand
 * und erklärt ihn; ändern lässt er sich über die Wahl des Modells.
 */
export function Datenraumleiste({ lokal, modell }: DatenraumleisteProps) {
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

  return (
    <div
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-1.5 text-xs backdrop-blur-sm ${
        lokal
          ? 'border-emerald-200/70 bg-emerald-50/70 dark:border-emerald-900/60 dark:bg-emerald-950/30'
          : 'border-amber-200/70 bg-amber-50/70 dark:border-amber-900/60 dark:bg-amber-950/30'
      }`}
    >
      {lokal ? (
        <>
          <HardDrive className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
          <span className="font-medium text-emerald-900 dark:text-emerald-200">
            Bleibt auf dieser Maschine
          </span>
          <span className="text-emerald-700/80 dark:text-emerald-300/80">
            {modell} rechnet lokal — der Text verlässt das Haus nicht.
          </span>
        </>
      ) : (
        <>
          <Cloud className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
          <span className="font-medium text-amber-900 dark:text-amber-200">Geht an {modell}</span>
          <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 font-medium text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-300">
            <ShieldCheck className="h-3 w-3" />
            Anonymisiert
          </span>
        </>
      )}

      <div className="relative ml-auto" ref={feld}>
        <button
          onClick={() => setOffen(o => !o)}
          className={`rounded-md px-2 py-0.5 font-medium underline underline-offset-2 transition-colors ${
            lokal
              ? 'text-emerald-700 hover:text-emerald-900 dark:text-emerald-300'
              : 'text-amber-700 hover:text-amber-900 dark:text-amber-300'
          }`}
        >
          Was heisst das?
        </button>
        {offen && (
          <div className="absolute right-0 top-full z-50 mt-1.5 w-80 rounded-xl border border-gray-200 bg-white p-4 text-xs shadow-2xl dark:border-gray-700 dark:bg-gray-800">
            {lokal ? (
              <p className="leading-relaxed text-gray-600 dark:text-gray-300">
                Das Modell läuft auf derselben Maschine wie InnoSmith OS. Es gibt keinen Weg nach
                draussen, den eine Maskierung schützen müsste — darum wird hier nicht maskiert.
              </p>
            ) : (
              <>
                <p className="mb-2 leading-relaxed text-gray-600 dark:text-gray-300">
                  Frage, Verlauf und angeheftete Dokumente werden ersetzt, bevor sie an{' '}
                  <span className="font-medium">{modell}</span> gehen. Die Antwort wird
                  automatisch zurückübersetzt. Was ersetzt wurde, steht als Vermerk im Verlauf.
                </p>
                <p className="mb-2 font-medium text-gray-700 dark:text-gray-200">Erkannt wird:</p>
                <Entitaetenlegende knapp />
              </>
            )}
            <Link
              to="/datenschutz"
              className="mt-3 inline-block font-medium text-indigo-600 hover:underline dark:text-indigo-400"
            >
              Zur Datenschutz-Seite
            </Link>
          </div>
        )}
      </div>
    </div>
  );
}
