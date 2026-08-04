import { AlertTriangle } from 'lucide-react';

/**
 * DraftWarning — nennt vor der Freigabe die Angaben, die im Entwurf keinen Beleg haben.
 *
 * Der Schreib-Pass darf nur wiedergeben, was in einer recherchierten Quelle steht.
 * Am 04.08.2026 füllte er den Platzhalter einer gelöschten Entwurfs-Quelle mit einer
 * frei erfundenen IP-Adresse — sichtbar wäre das nur beim genauen Nachlesen des
 * ganzen Threads gewesen. Darum stehen unbelegte Angaben jetzt oben an der Karte,
 * nicht im Trace: verworfen wird nichts, der Mensch entscheidet.
 */

export function DraftWarning({
  warning,
  ungroundedValues,
  placeholders,
  glassBg = false,
}: {
  warning?: string | null;
  ungroundedValues?: string[] | null;
  placeholders?: string[] | null;
  glassBg?: boolean;
}) {
  const values = ungroundedValues ?? [];
  const marks = placeholders ?? [];
  if (!warning && values.length === 0 && marks.length === 0) return null;

  return (
    <div
      data-testid="approval-draft-warning"
      className={`rounded-lg border px-3 py-2 text-xs ${
        glassBg
          ? 'border-amber-300/30 bg-amber-400/10 text-amber-100'
          : 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700/60 dark:bg-amber-900/20 dark:text-amber-200'
      }`}
    >
      <div className="flex items-start gap-1.5">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <div className="space-y-1">
          {warning && <p className="wrap-anywhere">{warning}</p>}
          {values.length > 0 && (
            <p className="wrap-anywhere">
              <span className="font-medium">Ohne Beleg:</span>{' '}
              {values.map((v) => (
                <code key={v} className="mr-1 rounded bg-black/10 px-1 dark:bg-white/10">
                  {v}
                </code>
              ))}
            </p>
          )}
          {marks.length > 0 && (
            <p className="wrap-anywhere">
              <span className="font-medium">Platzhalter:</span>{' '}
              {marks.map((m) => (
                <code key={m} className="mr-1 rounded bg-black/10 px-1 dark:bg-white/10">
                  {m}
                </code>
              ))}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
