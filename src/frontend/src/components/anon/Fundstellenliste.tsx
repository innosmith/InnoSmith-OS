import { ArrowDown } from 'lucide-react';

import { artName, ENTITAET_FARBE, type Fundstelle } from './typen';

interface FundstellenlisteProps {
  fundstellen: Fundstelle[];
  /** Ersatzwerte, deren Maskierung der Nutzer zurückgenommen hat. */
  zurueckgenommen?: Set<string>;
  /** Fehlt der Rückruf, entfällt der Knopf «Zurücknehmen» -- Anzeige ohne Eingriff. */
  onUmschalten?: (fake: string) => void;
  /** Tailwind-Höhenklasse; darüber scrollt die Liste in sich statt die Seite zu strecken. */
  maxHoehe?: string;
}

/** Was wodurch ersetzt wurde, mit der Möglichkeit, eine Zeile zurückzunehmen.
 *
 * Die Rücknahme ist kein Komfort, sondern die Antwort auf einen echten Fall:
 * Die Erkennung hält ein Sachwort für eine Firma und maskiert es. Ohne Rücknahme
 * bleiben zwei schlechte Wege -- den verfälschten Text abschicken oder von Hand
 * nacharbeiten. Beide führen dazu, dass die Anonymisierung beim nächsten Mal
 * ausgeschaltet bleibt.
 */
export function Fundstellenliste({
  fundstellen,
  zurueckgenommen = new Set(),
  onUmschalten,
  maxHoehe,
}: FundstellenlisteProps) {
  if (fundstellen.length === 0) {
    return (
      <p className="rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-500 dark:border-gray-600 dark:text-gray-400">
        Keine Ersetzungen.
      </p>
    );
  }

  return (
    <ul
      className={`divide-y divide-gray-200 rounded-lg border border-gray-200 dark:divide-gray-600 dark:border-gray-600 ${
        maxHoehe ? `${maxHoehe} overflow-y-auto` : ''
      }`}
    >
      {fundstellen.map((f, i) => {
        const offen = zurueckgenommen.has(f.fake);
        return (
          <li
            key={`${f.fake}-${i}`}
            className={`px-3 py-2.5 ${
              offen ? 'bg-amber-50 dark:bg-amber-900/20' : 'hover:bg-gray-50 dark:hover:bg-gray-700/30'
            }`}
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {artName(f.entity_type)}
              </span>
              {onUmschalten && (
                <button
                  type="button"
                  onClick={() => onUmschalten(f.fake)}
                  className="shrink-0 rounded-md border border-gray-300 px-2 py-1 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                >
                  {offen ? 'Wieder maskieren' : 'Zurücknehmen'}
                </button>
              )}
            </div>

            <p className="mt-1.5 text-sm">
              <span className="sr-only">Im Text: </span>
              <span
                title="Im Text"
                className="rounded bg-rose-100 px-1.5 py-0.5 font-medium break-words text-rose-800 dark:bg-rose-900/30 dark:text-rose-300"
              >
                {f.original}
              </span>
            </p>

            <p className="mt-1 flex items-start gap-1.5 text-sm">
              <ArrowDown className="mt-1 h-3 w-3 shrink-0 text-gray-400" aria-hidden />
              <span className="sr-only">Ersetzt durch: </span>
              <span
                title="Ersetzt durch"
                className={`min-w-0 rounded px-1.5 py-0.5 break-words ${
                  ENTITAET_FARBE[f.entity_type] || ENTITAET_FARBE.UNKNOWN
                } ${offen ? 'line-through opacity-60' : ''}`}
              >
                {f.fake}
              </span>
            </p>
          </li>
        );
      })}
    </ul>
  );
}
