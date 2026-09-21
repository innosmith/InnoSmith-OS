/**
 * Prüfung des Rechnungslaufs — der Ersatz für den Excel-Bericht.
 *
 * Rein lesend. Sie beantwortet vor dem Versand eine einzige Frage: Welche
 * Entwürfe kann ich rausschicken, und was hält die übrigen zurück.
 *
 * Drei Entscheidungen, die aus den Mängeln des Excel-Berichts folgen:
 *
 * 1. **Jeder Befund nennt Erwartung und Istwert.** Im Excel stand eine
 *    Fehlerliste als Fliesstext, und die Einfärbung suchte darin nach
 *    Stichworten — ein Übertragsfehler färbte deshalb vier fremde Spalten rot.
 *    Hier gehört jeder Befund zu genau einem Feld.
 * 2. **«Offen» ist eine eigene Farbe, nicht grün.** Eine Regel, die mangels
 *    Daten nicht geprüft werden konnte, sah im Excel aus wie eine bestandene.
 * 3. **Was gar nicht geprüft wurde, steht zuoberst.** Ein Entwurf ohne Vertrag
 *    und ein Projekt ohne Entwurf tauchten im Excel nirgends auf — und
 *    ungeprüft sieht aus wie unbeanstandet.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight,
  CircleDashed, FileWarning, Minus, RefreshCw, XCircle,
} from 'lucide-react';
import { api } from '../../api/client';

type Zustand = 'richtig' | 'falsch' | 'offen' | 'entfaellt';
type Gewicht = 'fehler' | 'warnung' | 'hinweis';

interface Befund {
  regel: string;
  titel: string;
  feld: string;
  zustand: Zustand;
  gewicht: Gewicht;
  begruendung: string;
  erwartet: string | null;
  ist: string | null;
}

interface Rechnungspruefung {
  rechnung: string;
  rechnung_id: number | null;
  projekt: string;
  kunde: string;
  vertragsart: string;
  gesamt: Gewicht | null;
  versandbereit: boolean;
  befunde: Befund[];
  vermerk: Vermerk;
}

interface Aenderung {
  positionsart: string;
  position_id: number;
  handlung: string;
  feld: string;
  alt: string;
  neu: string;
  begruendung: string;
}

interface Vorschlag {
  rechnung: string;
  projekt: string;
  aenderungen: Aenderung[];
  hindernisse: string[];
  hinweise: string[];
}

/** Gemeinsam allen schreibenden Antworten: lief es trocken, und was wurde
 *  dabei angehalten. */
interface Trockenstand {
  trocken?: boolean;
  vermerke?: string[];
}

interface AnwendenErgebnis extends Trockenstand {
  periode: string;
  geschrieben: number;
  fehlgeschlagen: number;
  protokoll: { rechnung: string; erfolg: boolean; meldung: string }[];
  uebergangen: string[];
  zurueckgestellt?: string[];
  fehler?: string;
}

/** Was am Lauf vermerkt ist — getrennt vom Prüfergebnis, andere Quelle. */
interface Vermerk {
  zurueckgestellt: boolean;
  grund: string | null;
  dokumente_erzeugt_am: string | null;
  mailentwurf_id: string | null;
  versendet_am: string | null;
  abgelegt_am: string | null;
}

interface ErzeugenErgebnis extends Trockenstand {
  periode: string;
  stichtag: string;
  erzeugt: number;
  gescheitert: number;
  protokoll: { titel: string; nummer: string; datum: string | null; hindernis: string }[];
  fehler?: string;
}

interface MailErgebnis extends Trockenstand {
  periode: string;
  angelegt: number;
  uebergangen: number;
  fehlgeschlagen: number;
  protokoll: { rechnung: string; hindernis: string; mailentwurf_id: string }[];
  zurueckgestellt: string[];
  fehler?: string;
}

interface AusstellenErgebnis extends Trockenstand {
  periode: string;
  ausgestellt: number;
  uebergangen: number;
  fehlgeschlagen: number;
  protokoll: { rechnung: string; ausgestellt: boolean; hindernis: string }[];
  fehler?: string;
}

interface AblageDatei {
  pfad: string;
  bytes: number;
  lag_schon: boolean;
  hindernis: string;
}

interface AblegenErgebnis extends Trockenstand {
  periode: string;
  abgelegt: number;
  uebergangen: number;
  fehlgeschlagen: number;
  protokoll: { rechnung: string; ordner: string; dateien: AblageDatei[]; hindernis: string }[];
  fehler?: string;
}

interface Verrechnungszeile {
  eintrag_id: number;
  datum: string;
  projekt: string;
  stunden: number;
  art: string;
  grund: string;
  vorhanden: string[];
  gesetzt: boolean;
  hindernis: string;
}

interface VerrechnungsartErgebnis extends Trockenstand {
  periode: string;
  gesetzt: number;
  schon_richtig: number;
  fehlgeschlagen: number;
  stunden_gesetzt: number;
  protokoll: Verrechnungszeile[];
  fremd: Verrechnungszeile[];
  ohne_zuordnung: Verrechnungszeile[];
  uebergangen: string[];
  hindernis: string;
  /** Nur für die Anzeige: welcher der beiden Knöpfe den Lauf ausgelöst hat. */
  intern?: boolean;
  fehler?: string;
}

export interface PruefungData {
  periode: string;
  regeln: string[];
  versandbereit: number;
  blockiert: number;
  rechnungen: Rechnungspruefung[];
  ohne_vertrag: Record<string, unknown>[];
  ohne_entwurf: Record<string, unknown>[];
  ausserhalb: Record<string, unknown>[];
  intern: string[];
  auftragsluecken: Record<string, unknown>[];
  auffaelligkeiten: Record<string, string[]>;
  vorschlaege: Vorschlag[];
  uebertrag: Record<string, number>;
  uebertrag_herkunft: Record<string, string>;
  uebertrag_hinweise: string[];
  stammdaten: string[];
  fehler: string[];
}

interface Props {
  month: string;
  sectionClass: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  hasBg: boolean;
}

const VERTRAGSART_TEXT: Record<string, string> = {
  fix_uebertragbar: 'Fixstunden, übertragbar',
  fix_verfallend: 'Fixstunden, verfallend',
  variabel: 'Nach Aufwand',
  festpreis: 'Festpreis',
};

