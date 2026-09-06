/**
 * Ablehnungsgrund — die vier wählbaren Gründe, einen Antwort-Entwurf abzulehnen.
 *
 * Bis September 2026 war die Ablehnung das einzige Rückmeldesignal ohne jede
 * Aussage: 16 abgelehnte Entwürfe, alle mit derselben Fehlermeldung, und
 * ausnahmslos an namentliche Menschen gerichtet — kein einziger
 * Maschinen-Absender. Das System wusste also, dass seine Entwürfe an der
 * wichtigsten Post scheitern, und nichts darüber, woran.
 *
 * Die Reihenfolge ist nicht beliebig: die ersten beiden Gründe sagen, dass gar
 * kein Entwurf hätte entstehen sollen, die letzten beiden, dass er schlecht war.
 *
 * Der Grund ist **überspringbar**. Wäre er Pflicht, wäre die schnellste Antwort
 * die erste in der Liste, und die Messreihe trüge eine Mehrheit, die niemand
 * gemeint hat.
 */

/** Kennungen wie im Backend (``learning.REJECTION_REASONS``) — ASCII, nie angezeigt. */
export const ABLEHNUNGSGRUENDE = [
  'nicht_noetig',
  'schreibe_selbst',
  'inhaltlich_falsch',
  'zu_duenn',
] as const;

export type Ablehnungsgrund = (typeof ABLEHNUNGSGRUENDE)[number];

/** Beschriftung je Kennung. Eine Kennung wird nie roh angezeigt. */
export const GRUND_TEXT: Record<Ablehnungsgrund, string> = {
  nicht_noetig: 'Antwort nicht nötig',
  schreibe_selbst: 'Schreibe ich selbst',
  inhaltlich_falsch: 'Inhaltlich falsch',
  zu_duenn: 'Zu dünn, sagt nichts',
};

interface AblehnungsgrundWahlProps {
  /** Wird mit der Kennung aufgerufen, oder mit ``null`` für «ohne Grund». */
  onWahl: (grund: Ablehnungsgrund | null) => void;
  onAbbruch: () => void;
  disabled?: boolean;
  glassBg?: boolean;
}

export function AblehnungsgrundWahl({
  onWahl,
  onAbbruch,
  disabled = false,
  glassBg = false,
}: AblehnungsgrundWahlProps) {
  const rahmen = glassBg
    ? 'border-white/20 bg-white/5'
    : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800';
  const knopf = glassBg
    ? 'border border-white/20 bg-white/10 text-white hover:bg-white/20'
    : 'border border-gray-300 bg-white text-gray-700 hover:bg-gray-100 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600';
  const leise = glassBg ? 'text-white/60' : 'text-gray-500 dark:text-gray-400';

  return (
    <div className={`rounded-lg border p-3 ${rahmen}`}>
      <div className={`mb-2 text-xs font-medium ${leise}`}>Warum lehnst du ab?</div>
      <div className="flex flex-wrap gap-2">
        {ABLEHNUNGSGRUENDE.map((grund) => (
          <button
            key={grund}
            onClick={() => onWahl(grund)}
            disabled={disabled}
            className={`rounded-lg px-3 py-1.5 text-sm transition-colors disabled:opacity-50 ${knopf}`}
          >
            {GRUND_TEXT[grund]}
          </button>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <button
          onClick={() => onWahl(null)}
          disabled={disabled}
          className={`text-xs underline underline-offset-2 disabled:opacity-50 ${leise}`}
        >
          Ohne Grund ablehnen
        </button>
        <button
          onClick={onAbbruch}
          disabled={disabled}
          className={`text-xs underline underline-offset-2 disabled:opacity-50 ${leise}`}
        >
          Abbrechen
        </button>
      </div>
    </div>
  );
}
