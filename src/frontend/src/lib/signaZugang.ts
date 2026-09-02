/**
 * Der Zugang zu Signa mit TaskPilots Anmeldung.
 *
 * Er stand zuerst in `SignalePage`, weil nur die Seite ihn brauchte. Mit der
 * Briefing-Kachel im Cockpit und der Tonspur in der Rahmenseite gibt es weitere
 * Aufrufer -- und mehrere Fassungen derselben Anmeldung wuerden auseinanderlaufen,
 * sobald sich an der Erneuerung des Tokens etwas aendert.
 *
 * Bewusst nicht der gemeinsame `api`-Client: Der wirft bei 401 den Benutzer auf die
 * Anmeldeseite. Ein Ausfall von Signa darf niemanden abmelden.
 *
 * Hier steht nur das Holen; welche Endpunkte es gibt, weiss `baueApi` im Paket. Ein
 * neuer Endpunkt in Signa ist damit keine Aenderung in TaskPilot -- nur ein Eintrag in
 * der Allowlist von `routers/signa2.py`.
 */

import { baueApi } from '@signa/reader';
import type { SignaApi } from '@signa/reader';
import { getToken, tryRefreshToken } from '../api/client';

// Der Weg laeuft in allen Umgebungen durch das TaskPilot-Backend, das die Anfrage an
// Signa weiterreicht (routers/signa2.py). Dort greift die Anmeldung; die Signa-API
// selbst kennt keine Benutzer und bleibt deshalb nach aussen zu.
const BASIS = '/api/signa2/v1';

export function erstelleZugang(): SignaApi {
  async function hole<T>(pfad: string, optionen: RequestInit = {}): Promise<T> {
    const kopfzeilen: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(optionen.headers as Record<string, string>),
    };
    const marke = getToken();
    if (marke) kopfzeilen['Authorization'] = `Bearer ${marke}`;

    let antwort = await fetch(`${BASIS}${pfad}`, { ...optionen, headers: kopfzeilen });

    if (antwort.status === 401 && (await tryRefreshToken())) {
      antwort = await fetch(`${BASIS}${pfad}`, {
        ...optionen,
        headers: { ...kopfzeilen, Authorization: `Bearer ${getToken()}` },
      });
    }

    if (!antwort.ok) {
      // Die Meldung des Dienstes weiterreichen statt sie zu ersetzen: «502 Bad Gateway»
      // sagt, dass Signa nicht laeuft; «Fehler beim Laden» sagt gar nichts.
      let meldung = `${antwort.status} ${antwort.statusText}`;
      try {
        const inhalt = await antwort.json();
        if (inhalt?.detail) meldung = String(inhalt.detail);
      } catch {
        /* Antwort ohne JSON -- dann bleibt der Statustext. */
      }
      throw new Error(meldung);
    }

    if (antwort.status === 204) return undefined as T;
    return (await antwort.json()) as T;
  }

  // Derselbe Weg fuer Inhalte, die kein JSON sind: die Kostproben der Stimmen und der
  // Ton der Podcastfolgen. Ein `src`-Attribut am `audio`-Element waere einfacher gewesen
  // und traegt die Bearer-Kopfzeile nicht mit; die Anfrage kaeme unangemeldet am Proxy an.
  async function holeRoh(pfad: string): Promise<Blob> {
    const kopfzeilen: Record<string, string> = {};
    const marke = getToken();
    if (marke) kopfzeilen['Authorization'] = `Bearer ${marke}`;

    let antwort = await fetch(`${BASIS}${pfad}`, { headers: kopfzeilen });
    if (antwort.status === 401 && (await tryRefreshToken())) {
      antwort = await fetch(`${BASIS}${pfad}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
    }

    if (!antwort.ok) {
      let meldung = `${antwort.status} ${antwort.statusText}`;
      try {
        const inhalt = await antwort.json();
        if (inhalt?.detail) meldung = String(inhalt.detail);
      } catch {
        /* Antwort ohne JSON -- dann bleibt der Statustext. */
      }
      throw new Error(meldung);
    }
    return await antwort.blob();
  }

  return baueApi(hole, holeRoh);
}
