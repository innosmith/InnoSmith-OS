/**
 * Die Signale-Seite: Signa 2.0 in TaskPilot.
 *
 * Sie enthaelt bewusst **keine** Fachlichkeit. Sie reicht dem Paket `@signa/reader` nur
 * das herein, was TaskPilot eigen ist: den Zugang zur Signa-API samt Anmeldung und das
 * aufgeloeste Farbschema. Alles Weitere lebt in Signa und wird dort gepflegt -- was hier
 * landet statt dort, fehlt einem Kunden mit eigener Instanz.
 *
 * Eingehaengt wird ein einziges Bauteil, nicht eine Ansicht. `Signa` bringt seine eigene
 * Reiterzeile mit (Leseliste, Quellen, Briefing, Einstellungen). Kommt in Signa eine
 * Ansicht dazu, erscheint sie hier ohne Aenderung -- bei vier Menuepunkten in TaskPilot
 * muesste jede einzeln nachgezogen werden.
 *
 * Die Ansicht steht in der Adresse (`?tab=`), wie im Agenten-Cockpit. Sonst koennte die
 * Briefing-Kachel im Cockpit nur auf die Seite verweisen und nicht auf den Text, den
 * sie anreisst -- und ein Wiederladen fiele auf die Leseliste zurueck.
 *
 * Signa ist ein eigenstaendiger Dienst. Faellt er aus, zeigt diese Seite einen Fehler;
 * der Rest von TaskPilot bleibt unberuehrt.
 *
 * Die vorherige Fassung dieser Seite las die alte ISI-Datenbank ueber `/api/signa/*`.
 * Sie steht weiterhin in der Git-Historie.
 */

import { useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Signa } from '@signa/reader';
import type { Ansicht } from '@signa/reader';
import '@signa/reader/styles.css';
import { useTheme } from '../contexts/ThemeContext';
import { erstelleZugang } from '../lib/signaZugang';

const ANSICHTEN: Ansicht[] = ['leseliste', 'quellen', 'briefing', 'einstellungen'];

export function SignalePage() {
  const { resolved } = useTheme();
  const [suche, setSuche] = useSearchParams();

  // Einmal bauen und hineinreichen. Ein Modul-Singleton wuerde die Einbettung
  // stillschweigend an diese eine Anwendung binden.
  const zugang = useMemo(() => erstelleZugang(), []);

  const gefragt = suche.get('tab') as Ansicht | null;
  const ansicht = gefragt && ANSICHTEN.includes(gefragt) ? gefragt : 'leseliste';

  return (
    <Signa
      api={zugang}
      theme={resolved}
      ansicht={ansicht}
      onAnsicht={(ziel) => setSuche({ tab: ziel }, { replace: true })}
    />
  );
}
