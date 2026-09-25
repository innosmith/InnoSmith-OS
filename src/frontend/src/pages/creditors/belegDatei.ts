/**
 * PDFs aus dem Backend anzeigen, angemeldet.
 *
 * Die Routen verlangen den Bearer-Header, und ein `<iframe src>` oder ein Link
 * schickt keinen -- die Antwort wäre 403 statt des Belegs. Darum wird die Datei
 * über `api.blob` geholt und als Objekt-URL gezeigt.
 */
import { useEffect, useState } from 'react';
import { api } from '../../api/client';

/** Die Objekt-URL zu `pfad`, solange die Komponente steht; `null` heisst: nichts laden. */
export function useBelegUrl(pfad: string | null): { url: string | null; fehler: string | null } {
  const [stand, setStand] = useState<{ pfad: string; url?: string; fehler?: string } | null>(null);

  useEffect(() => {
    if (!pfad) return;
    let url: string | null = null;
    let gueltig = true;
    api.blob(pfad)
      .then(blob => {
        url = URL.createObjectURL(blob);
        if (gueltig) setStand({ pfad, url });
        else URL.revokeObjectURL(url);
      })
      .catch((e: Error) => { if (gueltig) setStand({ pfad, fehler: e.message }); });
    return () => {
      gueltig = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [pfad]);

  const aktuell = stand && stand.pfad === pfad ? stand : null;
  return { url: aktuell?.url ?? null, fehler: aktuell?.fehler ?? null };
}

/** Öffnet die Datei in einem neuen Fenster. Das Fenster entsteht vor dem Laden,
 *  sonst hielte der Popup-Blocker es für ungefragt. */
export async function belegOeffnen(pfad: string): Promise<void> {
  const fenster = window.open('', '_blank');
  try {
    const url = URL.createObjectURL(await api.blob(pfad));
    if (fenster) fenster.location.href = url;
    else window.location.href = url;
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (e) {
    fenster?.close();
    throw e;
  }
}
