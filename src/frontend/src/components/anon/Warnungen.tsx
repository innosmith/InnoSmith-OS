import { AlertTriangle, ShieldCheck } from 'lucide-react';
import { ENTITAET_FARBE, ENTITAETEN, artName } from './typen';

/** Echte Werte, die im maskierten Text stehen geblieben sind.
 *
 * Steht oben und nicht als Fussnote. Wer den Text kopiert, ohne hier
 * hinzusehen, gibt echte Daten weiter -- und merkt es nie.
 */
export function RestbestandWarnung({ restbestaende }: { restbestaende: string[] }) {
  if (restbestaende.length === 0) return null;
  return (
    <div
      role="alert"
      className="rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 dark:border-rose-800 dark:bg-rose-900/20"
    >
      <h4 className="mb-1 flex items-center gap-1.5 text-sm font-semibold text-rose-800 dark:text-rose-300">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        {restbestaende.length === 1
          ? 'Ein echter Wert steht noch im Text'
          : `${restbestaende.length} echte Werte stehen noch im Text`}
      </h4>
      <p className="mb-2 text-xs leading-snug text-rose-700 dark:text-rose-300">
        Diese Bruchstücke gehören zu einem erkannten Namen, wurden an dieser Stelle aber nicht als
        solcher erkannt — etwa «Eglis» neben «Egli Immobilien AG». Automatisch nachzuziehen hiesse
        raten. Sie sind unten rot markiert; bitte vor dem Weitergeben ansehen.
      </p>
      <div className="flex flex-wrap gap-1.5">
        {restbestaende.map((r, i) => (
          <span
            key={i}
            className="rounded bg-rose-200 px-1.5 py-0.5 font-mono text-xs text-rose-900 dark:bg-rose-800/60 dark:text-rose-100"
          >
            {r}
          </span>
        ))}
      </div>
    </div>
  );
}

/** Ersatznamen, die den Rückweg überlebt haben.
 *
 * Der stillste Fehler der ganzen Strecke: ein erfundener Name, der genauso
 * plausibel aussieht wie ein echter, in einem Text, der als fertig gilt.
 */
export function RueckstandWarnung({ rueckstaende }: { rueckstaende: string[] }) {
  if (rueckstaende.length === 0) {
    return (
      <p className="flex items-center gap-2 text-sm font-medium text-emerald-700 dark:text-emerald-400">
        <ShieldCheck className="h-4 w-4" />
        Alle erfundenen Werte wurden zurückübersetzt.
      </p>
    );
  }
  return (
    <div
      role="alert"
      className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 dark:border-amber-700 dark:bg-amber-900/20"
    >
      <p className="flex items-center gap-1.5 text-sm font-semibold text-amber-900 dark:text-amber-200">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        Nicht verwenden, ohne die markierten Stellen zu prüfen
      </p>
      <p className="mt-1 text-xs leading-snug text-amber-800 dark:text-amber-300">
        Im Text stehen noch erfundene Werte. Sie sehen aus wie echte Namen:
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {rueckstaende.map((r, i) => (
          <span
            key={i}
            className="rounded border border-amber-400/60 px-1.5 py-0.5 font-mono text-xs text-amber-900 dark:text-amber-200"
          >
            {r}
          </span>
        ))}
      </div>
    </div>
  );
}

/** Was erkannt und ersetzt wird -- die Antwort auf die erste Frage jedes Kunden. */
export function Entitaetenlegende({ knapp = false }: { knapp?: boolean }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {ENTITAETEN.map(id => (
        <span
          key={id}
          className={`inline-flex items-center rounded-full px-2.5 py-0.5 font-medium ${
            knapp ? 'text-[10px]' : 'text-xs'
          } ${ENTITAET_FARBE[id]}`}
        >
          {artName(id)}
        </span>
      ))}
    </div>
  );
}
