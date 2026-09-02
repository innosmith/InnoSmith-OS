/** Die Übergabe von der Datenschutz-Seite an den Chat.
 *
 * Ein eigener Baustein und keine Konstante in einer der beiden Seiten: Beide
 * werden getrennt nachgeladen (`lazy`), und ein Import quer würde die eine
 * Seite mitsamt ihrer Abhängigkeiten in das Bündel der anderen ziehen.
 *
 * sessionStorage und nicht Router-State, weil der Weg einen Reload überstehen
 * soll: Wer den maskierten Text in den Chat schickt und dort erst noch ein
 * Modell wählt, darf ihn nicht verlieren.
 */

export const UEBERGABE_SCHLUESSEL = 'datenschutz_uebergabe';

export interface Uebergabe {
  text: string;
  sessionId: string;
}

export function legeUebergabeAb(uebergabe: Uebergabe): void {
  try {
    sessionStorage.setItem(UEBERGABE_SCHLUESSEL, JSON.stringify(uebergabe));
  } catch {
    /* sessionStorage evtl. nicht verfügbar */
  }
}

/** Holt die Übergabe und räumt sie weg -- sie gilt genau einmal. */
export function nimmUebergabe(): Uebergabe | null {
  try {
    const roh = sessionStorage.getItem(UEBERGABE_SCHLUESSEL);
    if (!roh) return null;
    sessionStorage.removeItem(UEBERGABE_SCHLUESSEL);
    const gelesen = JSON.parse(roh) as Partial<Uebergabe>;
    if (typeof gelesen.text !== 'string' || typeof gelesen.sessionId !== 'string') return null;
    return { text: gelesen.text, sessionId: gelesen.sessionId };
  } catch {
    return null;
  }
}
