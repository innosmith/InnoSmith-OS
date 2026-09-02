import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { artName, type Fundstelle } from './typen';

interface MaskenvermerkProps {
  fundstellen: Fundstelle[];
  restbestaende?: string[];
  /** An welches Modell der Text ging -- macht den Vorgang konkret. */
  ziel?: string;
}

/** Der Beleg im Gesprächsverlauf: was ersetzt wurde, bevor die Frage hinausging.
 *
 * Ohne ihn ist die Anonymisierung unsichtbare Arbeit. Sie läuft, sie wirkt, und
 * niemand erfährt davon — weder der Nutzer im Alltag noch ein Kunde in einer
 * Vorführung. Der Vermerk ist der einzige Ort im Chat, an dem das Versprechen
 * «es geht maskiert hinaus» eingelöst und nachprüfbar wird.
 */
export function Maskenvermerk({ fundstellen, restbestaende = [], ziel }: MaskenvermerkProps) {
  const [offen, setOffen] = useState(false);

  if (fundstellen.length === 0 && restbestaende.length === 0) return null;

  return (
    <div className="my-2 rounded-xl border border-emerald-200 bg-emerald-50/70 px-3 py-2 text-xs dark:border-emerald-800 dark:bg-emerald-900/20">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
        <span className="text-emerald-900 dark:text-emerald-200">
          {fundstellen.length === 1
            ? 'Eine Angabe ersetzt'
            : `${fundstellen.length} Angaben ersetzt`}
          , bevor die Frage {ziel ? `an ${ziel} ` : ''}hinausging.
        </span>
        {fundstellen.length > 0 && (
          <button
            type="button"
            onClick={() => setOffen(o => !o)}
            className="font-medium text-emerald-700 underline underline-offset-2 hover:text-emerald-900 dark:text-emerald-300 dark:hover:text-emerald-100"
          >
            {offen ? 'verbergen' : 'welche?'}
          </button>
        )}
        {restbestaende.length > 0 && (
          <span className="text-rose-700 dark:text-rose-300">
            Nicht ersetzt: {restbestaende.join(', ')}
          </span>
        )}
      </div>

      {offen && (
        <ul className="mt-2 space-y-1 border-t border-emerald-200 pt-2 dark:border-emerald-800">
          {fundstellen.map((f, i) => (
            <li key={`${f.fake}-${i}`} className="flex flex-wrap items-center gap-1.5">
              <span className="font-medium text-gray-800 dark:text-gray-100">{f.original}</span>
              <span className="text-gray-400">&rarr;</span>
              <span className="font-mono text-gray-600 dark:text-gray-300">{f.fake}</span>
              <span className="rounded-full bg-white/70 px-1.5 py-px text-[10px] text-gray-500 dark:bg-gray-800/60 dark:text-gray-400">
                {artName(f.entity_type)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
