/**
 * Die Signale im Cockpit -- derselbe Leser, nur schmaler.
 *
 * Eingehängt wird `Leseansicht` aus `@signa/reader`: dasselbe Bauteil, das die
 * Signale-Seite zeigt. Nicht aus Bequemlichkeit, sondern weil eine zweite, «für das
 * Cockpit vereinfachte» Signalliste zwangsläufig auseinanderläuft -- ein neues Feld in
 * Signa erscheint dann an einer Stelle und an der anderen nicht. Die Kachel hier trägt
 * deshalb nur die Umrandung und den Verweis; alles Fachliche lebt in Signa.
 *
 * **Die Filter sind bewusst dieselben.** Der Leser legt seinen Stand unter einem
 * Schlüssel im localStorage ab, und dieser Stand gilt hier wie dort. Wer im Cockpit auf
 * «Neuste» stellt, findet es auf der Signale-Seite wieder -- eine Einstellung, zwei
 * Orte. Zwei getrennte Stände wären die häufigere Verwirrung: Man stellt etwas ein und
 * sieht es anderswo nicht.
 *
 * Schmal wird die Ansicht nicht durch eine Fensterabfrage, sondern durch eine
 * Containerabfrage im Paket (`@container sg`): Die Schiene ist genau dann schmal, wenn
 * das Fenster breit ist -- eine Fensterabfrage antwortete hier falsch.
 *
 * Die Höhe kommt von aussen (`className`). Der Leser scrollt selbst und braucht dafür
 * einen Rahmen mit fester Höhe; ohne den fällt sein Inhalt auf Null zusammen.
 */

import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Leseansicht } from '@signa/reader';
import '@signa/reader/styles.css';
import { useTheme } from '../contexts/ThemeContext';
import { erstelleZugang } from '../lib/signaZugang';

interface SignalstreifenCardProps {
  cardClass: string;
  textSecondary: string;
  hasBg: boolean;
  className?: string;
}

export function SignalstreifenCard({
  cardClass,
  textSecondary,
  hasBg,
  className = '',
}: SignalstreifenCardProps) {
  const navigate = useNavigate();
  const { resolved } = useTheme();

  // Einmal bauen, wie auf der Signale-Seite: Ein neuer Zugang je Darstellung würde bei
  // jedem Zustandswechsel des Cockpits die Leseliste neu laden.
  const zugang = useMemo(() => erstelleZugang(), []);

  return (
    <section
      className={`flex min-h-0 flex-col overflow-hidden rounded-xl border ${cardClass} ${className}`}
      data-testid="cockpit-signalstreifen"
    >
      <div className="flex shrink-0 items-center justify-between px-4 py-2.5">
        <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
          Signale
        </h2>
        <button
          onClick={() => navigate('/signale')}
          className={`text-xs font-medium ${hasBg ? 'text-white/60 hover:text-white' : 'text-indigo-600 hover:text-indigo-800 dark:text-indigo-400'}`}
          data-testid="cockpit-signalstreifen-oeffnen"
        >
          Alle Signale →
        </button>
      </div>

      {/* `min-h-0` ist die tragende Zeile: Ohne sie wächst das Flex-Kind über den Rahmen
          hinaus, statt innen zu scrollen -- und der Leser schneidet unten ab. */}
      <div className="min-h-0 flex-1">
        {/* Ohne `onSignalGeoeffnet`: Gelesen wird in der Karte, nicht auf einer
            anderen Seite. Der Leser klappt das Signal an Ort und Stelle auf. */}
        <Leseansicht api={zugang} theme={resolved} />
      </div>
    </section>
  );
}
