import type { ReactNode } from 'react';

interface TextvorschauProps {
  text: string;
  /** Ersetzte Stellen -- indigo, Hover zeigt den Originalwert. */
  nadeln?: string[];
  titelFuer?: (nadel: string) => string;
  /** Echte Werte, die stehen geblieben sind -- rose, Hover warnt. */
  warnNadeln?: string[];
  warnTitelFuer?: (nadel: string) => string;
  /** Höhe begrenzen, wenn die Vorschau in einem Dialog steht. */
  maxHoehe?: string;
}

const TON_ERSATZ =
  'rounded bg-indigo-100 px-0.5 text-indigo-900 underline decoration-indigo-400 decoration-dotted underline-offset-2 dark:bg-indigo-900/50 dark:text-indigo-100';
const TON_WARNUNG =
  'rounded bg-rose-200 px-0.5 font-semibold text-rose-900 dark:bg-rose-800/60 dark:text-rose-100';

function maskiereRegex(wert: string): string {
  return wert.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Der Text mit farbig markierten Stellen.
 *
 * Bis jetzt markierte das Frontend nur die **Fehler** -- die Bruchstücke, die
 * stehen geblieben sind. Man sah also, was schiefging, und nie, was das System
 * geleistet hat. Genau das ist aber der Beweis: Wer im eigenen Text sieht, dass
 * «Sandra Odermatt» dort steht, wo «Louis Egli» stand, versteht die
 * Anonymisierung in einer Sekunde. Eine Liste unter dem Text erklärt sie nie.
 *
 * Die Nadeln werden nach Länge absteigend sortiert, bevor sie in das Muster
 * gehen: Sonst schlägt «Egli» innerhalb von «Egli Immobilien AG» zuerst an, und
 * die Markierung zerfällt mitten im Namen.
 */
export function Textvorschau({
  text,
  nadeln = [],
  titelFuer,
  warnNadeln = [],
  warnTitelFuer,
  maxHoehe = 'max-h-96',
}: TextvorschauProps) {
  const alle = [...new Set([...nadeln, ...warnNadeln])]
    .filter(n => n.length > 0)
    .sort((a, b) => b.length - a.length);

  let inhalt: ReactNode = text;

  if (alle.length > 0) {
    const warnSet = new Set(warnNadeln);
    const muster = new RegExp(`(${alle.map(maskiereRegex).join('|')})`, 'g');
    inhalt = text.split(muster).map((teil, i) => {
      if (!alle.includes(teil)) return teil;
      const warnt = warnSet.has(teil);
      const titel = warnt ? warnTitelFuer?.(teil) : titelFuer?.(teil);
      return (
        <mark key={i} className={warnt ? TON_WARNUNG : TON_ERSATZ} title={titel}>
          {teil}
        </mark>
      );
    });
  }

  return (
    <div
      className={`${maxHoehe} overflow-y-auto rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-600 dark:bg-gray-900/50`}
    >
      <pre className="whitespace-pre-wrap break-words font-mono text-sm leading-relaxed text-gray-900 dark:text-gray-100">
        {inhalt}
      </pre>
    </div>
  );
}