const ZUSTAND_TEXT: Record<Zustand, string> = {
  richtig: 'stimmt',
  falsch: 'falsch',
  offen: 'nicht prüfbar',
  entfaellt: 'entfällt',
};

/** Ein Zustand, eine Farbe, ein Zeichen — Farbe allein trägt keine Information. */
function ZustandsZeichen({ zustand, className = 'h-4 w-4' }: { zustand: Zustand; className?: string }) {
  switch (zustand) {
    case 'richtig': return <CheckCircle2 className={`${className} text-emerald-500`} />;
    case 'falsch': return <XCircle className={`${className} text-red-500`} />;
    case 'offen': return <CircleDashed className={`${className} text-amber-500`} />;
    default: return <Minus className={`${className} text-gray-400`} />;
  }
}

/** Der zweite Klick.
 *
 *  Erscheint nur nach einem Trockenlauf und nennt die angehaltenen Aufrufe im
 *  Wortlaut. Eine Zusammenfassung («3 Schreibzugriffe») wäre wertlos: geprüft
 *  werden soll, *was* geschähe, nicht *wie viel*.
 */
function Trockenbalken({ stand, ausfuehren, laeuft, beschriftung }: {
  stand: Trockenstand;
  ausfuehren: () => void;
  laeuft: boolean;
  beschriftung: string;
}) {
  if (!stand.trocken) return null;
  const vermerke = stand.vermerke ?? [];
  return (
    <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-2 text-sm dark:border-amber-700/60 dark:bg-amber-900/20">
      <p className="text-amber-900 dark:text-amber-200">
        Trockenlauf — gerechnet mit den echten Daten, geschrieben wurde nichts.
      </p>
      {vermerke.length > 0 && (
        <ul className="mt-1 space-y-0.5 font-mono text-xs text-amber-800 dark:text-amber-300/80">
          {vermerke.map((v, i) => <li key={`${i}-${v}`}>{v}</li>)}
        </ul>
      )}
      <button
        onClick={ausfuehren}
        disabled={laeuft}
        className="mt-2 rounded-lg bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 disabled:opacity-50"
      >
        {laeuft ? 'führt aus …' : beschriftung}
      </button>
    </div>
  );
}

function monatLang(periode: string): string {
  const [j, m] = periode.split('-').map(Number);
  if (!j || !m) return periode;
  return new Date(j, m - 1, 1).toLocaleDateString('de-CH', { month: 'long', year: 'numeric' });
}

