/**
 * Die Signale-Seite: Signa 2.0 in TaskPilot.
 *
 * Sie enthaelt bewusst **keine** Fachlichkeit. Sie reicht dem Paket `@signa/reader` nur
 * das herein, was TaskPilot eigen ist: den Zugang zur Signa-API samt Anmeldung und das
 * aufgeloeste Farbschema. Alles Weitere lebt in Signa und wird dort gepflegt -- was hier
 * landet statt dort, fehlt einem Kunden mit eigener Instanz.
 *
 * Eingehaengt wird ein einziges Bauteil, nicht eine Ansicht. `Signa` bringt seine eigene
 * Reiterzeile mit (Leseliste, Quellen, Einordnung, Einstellungen). Kommt in Signa eine
 * Ansicht dazu, erscheint sie hier ohne Aenderung -- bei vier Menuepunkten in TaskPilot
 * muesste jede einzeln nachgezogen werden.
 *
 * Signa ist ein eigenstaendiger Dienst. Faellt er aus, zeigt diese Seite einen Fehler;
 * der Rest von TaskPilot bleibt unberuehrt.
 *
 * Die vorherige Fassung dieser Seite las die alte ISI-Datenbank ueber `/api/signa/*`.
 * Sie steht weiterhin in der Git-Historie.
 */

import { useMemo } from 'react';
import { Signa, baueApi } from '@signa/reader';
import type { SignaApi } from '@signa/reader';
import '@signa/reader/styles.css';
import { useTheme } from '../contexts/ThemeContext';
import { getToken, tryRefreshToken } from '../api/client';

// Der Weg laeuft in allen Umgebungen durch das TaskPilot-Backend, das die Anfrage an
// Signa weiterreicht (routers/signa2.py). Dort greift die Anmeldung; die Signa-API
// selbst kennt keine Benutzer und bleibt deshalb nach aussen zu.
const BASIS = '/api/signa2/v1';

/**
 * Ein Zugang zu Signa mit TaskPilots Anmeldung.
 *
 * Bewusst nicht der gemeinsame `api`-Client: Der wirft bei 401 den Benutzer auf die
 * Anmeldeseite. Ein Ausfall von Signa darf niemanden abmelden.
 *
 * Hier steht nur das Holen; welche Endpunkte es gibt, weiss `baueApi` im Paket. Ein
 * neuer Endpunkt in Signa ist damit keine Aenderung in TaskPilot.
 */
function erstelleZugang(): SignaApi {
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

  return baueApi(hole);
}

export function SignalePage() {
  const { resolved } = useTheme();

  // Einmal bauen und hineinreichen. Ein Modul-Singleton wuerde die Einbettung
  // stillschweigend an diese eine Anwendung binden.
  const zugang = useMemo(erstelleZugang, []);

  return <Signa api={zugang} theme={resolved} />;
}