export function DebtorsPruefung({ month, sectionClass, textPrimary, textSecondary, textMuted, hasBg }: Props) {
  const [offen, setOffen] = useState<string | null>(null);
  const [versuch, setVersuch] = useState(0);
  const [stand, setStand] = useState<{ schluessel: string; data: PruefungData | null; fehler: string | null } | null>(null);

  // Der Ladezustand wird **abgeleitet**, nicht gesetzt: er ist wahr, solange
  // das Vorliegende nicht zum Angefragten gehört. Damit steht beim
  // Monatswechsel nie kurz das Ergebnis des alten Monats unter der neuen
  // Überschrift -- und der Effekt bleibt frei von synchronem setState.
  const schluessel = `${month}#${versuch}`;
  const laedt = stand?.schluessel !== schluessel;
  const data = stand?.data ?? null;
  const error = stand?.fehler ?? null;

  const laden = useCallback(() => setVersuch(v => v + 1), []);

  // Ob der erste Druck zeigt oder ausführt. Die Sicherung liegt nicht hier,
  // sondern im Vorgabewert der Endpunkte — dort schreibt ein Aufruf ohne
  // ``echt`` grundsätzlich nichts. Diese Einstellung bestimmt allein, ob es
  // zwei Klicks braucht oder einen. Bis sie geladen ist, gilt «zeigen»: der
  // vorsichtige Zustand ist der richtige, solange nichts bekannt ist.
  const [erstZeigen, setErstZeigen] = useState(true);
  useEffect(() => {
    api.get<{ debitoren_trockenlauf?: boolean | null }>('/api/settings')
      .then(s => setErstZeigen(s.debitoren_trockenlauf ?? true))
      .catch(() => setErstZeigen(true));
  }, []);

  // Anwenden schickt **nur** den Monat: welche Menge wohin gehört, rechnet das
  // Backend aus denselben Daten neu, aus denen die Anzeige oben entstand.
  const [schreibt, setSchreibt] = useState(false);
  const [protokoll, setProtokoll] = useState<AnwendenErgebnis | null>(null);
  const anwenden = useCallback((echt = !erstZeigen) => {
    setSchreibt(true);
    setProtokoll(null);
    api.post<AnwendenErgebnis>('/api/debtors/pruefung/anwenden', { month, echt })
      .then(p => { setProtokoll(p); laden(); })
      .catch((e: unknown) => setProtokoll({
        periode: month, geschrieben: 0, fehlgeschlagen: 0, protokoll: [],
        uebergangen: [], fehler: e instanceof Error ? e.message : 'Schreiben fehlgeschlagen',
      }))
      .finally(() => setSchreibt(false));
  }, [month, laden, erstZeigen]);

  // Erzeugen schickt ebenfalls nur den Monat. Welche Aufträge fällig sind,
  // entscheidet das Backend aus Takt und Vertragszustand — der Browser kennt
  // keine Auftragskennung.
  const [erzeugt, setErzeugt] = useState(false);
  const [erzeugtProtokoll, setErzeugtProtokoll] = useState<ErzeugenErgebnis | null>(null);
  const entwuerfeErzeugen = useCallback((echt = !erstZeigen) => {
    setErzeugt(true);
    setErzeugtProtokoll(null);
    api.post<ErzeugenErgebnis>('/api/debtors/pruefung/erzeugen', { month, echt })
      .then(p => { setErzeugtProtokoll(p); laden(); })
      .catch((e: unknown) => setErzeugtProtokoll({
        periode: month, stichtag: '', erzeugt: 0, gescheitert: 0, protokoll: [],
        fehler: e instanceof Error ? e.message : 'Erzeugen fehlgeschlagen',
      }))
      .finally(() => setErzeugt(false));
  }, [month, laden, erstZeigen]);

  // Zurückstellen und Wiederaufnehmen. Danach wird neu geladen, statt den
  // Vermerk lokal zu setzen: sonst zeigte die Ansicht einen Zustand, den der
  // Server womöglich anders verbucht hat.
  const [vermerkt, setVermerkt] = useState<number | null>(null);
  const [vermerkFehler, setVermerkFehler] = useState<Record<number, string>>({});
  const [fragtNach, setFragtNach] = useState<number | null>(null);
  const [grund, setGrund] = useState('');
  const vermerken = useCallback(
    (weg: 'zuruecklegen' | 'zurueckholen', rechnungId: number, begruendung?: string) => {
      setVermerkt(rechnungId);
      setVermerkFehler(f => ({ ...f, [rechnungId]: '' }));
      api.post(`/api/debtors/pruefung/${weg}`, {
        month, rechnung_id: rechnungId, grund: begruendung,
      })
        .then(() => { setFragtNach(null); laden(); })
        .catch((e: unknown) => setVermerkFehler(f => ({
          ...f,
          [rechnungId]: e instanceof Error ? e.message : 'Vermerk fehlgeschlagen',
        })))
        .finally(() => setVermerkt(null));
    },
    [month, laden],
  );

  const [schreibtMail, setSchreibtMail] = useState(false);
  const [mailProtokoll, setMailProtokoll] = useState<MailErgebnis | null>(null);
  const mailentwuerfe = useCallback((echt = !erstZeigen) => {
    setSchreibtMail(true);
    setMailProtokoll(null);
    api.post<MailErgebnis>('/api/debtors/pruefung/mailentwuerfe', { month, echt })
      .then(p => { setMailProtokoll(p); laden(); })
      .catch((e: unknown) => setMailProtokoll({
        periode: month, angelegt: 0, uebergangen: 0, fehlgeschlagen: 0,
        protokoll: [], zurueckgestellt: [],
        fehler: e instanceof Error ? e.message : 'Mailentwürfe fehlgeschlagen',
      }))
      .finally(() => setSchreibtMail(false));
  }, [month, laden, erstZeigen]);

  const versendetBestaetigen = useCallback((rechnungId: number) => {
    setVermerkt(rechnungId);
    api.post(`/api/debtors/pruefung/versendet`, { month, rechnung_id: rechnungId })
      .then(() => laden())
      .catch((e: unknown) => setVermerkFehler(f => ({
        ...f,
        [rechnungId]: e instanceof Error ? e.message : 'Bestätigung fehlgeschlagen',
      })))
      .finally(() => setVermerkt(null));
  }, [month, laden]);

  const [stelltAus, setStelltAus] = useState(false);
  const [ausstellenProtokoll, setAusstellenProtokoll] = useState<AusstellenErgebnis | null>(null);
  const ausstellen = useCallback((echt = !erstZeigen) => {
    setStelltAus(true);
    setAusstellenProtokoll(null);
    api.post<AusstellenErgebnis>('/api/debtors/pruefung/ausstellen', { month, echt })
      .then(p => { setAusstellenProtokoll(p); laden(); })
      .catch((e: unknown) => setAusstellenProtokoll({
        periode: month, ausgestellt: 0, uebergangen: 0, fehlgeschlagen: 0,
        protokoll: [],
        fehler: e instanceof Error ? e.message : 'Ausstellen fehlgeschlagen',
      }))
      .finally(() => setStelltAus(false));
  }, [month, laden, erstZeigen]);

  const [legtAb, setLegtAb] = useState(false);
  const [ablageProtokoll, setAblageProtokoll] = useState<AblegenErgebnis | null>(null);
  const ablegen = useCallback((echt = !erstZeigen) => {
    setLegtAb(true);
    setAblageProtokoll(null);
    api.post<AblegenErgebnis>('/api/debtors/pruefung/ablegen', { month, echt })
      .then(p => { setAblageProtokoll(p); laden(); })
      .catch((e: unknown) => setAblageProtokoll({
        periode: month, abgelegt: 0, uebergangen: 0, fehlgeschlagen: 0,
        protokoll: [],
        fehler: e instanceof Error ? e.message : 'Ablage fehlgeschlagen',
      }))
      .finally(() => setLegtAb(false));
  }, [month, laden, erstZeigen]);

  const [taggt, setTaggt] = useState(false);
  const [artProtokoll, setArtProtokoll] = useState<VerrechnungsartErgebnis | null>(null);
  const verrechnungsart = useCallback((intern = false, echt = !erstZeigen) => {
    setTaggt(true);
    setArtProtokoll(null);
    api.post<VerrechnungsartErgebnis>('/api/debtors/pruefung/verrechnungsart', { month, intern, echt })
      .then(p => { setArtProtokoll({ ...p, intern }); laden(); })
      .catch((e: unknown) => setArtProtokoll({
        periode: month, gesetzt: 0, schon_richtig: 0, fehlgeschlagen: 0,
        stunden_gesetzt: 0, protokoll: [], fremd: [], ohne_zuordnung: [],
        uebergangen: [], hindernis: '', intern,
        fehler: e instanceof Error ? e.message : 'Verrechnungsart fehlgeschlagen',
      }))
      .finally(() => setTaggt(false));
  }, [month, laden, erstZeigen]);

  const dokumentOeffnen = useCallback((rechnungId: number) => {
    api.blob(`/api/debtors/pruefung/dokument/${rechnungId}?month=${month}`)
      .then(blob => {
        const url = URL.createObjectURL(blob);
        window.open(url, '_blank', 'noopener');
      })
      .catch((e: unknown) => setVermerkFehler(f => ({
        ...f,
        [rechnungId]: e instanceof Error ? e.message : 'PDF nicht lesbar',
      })));
  }, [month]);

  useEffect(() => {
    let verworfen = false;
    api.get<PruefungData>(`/api/debtors/pruefung?month=${month}`)
      .then(d => { if (!verworfen) setStand({ schluessel, data: d, fehler: null }); })
      .catch((e: unknown) => {
        if (verworfen) return;
        setStand({
          schluessel,
          data: null,
          fehler: e instanceof Error ? e.message : 'Prüfung konnte nicht geladen werden',
        });
      });
    return () => { verworfen = true; };
  }, [month, schluessel]);

  if (laedt) {
    return (
      <div className={`flex items-center justify-center py-20 ${sectionClass}`}>
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-indigo-200 border-t-indigo-600" />
      </div>
    );
  }

  if (error) {
    return (
      <div className={`${sectionClass}`}>
        <div className="flex items-start gap-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-red-500" />
          <div>
            <p className={`text-sm font-semibold ${textPrimary}`}>Prüfung nicht möglich</p>
            <p className={`mt-1 text-sm ${textSecondary}`}>{error}</p>
            <button onClick={laden} className={`mt-3 flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>
              <RefreshCw className="h-4 w-4" /> Erneut versuchen
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (!data) return null;

  const nichtsGeprueft =
    data.ohne_vertrag.length +
    data.ohne_entwurf.length +
    data.ausserhalb.length +
    (data.auftragsluecken?.length ?? 0);

  // Nur diese eine Art führt zu einem Entwurf. Ein Auftrag ohne Vertrag oder
  // ein Auftrag, der nach Vertragsende weiterläuft, verlangt eine Entscheidung
  // und keine Rechnung.
  const fehlendeEntwuerfe = (data.auftragsluecken ?? [])
    .filter(e => e.art === 'auftrag_ohne_rechnung').length;

  return (
    <div className="space-y-4">
      {/* Kopfzeile: die eine Zahl, die zählt */}
      <div className={sectionClass}>
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <div>
            <h2 className={`text-lg font-semibold ${textPrimary}`}>
              Leistungsmonat {monatLang(data.periode)}
            </h2>
            <p className={`mt-1 text-xs ${textMuted}`}>
              Entwürfe, Positionen und Stunden live aus Bexio und Toggl
            </p>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-right">
              <div className="text-2xl font-bold tabular-nums text-emerald-500">{data.versandbereit}</div>
              <div className={`text-[11px] ${textMuted}`}>versandbereit</div>
            </div>
            <div className="text-right">
              <div className={`text-2xl font-bold tabular-nums ${data.blockiert > 0 ? 'text-red-500' : textMuted}`}>{data.blockiert}</div>
              <div className={`text-[11px] ${textMuted}`}>zurückgehalten</div>
            </div>
          </div>
        </div>
      </div>

      {/* Abrufehler zuerst: ein Teilausfall darf nicht wie «nichts gefunden» aussehen */}
      {data.fehler.length > 0 && (
        <div className={`${sectionClass} border-l-4 border-l-red-500`}>
          <p className={`mb-2 flex items-center gap-2 text-sm font-semibold ${textPrimary}`}>
            <AlertTriangle className="h-4 w-4 text-red-500" />
            Nicht alle Daten konnten geholt werden
          </p>
          <ul className={`space-y-1 text-sm ${textSecondary}`}>
            {data.fehler.map((f, i) => <li key={i}>· {f}</li>)}
          </ul>
          <p className={`mt-2 text-xs ${textMuted}`}>
            Die Prüfung unten beruht auf einem unvollständigen Bestand.
          </p>
        </div>
      )}

      {/* Was gar nicht geprüft wurde — im Excel fehlte das vollständig */}
      {nichtsGeprueft > 0 && (
        <div className={`${sectionClass} border-l-4 border-l-amber-500`}>
          <p className={`mb-3 flex items-center gap-2 text-sm font-semibold ${textPrimary}`}>
            <FileWarning className="h-4 w-4 text-amber-500" />
            {nichtsGeprueft} Posten ausserhalb der Prüfung
          </p>
          <div className="space-y-3 text-sm">
            {data.ohne_vertrag.length > 0 && (
              <div>
                <p className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Entwurf ohne hinterlegten Vertrag</p>
                <ul className={`mt-1 space-y-1 ${textSecondary}`}>
                  {data.ohne_vertrag.map((e, i) => (
                    <li key={i}>
                      <span className={textPrimary}>{String(e.nummer ?? '—')}</span>
                      {' · '}{String(e.titel ?? '')}
                      {e.kunde ? ` · ${String(e.kunde)}` : ''}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data.ohne_entwurf.length > 0 && (
              <div>
                <p className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Stunden erfasst, kein Entwurf</p>
                <ul className={`mt-1 space-y-1 ${textSecondary}`}>
                  {data.ohne_entwurf.map((e, i) => (
                    <li key={i}>
                      <span className={textPrimary}>{String(e.projekt ?? '')}</span>
                      {' · '}{Number(e.stunden ?? 0).toLocaleString('de-CH')} h
                      {' · '}{String(e.grund ?? '')}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data.auftragsluecken?.length > 0 && (
              <div>
                <div className="flex items-start justify-between gap-3">
                  <p className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Was die Bexio-Aufträge sagen</p>
                  {fehlendeEntwuerfe > 0 && (
                    <button
                      onClick={() => entwuerfeErzeugen()}
                      disabled={erzeugt}
                      className={`shrink-0 rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
                    >
                      {erzeugt
                        ? 'erzeugt …'
                        : `${fehlendeEntwuerfe} Entwurf${fehlendeEntwuerfe === 1 ? '' : 'e'} in Bexio anlegen`}
                    </button>
                  )}
                </div>
                {erzeugtProtokoll && (
                  <p className={`mt-1 text-sm ${textSecondary}`}>
                    {erzeugtProtokoll.fehler
                      ? erzeugtProtokoll.fehler
                      : `${erzeugtProtokoll.erzeugt} Entwurf/Entwürfe angelegt, datiert auf ${erzeugtProtokoll.stichtag}`
                        + (erzeugtProtokoll.gescheitert
                            ? `, ${erzeugtProtokoll.gescheitert} ohne Erfolg: `
                              + erzeugtProtokoll.protokoll.filter(p => p.hindernis)
                                  .map(p => `${p.titel} (${p.hindernis})`).join(', ')
                            : '.')}
                  </p>
                )}
                {erzeugtProtokoll && !erzeugtProtokoll.fehler && (
                  <Trockenbalken
                    stand={erzeugtProtokoll} laeuft={erzeugt}
                    ausfuehren={() => entwuerfeErzeugen(true)}
                    beschriftung="Entwürfe jetzt wirklich in Bexio anlegen"
                  />
                )}
                <ul className={`mt-1 space-y-1 ${textSecondary}`}>
                  {data.auftragsluecken.map((e, i) => (
                    <li key={i}>
                      <span className={textPrimary}>{String(e.titel ?? '')}</span>
                      {e.auftrag ? ` · ${String(e.auftrag)}` : ''}
                      {' · '}{String(e.grund ?? '')}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data.ausserhalb.length > 0 && (
              <div>
                <p className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Entwurf in einem anderen Monat datiert</p>
                <ul className={`mt-1 space-y-1 ${textSecondary}`}>
                  {data.ausserhalb.map((e, i) => (
                    <li key={i}>
                      <span className={textPrimary}>{String(e.nummer ?? '—')}</span>
                      {' · '}{String(e.titel ?? '')}{' · '}{String(e.datum ?? '')}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Die Rechnungen */}
      {data.rechnungen.length === 0 ? (
        <div className={`${sectionClass} py-12 text-center`}>
          <p className={`text-sm ${textSecondary}`}>Keine Entwürfe für diesen Leistungsmonat.</p>
        </div>
      ) : (
        <div className={`${sectionClass} p-0 overflow-hidden`}>
          {data.rechnungen.map(r => {
            const aufgeklappt = offen === r.rechnung;
            const hinweise = data.auffaelligkeiten[r.rechnung] ?? [];
            return (
              <div key={r.rechnung} className={`border-b last:border-b-0 ${hasBg ? 'border-white/10' : 'border-gray-100 dark:border-gray-700/50'}`}>
                <button
                  onClick={() => setOffen(aufgeklappt ? null : r.rechnung)}
                  className={`flex w-full items-center gap-3 px-4 py-3 text-left transition-colors sm:px-6 ${hasBg ? 'hover:bg-white/5' : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'}`}
                >
                  {aufgeklappt
                    ? <ChevronDown className={`h-4 w-4 shrink-0 ${textMuted}`} />
                    : <ChevronRight className={`h-4 w-4 shrink-0 ${textMuted}`} />}
                  {r.versandbereit
                    ? <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-500" />
                    : <XCircle className="h-5 w-5 shrink-0 text-red-500" />}
                  <div className="min-w-0 flex-1">
                    <div className={`flex items-center gap-2 truncate text-sm font-medium ${textPrimary}`}>
                      <span className={r.vermerk?.zurueckgestellt ? 'line-through opacity-60' : ''}>{r.projekt}</span>
                      {r.vermerk?.zurueckgestellt && (
                        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${hasBg ? 'bg-amber-400/20 text-amber-200' : 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'}`}>
                          zurückgestellt
                        </span>
                      )}
                    </div>
                    <div className={`text-[11px] ${textMuted}`}>
                      {r.rechnung || 'ohne Nummer'} · {r.kunde} · {VERTRAGSART_TEXT[r.vertragsart] ?? r.vertragsart}
                      {r.vermerk?.grund && r.vermerk.zurueckgestellt ? ` · ${r.vermerk.grund}` : ''}
                      {r.vermerk?.mailentwurf_id && !r.vermerk.versendet_am ? ' · Mailentwurf im Postfach' : ''}
                      {r.vermerk?.versendet_am ? ' · Versand bestätigt' : ''}
                      {r.vermerk?.abgelegt_am ? ' · im Kundenarchiv' : ''}
                    </div>
                  </div>
                  {/* Eine Marke je Regel: die Zeile zeigt, wo es hakt, ohne Aufklappen */}
                  <div className="hidden shrink-0 items-center gap-1 sm:flex">
                    {r.befunde.map(b => (
                      <span key={b.regel} title={`${b.titel}: ${ZUSTAND_TEXT[b.zustand]}`}>
                        <ZustandsZeichen zustand={b.zustand} className="h-3.5 w-3.5" />
                      </span>
                    ))}
                  </div>
                </button>

                {aufgeklappt && (
                  <div className={`px-4 pb-4 sm:px-6 ${hasBg ? 'bg-black/20' : 'bg-gray-50/60 dark:bg-gray-900/30'}`}>
                    <table className="w-full text-sm">
                      <thead>
                        <tr className={`text-left text-[11px] uppercase tracking-wider ${textMuted}`}>
                          <th className="py-2 pr-3 font-semibold">Prüfung</th>
                          <th className="py-2 pr-3 font-semibold">Erwartet</th>
                          <th className="py-2 pr-3 font-semibold">Auf der Rechnung</th>
                          <th className="py-2 font-semibold">Befund</th>
                        </tr>
                      </thead>
                      <tbody>
                        {r.befunde.map(b => (
                          <tr key={b.regel} className={`align-top ${hasBg ? 'border-t border-white/5' : 'border-t border-gray-200/70 dark:border-gray-700/40'}`}>
                            <td className="py-2 pr-3">
                              <span className="flex items-center gap-2">
                                <ZustandsZeichen zustand={b.zustand} />
                                <span className={textPrimary}>{b.titel}</span>
                              </span>
                            </td>
                            <td className={`py-2 pr-3 tabular-nums ${textSecondary}`}>{b.erwartet ?? '–'}</td>
                            <td className={`py-2 pr-3 tabular-nums ${b.zustand === 'falsch' ? 'font-semibold text-red-500' : textSecondary}`}>
                              {b.ist ?? '–'}
                            </td>
                            <td className={`py-2 ${textSecondary}`}>{b.begruendung}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>

                    {hinweise.length > 0 && (
                      <div className="mt-3">
                        <p className={`text-xs font-semibold uppercase tracking-wider ${textMuted}`}>Beim Lesen der Positionen</p>
                        <ul className={`mt-1 space-y-1 text-sm ${textSecondary}`}>
                          {hinweise.map((h, i) => <li key={i}>· {h}</li>)}
                        </ul>
                      </div>
                    )}

                    {/* Zurückstellen ist die einzige vorgesehene Ausnahme —
                        und sie verlangt eine Begründung, weil sie sonst in
                        drei Wochen wie ein Versehen aussieht. */}
                    {r.rechnung_id != null && (
                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        <button
                          onClick={() => dokumentOeffnen(r.rechnung_id!)}
                          className={`rounded-lg px-3 py-1.5 text-sm font-medium ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
                        >
                          PDF
                        </button>
                        {r.vermerk?.mailentwurf_id && !r.vermerk.versendet_am && !r.vermerk.zurueckgestellt && (
                          <button
                            onClick={() => versendetBestaetigen(r.rechnung_id!)}
                            disabled={vermerkt === r.rechnung_id}
                            className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
                          >
                            Versand bestätigt
                          </button>
                        )}
                        {r.vermerk?.zurueckgestellt ? (
                          <>
                            <span className={`text-sm ${textSecondary}`}>
                              Zurückgestellt: {r.vermerk.grund}
                            </span>
                            <button
                              onClick={() => vermerken('zurueckholen', r.rechnung_id!)}
                              disabled={vermerkt === r.rechnung_id}
                              className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
                            >
                              Wieder aufnehmen
                            </button>
                          </>
                        ) : fragtNach === r.rechnung_id ? (
                          <>
                            <input
                              autoFocus
                              value={grund}
                              onChange={e => setGrund(e.target.value)}
                              onKeyDown={e => {
                                if (e.key === 'Enter' && grund.trim()) {
                                  vermerken('zuruecklegen', r.rechnung_id!, grund);
                                }
                                if (e.key === 'Escape') setFragtNach(null);
                              }}
                              placeholder="Warum bleibt diese Rechnung liegen?"
                              className={`min-w-0 flex-1 rounded-lg px-3 py-1.5 text-sm ${hasBg ? 'bg-white/10 text-white placeholder-white/40' : 'border border-gray-200 bg-white text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100'}`}
                            />
                            <button
                              onClick={() => vermerken('zuruecklegen', r.rechnung_id!, grund)}
                              disabled={!grund.trim() || vermerkt === r.rechnung_id}
                              className={`shrink-0 rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-40 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
                            >
                              Zurückstellen
                            </button>
                            <button
                              onClick={() => setFragtNach(null)}
                              className={`shrink-0 text-sm ${textMuted} hover:underline`}
                            >
                              Abbrechen
                            </button>
                          </>
                        ) : (
                          <button
                            onClick={() => { setGrund(''); setFragtNach(r.rechnung_id); }}
                            disabled={vermerkt === r.rechnung_id}
                            className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
                          >
                            Zurückstellen
                          </button>
                        )}
                        {vermerkFehler[r.rechnung_id] && (
                          <span className="text-sm text-red-500">{vermerkFehler[r.rechnung_id]}</span>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Die letzten drei Schritte: Dokument + Mailentwurf, Versand
          bestätigen, in Bexio ausstellen. Senden tut ein Mensch im Postfach. */}
      {data.rechnungen.length > 0 && (
        <div className={sectionClass}>
          <p className={`mb-2 text-sm font-semibold ${textPrimary}`}>Versand</p>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => mailentwuerfe()}
              disabled={schreibtMail}
              className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
            >
              {schreibtMail ? 'legt Entwürfe an …' : 'Mailentwürfe ins Postfach legen'}
            </button>
            {data.rechnungen.some(r => r.vermerk?.versendet_am && !r.vermerk.zurueckgestellt) && (
              <button
                onClick={() => ausstellen()}
                disabled={stelltAus}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
              >
                {stelltAus ? 'stellt aus …' : 'Bestätigt Versendete in Bexio ausstellen'}
              </button>
            )}
            {data.rechnungen.some(r => r.vermerk?.versendet_am && !r.vermerk.abgelegt_am
                                       && !r.vermerk.zurueckgestellt) && (
              <button
                onClick={() => ablegen()}
                disabled={legtAb}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
              >
                {legtAb ? 'legt ab …' : 'Ins Kundenarchiv ablegen'}
              </button>
            )}
            {/* Zuletzt und nur nach bestätigtem Versand: bis dahin ist Toggl
                die Beweisgrundlage, auf die im Zweifel zurückgegriffen wird. */}
            {data.rechnungen.some(r => r.vermerk?.versendet_am && !r.vermerk.zurueckgestellt) && (
              <button
                onClick={() => verrechnungsart()}
                disabled={taggt}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
              >
                {taggt ? 'setzt Tags …' : 'Verrechnungsart in Toggl nachtragen'}
              </button>
            )}
            {/* Die eigenen Projekte hängen an keiner Rechnung, darum ein
                eigener Knopf — und er erscheint erst, wenn nichts offen ist. */}
            {data.rechnungen.length > 0
             && data.rechnungen.every(r => r.vermerk?.versendet_am || r.vermerk?.zurueckgestellt) && (
              <button
                onClick={() => verrechnungsart(true)}
                disabled={taggt}
                className={`rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
              >
                {taggt ? 'setzt Tags …' : 'Auch die eigenen Projekte abschliessen'}
              </button>
            )}
          </div>
          {mailProtokoll && (
            <p className={`mt-2 text-sm ${textSecondary}`}>
              {mailProtokoll.fehler
                ? mailProtokoll.fehler
                : `${mailProtokoll.angelegt} Mailentwurf/Mailentwürfe angelegt`
                  + (mailProtokoll.fehlgeschlagen ? `, ${mailProtokoll.fehlgeschlagen} ohne Erfolg` : '')
                  + (mailProtokoll.zurueckgestellt.length
                      ? `. Unberührt, weil zurückgestellt: ${mailProtokoll.zurueckgestellt.join(', ')}`
                      : '.')
                  + ' Senden bleibt eine Handlung im Postfach.'}
            </p>
          )}
          {mailProtokoll && !mailProtokoll.fehler && (
            <Trockenbalken
              stand={mailProtokoll} laeuft={schreibtMail}
              ausfuehren={() => mailentwuerfe(true)}
              beschriftung="Entwürfe jetzt wirklich anlegen"
            />
          )}
          {ausstellenProtokoll && (
            <p className={`mt-2 text-sm ${textSecondary}`}>
              {ausstellenProtokoll.fehler
                ? ausstellenProtokoll.fehler
                : `${ausstellenProtokoll.ausgestellt} Rechnung/Rechnungen ausgestellt`
                  + (ausstellenProtokoll.fehlgeschlagen
                      ? `, ${ausstellenProtokoll.fehlgeschlagen} nicht: `
                        + ausstellenProtokoll.protokoll.filter(p => !p.ausgestellt && p.hindernis)
                            .map(p => `${p.rechnung} (${p.hindernis})`).join(', ')
                      : '.')}
            </p>
          )}
          {ausstellenProtokoll && !ausstellenProtokoll.fehler && (
            <Trockenbalken
              stand={ausstellenProtokoll} laeuft={stelltAus}
              ausfuehren={() => ausstellen(true)}
              beschriftung="Jetzt wirklich in Bexio ausstellen"
            />
          )}
          {ablageProtokoll && (
            <div className={`mt-2 text-sm ${textSecondary}`}>
              {ablageProtokoll.fehler ? ablageProtokoll.fehler : (
                <>
                  <p>
                    {`${ablageProtokoll.abgelegt} Rechnung/Rechnungen im Kundenarchiv`}
                    {ablageProtokoll.fehlgeschlagen
                      ? `, ${ablageProtokoll.fehlgeschlagen} nicht.`
                      : '.'}
                  </p>
                  {/* Der Pfad steht da, weil «abgelegt» ohne Ort keine Auskunft
                      ist: nachsehen können soll man, ohne OneDrive zu durchsuchen. */}
                  <ul className={`mt-1 space-y-0.5 text-xs ${textMuted}`}>
                    {ablageProtokoll.protokoll.map(p => (
                      <li key={p.rechnung || p.hindernis}>
                        <span className="font-medium">{p.rechnung}</span>
                        {p.hindernis
                          ? ` — ${p.hindernis}`
                          : p.dateien.map(d => (
                              <span key={d.pfad} className="block pl-4">
                                {d.pfad.split('/').pop()}
                                {d.hindernis
                                  ? ` — ${d.hindernis}`
                                  : d.lag_schon ? ' — lag schon da' : ''}
                              </span>
                            ))}
                        {!p.hindernis && p.ordner && (
                          <span className="block pl-4 opacity-70">{p.ordner}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          )}
          {ablageProtokoll && !ablageProtokoll.fehler && (
            <Trockenbalken
              stand={ablageProtokoll} laeuft={legtAb}
              ausfuehren={() => ablegen(true)}
              beschriftung="Jetzt wirklich ins Archiv legen"
            />
          )}
          {artProtokoll && (
            <div className={`mt-2 text-sm ${textSecondary}`}>
              {artProtokoll.fehler ? artProtokoll.fehler : (
                <>
                  <p>
                    {`${artProtokoll.gesetzt} Zeiteintrag/Zeiteinträge getaggt`}
                    {artProtokoll.stunden_gesetzt ? ` (${artProtokoll.stunden_gesetzt} h)` : ''}
                    {artProtokoll.schon_richtig ? `, ${artProtokoll.schon_richtig} trugen es schon` : ''}
                    {artProtokoll.fehlgeschlagen ? `, ${artProtokoll.fehlgeschlagen} nicht.` : '.'}
                  </p>
                  {artProtokoll.hindernis && (
                    <p className="mt-1 text-amber-600 dark:text-amber-400">{artProtokoll.hindernis}</p>
                  )}
                  {/* Je Art eine Zeile statt je Eintrag: bei achtzig Buchungen
                      im Monat ist die Einzelliste keine Auskunft mehr. */}
                  <ul className={`mt-1 space-y-0.5 text-xs ${textMuted}`}>
                    {Object.entries(
                      artProtokoll.protokoll.reduce<Record<string, number>>((k, z) => {
                        const schluessel = z.gesetzt ? z.art : `${z.art} — ${z.hindernis}`;
                        k[schluessel] = (k[schluessel] ?? 0) + 1;
                        return k;
                      }, {}),
                    ).map(([art, anzahl]) => (
                      <li key={art}>{anzahl}× {art}</li>
                    ))}
                  </ul>
                  {/* Ein fremder Tag wird nie überschrieben — er ist eine
                      Entscheidung. Beide Werte stehen da, damit man sie
                      vergleichen kann, ohne Toggl zu öffnen. */}
                  {artProtokoll.fremd.length > 0 && (
                    <div className="mt-2">
                      <p>Trägt schon eine andere Art — unangetastet:</p>
                      <ul className={`mt-1 space-y-0.5 text-xs ${textMuted}`}>
                        {artProtokoll.fremd.map(z => (
                          <li key={z.eintrag_id}>
                            {z.datum} · {z.projekt} · {z.stunden} h — steht auf
                            «{z.vorhanden.join(', ')}», erwartet «{z.art}»
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {artProtokoll.ohne_zuordnung.length > 0 && (
                    <div className="mt-2">
                      <p className="text-amber-600 dark:text-amber-400">
                        Ohne Zuordnung — weder Vertrag noch als eigenes Projekt
                        deklariert. Wird nicht getaggt:
                      </p>
                      <ul className={`mt-1 space-y-0.5 text-xs ${textMuted}`}>
                        {Object.entries(
                          artProtokoll.ohne_zuordnung.reduce<Record<string, number>>((k, z) => {
                            k[z.projekt] = Math.round(((k[z.projekt] ?? 0) + z.stunden) * 100) / 100;
                            return k;
                          }, {}),
                        ).map(([projekt, stunden]) => (
                          <li key={projekt}>{projekt} — {stunden} h</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {artProtokoll.uebergangen.length > 0 && (
                    <ul className={`mt-2 space-y-0.5 text-xs ${textMuted}`}>
                      {artProtokoll.uebergangen.map(u => <li key={u}>{u}</li>)}
                    </ul>
                  )}
                </>
              )}
            </div>
          )}
          {artProtokoll && !artProtokoll.fehler && (
            <Trockenbalken
              stand={artProtokoll} laeuft={taggt}
              ausfuehren={() => verrechnungsart(artProtokoll.intern ?? false, true)}
              beschriftung="Jetzt wirklich in Toggl setzen"
            />
          )}
          <p className={`mt-2 text-xs ${textMuted}`}>
            Der Entwurf geht an den Empfänger aus den Stammdaten, mit Rechnung und
            Leistungsrapport als ein PDF. Bexio stellt erst aus, nachdem der Versand
            bestätigt ist — vorher wäre eine Forderung gebucht, die niemand gesehen hat.
            Die Ablage nach <code>Finanzen/Debitoren/{'{Kunde}'}/{'{Jahr}'}</code> schreibt
            nie über eine bestehende Datei; eine vorhandene gilt als «lag schon da».
            Die Verrechnungsart kommt zuletzt: bis der Versand bestätigt ist, bleibt
            Toggl unangetastet und ist damit die Instanz, auf die im Zweifel
            zurückgegriffen werden kann.
          </p>
        </div>
      )}

      {/* Eigene Arbeit: genannt, aber nicht als Mangel */}
      {data.intern?.length > 0 && (
        <p className={`px-1 text-xs ${textMuted}`}>
          Nicht geprüft, weil ohne Vertrag und ohne verrechenbare Stunden:{' '}
          {data.intern.join(' · ')}
        </p>
      )}

      {/* Was in den Entwürfen stehen müsste. Berechnet, nicht geschrieben —
          angewendet wird nach Bestätigung, in einem eigenen Schritt. */}
      {data.vorschlaege?.length > 0 && (
        <div className={sectionClass}>
          <div className="mb-2 flex items-start justify-between gap-3">
            <p className={`text-sm font-semibold ${textPrimary}`}>
              Offene Anpassungen in den Entwürfen
            </p>
            {data.vorschlaege.some(v => v.aenderungen.length > 0) && (
              <button
                onClick={() => anwenden()}
                disabled={schreibt}
                className={`shrink-0 rounded-lg px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${hasBg ? 'bg-white/10 text-white/90 hover:bg-white/20' : 'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}
              >
                {schreibt ? 'schreibt …' : 'Alle nach Bexio schreiben'}
              </button>
            )}
          </div>
          {protokoll && (
            <p className={`mb-2 text-sm ${textSecondary}`}>
              {protokoll.fehler
                ? protokoll.fehler
                : `${protokoll.geschrieben} Zeile(n) geschrieben`
                  + (protokoll.fehlgeschlagen ? `, ${protokoll.fehlgeschlagen} fehlgeschlagen: `
                      + protokoll.protokoll.filter(p => !p.erfolg)
                          .map(p => `${p.rechnung} (${p.meldung})`).join(', ')
                    : '.')
                  + (protokoll.zurueckgestellt?.length
                      ? ` Unberührt geblieben, weil zurückgestellt: ${protokoll.zurueckgestellt.join(', ')}.`
                      : '')}
            </p>
          )}
          {protokoll && !protokoll.fehler && (
            <div className="mb-2">
              <Trockenbalken
                stand={protokoll} laeuft={schreibt}
                ausfuehren={() => anwenden(true)}
                beschriftung="Änderungen jetzt wirklich nach Bexio schreiben"
              />
            </div>
          )}
          <ul className="space-y-3">
            {data.vorschlaege.map((v) => (
              <li key={v.rechnung}>
                <p className={`text-sm font-medium ${textPrimary}`}>
                  {v.rechnung} · {v.projekt}
                </p>
                {v.aenderungen.map((a, i) => (
                  <div key={i} className={`mt-1 text-sm ${textSecondary}`}>
                    <span className="tabular-nums">
                      {a.handlung === 'entfernen' ? 'Zeile entfernen: ' : ''}
                      <span className="line-through opacity-60">{a.alt}</span>
                      {a.handlung === 'entfernen' ? '' : ` → ${a.neu}`}
                    </span>
                    <span className={`ml-2 text-xs ${textMuted}`}>{a.begruendung}</span>
                  </div>
                ))}
                {v.hindernisse.map((h, i) => (
                  <p key={i} className={`mt-1 text-sm ${textSecondary}`}>· {h}</p>
                ))}
              </li>
            ))}
          </ul>
          <p className={`mt-2 text-xs ${textMuted}`}>
            Der Stundensatz bleibt unangetastet; geändert wird allein die Menge.
          </p>
        </div>
      )}

      {/* Der Übertragsstand, auf dem die Stundenprüfung der übertragbaren
          Verträge beruht. Sichtbar, weil eine falsche Grundlage sonst ein
          richtig aussehendes Urteil erzeugt. */}
      {(Object.keys(data.uebertrag ?? {}).length > 0 || data.uebertrag_hinweise?.length > 0) && (
        <div className={sectionClass}>
          <p className={`mb-2 text-sm font-semibold ${textPrimary}`}>Übertrag zu Monatsbeginn</p>
          {Object.keys(data.uebertrag ?? {}).length > 0 && (
            <div className={`flex flex-wrap gap-x-5 gap-y-1 text-sm ${textSecondary}`}>
              {Object.entries(data.uebertrag).map(([vertrag, stunden]) => (
                <span key={vertrag}>
                  {vertrag} <span className="tabular-nums font-medium">{stunden}h</span>
                  {data.uebertrag_herkunft?.[vertrag] && (
                    <span className={`ml-1 text-xs ${textMuted}`}>
                      (aus {data.uebertrag_herkunft[vertrag]})
                    </span>
                  )}
                </span>
              ))}
            </div>
          )}
          {data.uebertrag_hinweise?.length > 0 && (
            <ul className={`mt-2 space-y-1.5 text-sm ${textSecondary}`}>
              {data.uebertrag_hinweise.map((h, i) => <li key={i}>· {h}</li>)}
            </ul>
          )}
          <p className={`mt-2 text-xs ${textMuted}`}>
            Gelesen aus der jüngsten Rechnung davor — nicht geschätzt. Ein Monat ohne
            Rechnung und ohne Stunden in Toggl gilt als Pause und trägt den Stand weiter.
          </p>
        </div>
      )}

      {/* Mängel der Stammdatei — ein Vertrag ohne Empfänger fällt sonst erst
          auf, wenn die Mail nicht abgeht */}
      {data.stammdaten.length > 0 && (
        <div className={sectionClass}>
          <p className={`mb-2 text-sm font-semibold ${textPrimary}`}>Stammdaten</p>
          <ul className={`space-y-1.5 text-sm ${textSecondary}`}>
            {data.stammdaten.map((s, i) => <li key={i}>· {s}</li>)}
          </ul>
          <p className={`mt-2 text-xs ${textMuted}`}>
            Gepflegt in <code>docs/debitorenvertraege.yaml</code>.
          </p>
        </div>
      )}
    </div>
  );
}
