/**
 * Der Kreditoreneingang: was auf eine Entscheidung wartet.
 *
 * Drei Handlungen, und alle drei brauchen den Beleg vor Augen: freigeben,
 * zurücklegen, korrigieren. Darum steht das PDF **neben** dem Vorschlag und
 * nicht hinter einem zweiten Klick -- wer prüfen soll, muss sehen.
 *
 * Auf dem Telefon geht das nicht nebeneinander. Dort ist die Liste die Seite,
 * und ein Beleg öffnet sich als eigene Ansicht mit Zurück-Schritt. Kein
 * Nebeneinander auf 390 Pixel: zwei Spalten wären dort zwei unlesbare.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle, ArrowLeft, Check, FileText, Inbox, Lock,
  Pencil, RefreshCw, Undo2,
} from 'lucide-react';
import { api } from '../../api/client';
import type { StyleCtx } from './creditors-types';
import BelegMaske from './BelegMaske';
import Nachholen, {
  BerichtAnzeige, PruefungAnzeige, type Buchungsbericht, type Buchungspruefung,
} from './Nachholen';
import Sammelbelege, { type SammelMonat } from './Sammelbelege';
import { useBelegUrl } from './belegDatei';

export interface EingangsBeleg {
  id: string;
  dateiname: string;
  quelle: string;
  eingang_am?: string | null;
  beleg_id?: number | null;
  lieferant_schluessel?: string | null;
  lieferant?: string | null;
  rechnungsnummer?: string | null;
  lieferant_bestaetigt: boolean;
  /** Wenn der Absender mehrere Lieferanten meint -- «Google» für Workspace,
   *  Cloud, One, YouTube und Gemini. Dann ist zuerst der Dienst zu wählen. */
  lieferant_kandidaten: { schluessel: string; anzeigename: string }[];
  betrag?: number | null;
  betrag_chf?: number | null;
  waehrung?: string | null;
  datum?: string | null;
  produkt?: string | null;
  sollkonto?: string | null;
  sollkonto_herkunft?: string | null;
  sollkonto_kandidaten: string[];
  steuerbehandlung?: string | null;
  zahlweg?: string | null;
  leistung?: string | null;
  leistung_herkunft?: 'lieferant' | 'beleg' | null;
  buchungstext?: string | null;
  dateiname_ziel?: string | null;
  freigegeben_am?: string | null;
  gebucht_am?: string | null;
  bexio_referenz?: string | null;
  abgelegt_am?: string | null;
  archiv_pfad?: string | null;
  /** Wird über den Sammelbeleg gebucht -- die Rechnung wird nur abgelegt. */
  nur_ablegen: boolean;
  zurueckgestellt: boolean;
  grund?: string | null;
  abweichungen: string[];
}

interface Warteliste {
  belege: EingangsBeleg[];
  /** Freigegeben, aber Buchung oder Ablage ist gescheitert. */
  zu_buchen: EingangsBeleg[];
  offen: number;
  ohne_konto: number;
  zurueckgestellt: number;
  dubletten: number;
  /** Nur nach einem Abgleich gefüllt: Dateien im Eingang, deren Rechnung schon im Archiv liegt. */
  schon_im_archiv?: string[];
  /** Nutzungsrechnungen je Kalendermonat -- sie warten auf den Sammelbeleg. */
  sammelbeleg_wartet?: SammelMonat[];
  stand?: string | null;
  hinweise: string[];
}

interface Konto {
  konto_nr: string;
  konto: string;
  /** Wie oft das Konto je auf der Sollseite stand. Trennt das Gewöhnliche
   *  vom Möglichen: 95 Aufwandskonten, 46 davon je benutzt. */
  buchungen: number;
}

/** Kennungen werden nie roh angezeigt -- sonst stünde «bank_direkt» am Bildschirm. */
const STEUER_TEXT: Record<string, string> = {
  bezugssteuer: 'Bezugssteuer',
  inland_mwst: 'Inland-MWST',
  ohne_mwst: 'ohne MWST',
  unbekannt: 'unbekannt',
};
const ZAHLWEG_TEXT: Record<string, string> = {
  karte: 'Karte',
  rechnung: 'Rechnung',
  bank_direkt: 'Bank direkt',
};
const QUELLE_TEXT: Record<string, string> = {
  autodownload: 'Autodownload',
  ablage_hand: 'von Hand abgelegt',
  postfach: 'Postfach',
  upload: 'hochgeladen',
};

export function geld(betrag?: number | null, waehrung?: string | null): string {
  if (betrag == null) return '–';
  return `${waehrung || 'CHF'} ${betrag.toLocaleString('de-CH', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

interface Props {
  styleCtx: StyleCtx;
  onAnzahl?: (offen: number) => void;
}

export function CreditorsEingang({ styleCtx, onAnzahl }: Props) {
  const { cardClass, textPrimary, textSecondary, textMuted } = styleCtx;

  const [liste, setListe] = useState<Warteliste | null>(null);
  const [laedt, setLaedt] = useState(true);
  const [gleicht, setGleicht] = useState(false);
  const [fehler, setFehler] = useState<string | null>(null);
  const [gewaehlt, setGewaehlt] = useState<string | null>(null);
  const [maskeOffen, setMaskeOffen] = useState(false);
  const [maskenBeleg, setMaskenBeleg] = useState<Record<string, unknown> | null>(null);
  const [konten, setKonten] = useState<Konto[]>([]);
  const [kontenFehler, setKontenFehler] = useState<string | null>(null);

  /** Die gewählten Konten, je Beleg gemerkt.
   *
   *  Je Beleg und nicht als ein Wert, weil beim Prüfen hin und her gewechselt
   *  wird: ein Konto, das beim Zurückkommen wieder weg ist, wird zweimal
   *  gesucht. Der Register-Wert bleibt unberührt, bis freigegeben wird. */
  const [kontoWahl, setKontoWahl] = useState<Record<string, string>>({});
  /** Die erfasste Leistung, je Beleg gemerkt -- aus demselben Grund. */
  const [leistungWahl, setLeistungWahl] = useState<Record<string, string>>({});

  const laden = useCallback(async () => {
    setFehler(null);
    try {
      const daten = await api.get<Warteliste>('/api/creditors/eingang');
      setListe(daten);
      onAnzahl?.(daten.offen);
      setGewaehlt(vorher =>
        vorher && daten.belege.some(b => b.id === vorher) ? vorher : daten.belege[0]?.id ?? null,
      );
    } catch (e) {
      setFehler((e as Error).message);
    }
    setLaedt(false);
  }, [onAnzahl]);

  useEffect(() => { void laden(); }, [laden]);

  /** Der Kontenplan einmal, nicht je Beleg: er ändert sich nicht im Minutentakt.
   *
   *  Ein Fehler hier sperrt die Seite nicht -- ein deklarierter Vorschlag lässt
   *  sich weiter freigeben. Nur die freie Wahl fehlt, und das steht dann dort. */
  useEffect(() => {
    void (async () => {
      try {
        const antwort = await api.get<{ konten: Konto[] }>('/api/creditors/kontenplan');
        setKonten(antwort.konten);
      } catch (e) {
        setKontenFehler((e as Error).message);
      }
    })();
  }, []);

  const abgleichen = async () => {
    setGleicht(true);
    setFehler(null);
    try {
      const daten = await api.post<Warteliste>('/api/creditors/eingang/abgleichen', {});
      setListe(daten);
      onAnzahl?.(daten.offen);
    } catch (e) {
      setFehler((e as Error).message);
    }
    setGleicht(false);
  };

  const beleg = liste?.belege.find(b => b.id === gewaehlt) ?? null;

  /** Das Konto, das gerade gilt: die Wahl, sonst der Registerwert. */
  const kontoVon = (b: EingangsBeleg) => kontoWahl[b.id] ?? b.sollkonto ?? '';

  const gewaehltesKonto = beleg ? kontoVon(beleg) : '';
  const geaendert = !!beleg && !!gewaehltesKonto && gewaehltesKonto !== beleg.sollkonto;

  /** Steht das geltende Konto in keiner Gruppe, braucht es eine eigene Option.
   *
   *  Sonst zeigte die Auswahl «noch offen», während im Register ein Konto steht --
   *  und das passiert genau dann, wenn der Kontenplan nicht abrufbar war. Eine
   *  Anzeige, die in der Störung das Gegenteil behauptet, ist schlimmer als keine. */
  const kontoFehltInListe =
    !!gewaehltesKonto &&
    !konten.some(k => k.konto_nr === gewaehltesKonto) &&
    !(beleg?.sollkonto_kandidaten ?? []).includes(gewaehltesKonto);

  const leistungVon = (b: EingangsBeleg) => leistungWahl[b.id] ?? b.leistung ?? '';
  const gewaehlteLeistung = beleg ? leistungVon(beleg).trim() : '';
  const leistungGeaendert =
    !!beleg && !!gewaehlteLeistung && gewaehlteLeistung !== (beleg.leistung ?? '');

  /** Die Normprüfung für genau das, was gerade in der Maske steht.
   *
   *  Der Schlüssel enthält Konto und Leistung: eine Prüfung, die zum vorigen
   *  Konto gehört, darf den Knopf nicht freischalten. Kurz verzögert, weil die
   *  Prüfung live gegen Bexio und das BAZG läuft und nicht bei jedem Tastendruck. */
  const [vorschau, setVorschau] = useState<{ schluessel: string; p?: Buchungspruefung; fehler?: string } | null>(null);
  const [freigabeLaeuft, setFreigabeLaeuft] = useState(false);
  const [berichte, setBerichte] = useState<{ titel: string; r: Buchungsbericht }[]>([]);

  const vorschauSchluessel = beleg && gewaehltesKonto && gewaehlteLeistung
    ? `${beleg.id}|${gewaehltesKonto}|${gewaehlteLeistung}`
    : null;

  useEffect(() => {
    if (!vorschauSchluessel || !beleg) return;
    const id = beleg.id;
    const abfrage = new URLSearchParams({ sollkonto: gewaehltesKonto, leistung: gewaehlteLeistung });
    // Eine späte Antwort zu einer früheren Eingabe überschreibt die neuere nicht.
    let gueltig = true;
    const setzen = (inhalt: { p?: Buchungspruefung; fehler?: string }) => {
      if (gueltig) setVorschau({ schluessel: vorschauSchluessel, ...inhalt });
    };
    const zeit = window.setTimeout(async () => {
      try {
        setzen({ p: await api.get<Buchungspruefung>(`/api/creditors/eingang/${id}/pruefung?${abfrage}`) });
      } catch (e) {
        setzen({ fehler: (e as Error).message });
      }
    }, 400);
    return () => { gueltig = false; window.clearTimeout(zeit); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vorschauSchluessel]);

  const aktuellePruefung = vorschau && vorschau.schluessel === vorschauSchluessel ? vorschau : null;
  const freigebbar = !!beleg && !!gewaehltesKonto && !!gewaehlteLeistung
    && !!aktuellePruefung?.p?.buchbar;

  const freigeben = async (b: EingangsBeleg) => {
    const konto = kontoVon(b);
    const leistung = leistungVon(b).trim();
    if (!konto || !leistung) return;
    const p = aktuellePruefung?.p;
    // Die Rückfrage nennt, was geschieht -- nicht «Sind Sie sicher?».
    const text = p
      ? `Freigeben, in Bexio buchen und ablegen?\n\n${p.buchungstext}\n` +
        `${p.datum} · Soll ${p.sollkonto} / Haben ${p.habenkonto}` +
        `${p.steuercode ? ` · ${p.steuercode}` : ''}\n` +
        `${geld(p.betrag, p.waehrung)}` +
        `${p.waehrung !== 'CHF' ? ` zu ${p.kurs} = ${geld(p.betrag_chf, 'CHF')}` : ''}\n\n` +
        `Ablage unter\n${p.ablageziel}`
      : null;
    if (!text || !window.confirm(text)) return;
    setFehler(null);
    setFreigabeLaeuft(true);
    try {
      // Nur **Geändertes** wird mitgeschickt. Sonst würde jede Freigabe zur
      // «Entscheidung», und die Deklaration liesse sich nicht mehr von einem
      // bloss übernommenen Vorschlag unterscheiden.
      const r = await api.post<Buchungsbericht>(`/api/creditors/eingang/${b.id}/freigeben`, {
        ...(konto === b.sollkonto ? {} : { sollkonto: konto }),
        ...(leistung === (b.leistung ?? '') ? {} : { leistung }),
      });
      setBerichte(vorher => [{ titel: b.buchungstext || b.lieferant || b.dateiname, r }, ...vorher]);
      const ohne = (vorher: Record<string, string>) => {
        const rest = { ...vorher };
        delete rest[b.id];
        return rest;
      };
      setKontoWahl(ohne);
      setLeistungWahl(ohne);
      await laden();
    } catch (e) {
      setFehler((e as Error).message);
    }
    setFreigabeLaeuft(false);
  };

  const [lieferantLaeuft, setLieferantLaeuft] = useState(false);

  /** Den Dienst hinter einem Sammelabsender wählen. Danach steht der Vorschlag
   *  des gewählten Lieferanten da -- Konto, Steuer, Zahlweg, Leistung. */
  const lieferantWaehlen = async (b: EingangsBeleg, schluessel: string) => {
    setFehler(null);
    setLieferantLaeuft(true);
    try {
      await api.post(`/api/creditors/eingang/${b.id}/lieferant`, { schluessel });
      await laden();
    } catch (e) {
      setFehler((e as Error).message);
    }
    setLieferantLaeuft(false);
  };

  const zuruecklegen = async (b: EingangsBeleg) => {
    const grund = window.prompt(
      `«${b.dateiname}» zurücklegen — warum?\n\n` +
      'Ohne Grund ist die Ausnahme in drei Wochen nicht mehr nachvollziehbar.',
    );
    if (!grund || grund.trim().length < 3) return;
    setFehler(null);
    try {
      await api.post(`/api/creditors/eingang/${b.id}/zuruecklegen`, { grund: grund.trim() });
      await laden();
    } catch (e) {
      setFehler((e as Error).message);
    }
  };

  /** Die Erwartung zum Lieferanten prüfen -- sie gilt danach für jede weitere
   *  Rechnung desselben Lieferanten, nicht nur für diese.
   *
   *  Das gewählte Konto wird nur dann fortgeschrieben, wenn die Deklaration gar
   *  keines trägt. Wo Kandidaten stehen, hängt das Konto ausdrücklich an der
   *  einzelnen Rechnung (Hosttech 6512 gegen 4200) -- die Wahl für **diese**
   *  Rechnung als Regel für alle festzuschreiben, würde eine bewusst offene
   *  Frage als Nebenwirkung schliessen. */
  const lieferantBestaetigen = async (b: EingangsBeleg) => {
    if (!b.lieferant_schluessel) return;
    const fortschreiben =
      !b.sollkonto_kandidaten.length && b.sollkonto_herkunft !== 'vorschlag';
    setFehler(null);
    try {
      await api.patch(`/api/creditors/lieferant/${b.lieferant_schluessel}`, {
        sollkonto: fortschreiben ? kontoVon(b) || null : null,
      });
      await laden();
    } catch (e) {
      setFehler((e as Error).message);
    }
  };

  const maskeOeffnen = async (b: EingangsBeleg) => {
    if (b.beleg_id == null) return;
    setFehler(null);
    try {
      const daten = await api.get<Record<string, unknown>>(`/api/creditors/beleg/${b.beleg_id}`);
      setMaskenBeleg(daten);
      setMaskeOffen(true);
    } catch (e) {
      setFehler((e as Error).message);
    }
  };

  if (laedt) {
    return (
      <div className="flex flex-col items-center gap-3 py-20">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-indigo-400 border-t-transparent" />
        <p className={textMuted}>Lade Eingang…</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Kopfzeile: Zahlen und der Abgleich */}
      <div className={`${cardClass} flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between`}>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
          <span className={`flex items-center gap-2 text-sm font-semibold ${textPrimary}`}>
            <Inbox className="h-4 w-4 shrink-0" />
            {liste?.offen ?? 0} warten auf Entscheidung
          </span>
          {(liste?.ohne_konto ?? 0) > 0 && (
            <span className="text-xs text-amber-600 dark:text-amber-400">
              {liste?.ohne_konto} ohne Kontovorschlag
            </span>
          )}
          {(liste?.zurueckgestellt ?? 0) > 0 && (
            <span className={`text-xs ${textMuted}`}>
              {liste?.zurueckgestellt} zurückgestellt
            </span>
          )}
        </div>
        <button
          onClick={abgleichen}
          disabled={gleicht}
          className="flex min-h-11 items-center justify-center gap-2 rounded-lg bg-indigo-500 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-600 disabled:opacity-60 lg:min-h-0"
        >
          <RefreshCw className={`h-4 w-4 ${gleicht ? 'animate-spin' : ''}`} />
          Eingang abgleichen
        </button>
      </div>

      {fehler && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300">
          {fehler}
        </div>
      )}

      {liste?.hinweise.map(h => (
        <div
          key={h}
          className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{h}</span>
        </div>
      ))}

      {(liste?.schon_im_archiv?.length ?? 0) > 0 && (
        <details className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
          <summary className="cursor-pointer">
            {liste!.schon_im_archiv!.length} Datei(en) im Eingang liegen schon im Archiv und
            stehen nicht in der Liste — sie können gelöscht werden
          </summary>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
            {liste!.schon_im_archiv!.map(s => <li key={s}>{s}</li>)}
          </ul>
        </details>
      )}

      {(liste?.sammelbeleg_wartet?.length ?? 0) > 0 && (
        <Sammelbelege
          monate={liste!.sammelbeleg_wartet!}
          styleCtx={styleCtx}
          onGebucht={(titel, r) => setBerichte(vorher => [{ titel, r }, ...vorher])}
          onGeaendert={laden}
        />
      )}

      {berichte.map(({ titel, r }, i) => (
        <div key={`${titel}-${i}`} className={`${cardClass} px-4 py-3`}>
          <p className={`mb-1 truncate text-xs ${textMuted}`}>{titel}</p>
          <BerichtAnzeige r={r} />
        </div>
      ))}

      {(liste?.zu_buchen.length ?? 0) > 0 && (
        <Nachholen belege={liste!.zu_buchen} styleCtx={styleCtx} onGeaendert={laden} />
      )}

      {(liste?.belege.length ?? 0) === 0 ? (
        <div className={`${cardClass} px-4 py-16 text-center`}>
          <Inbox className={`mx-auto mb-3 h-8 w-8 ${textMuted}`} />
          <p className={`text-sm font-medium ${textPrimary}`}>Der Eingang ist leer</p>
          <p className={`mt-1 text-xs ${textMuted}`}>
            Neue Rechnungen liegen in <code>_OPEN/InnoSmith</code>, bis sie freigegeben
            sind. Was hier fehlt, hat die Extraktion noch nicht gelesen — «Eingang
            abgleichen» holt es nach.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(18rem,22rem)_1fr]">
          {/* Warteliste. Auf dem Telefon versteckt, sobald einer gewählt ist. */}
          <div className={`${beleg ? 'hidden xl:block' : ''}`}>
            <div className={`${cardClass} divide-y divide-gray-100 overflow-hidden dark:divide-gray-800`}>
              {liste?.belege.map(b => {
                const aktiv = b.id === gewaehlt;
                return (
                  <button
                    key={b.id}
                    onClick={() => setGewaehlt(b.id)}
                    className={`flex w-full flex-col gap-1 px-4 py-3 text-left transition-colors ${
                      aktiv
                        ? 'bg-indigo-50 dark:bg-indigo-950/40'
                        : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'
                    }`}
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <span className={`truncate text-sm font-medium ${textPrimary}`}>
                        {b.lieferant || b.lieferant_schluessel || b.dateiname}
                      </span>
                      <span className={`shrink-0 text-xs tabular-nums ${textSecondary}`}>
                        {geld(b.betrag, b.waehrung)}
                      </span>
                    </div>
                    <div className={`flex flex-wrap items-center gap-2 text-[11px] ${textMuted}`}>
                      <span>{b.datum || '–'}</span>
                      {b.sollkonto ? (
                        <span className="rounded bg-gray-100 px-1.5 py-0.5 font-medium tabular-nums dark:bg-gray-800">
                          {b.sollkonto}
                        </span>
                      ) : (
                        <span className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                          Konto offen
                        </span>
                      )}
                      {b.zurueckgestellt && <span className="italic">zurückgestellt</span>}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Der gewählte Beleg: PDF neben Vorschlag, mobil untereinander. */}
          {beleg && (
            <div className="space-y-4">
              <button
                onClick={() => setGewaehlt(null)}
                className={`flex min-h-11 items-center gap-1.5 text-sm font-medium xl:hidden ${textSecondary}`}
              >
                <ArrowLeft className="h-4 w-4" /> Zur Liste
              </button>

              <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
                {/* Buchungsvorschlag */}
                <div className={`${cardClass} p-4`}>
                  <h3 className={`text-base font-bold ${textPrimary}`}>
                    {beleg.lieferant || beleg.lieferant_schluessel || 'Lieferant unbekannt'}
                  </h3>
                  <p className={`mt-0.5 truncate text-xs ${textMuted}`}>
                    {beleg.dateiname} · {QUELLE_TEXT[beleg.quelle] ?? beleg.quelle}
                  </p>
                  {beleg.produkt && (
                    <p className={`mt-2 text-sm ${textSecondary}`}>{beleg.produkt}</p>
                  )}

                  <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                    <div>
                      <dt className={`text-[11px] uppercase tracking-wide ${textMuted}`}>Betrag</dt>
                      <dd className={`tabular-nums ${textPrimary}`}>
                        {geld(beleg.betrag, beleg.waehrung)}
                      </dd>
                    </div>
                    <div>
                      <dt className={`text-[11px] uppercase tracking-wide ${textMuted}`}>in CHF</dt>
                      <dd className={`tabular-nums ${textPrimary}`}>{geld(beleg.betrag_chf, 'CHF')}</dd>
                    </div>
                    <div>
                      <dt className={`text-[11px] uppercase tracking-wide ${textMuted}`}>Steuer</dt>
                      <dd className={textPrimary}>
                        {beleg.steuerbehandlung ? STEUER_TEXT[beleg.steuerbehandlung] ?? beleg.steuerbehandlung : '–'}
                      </dd>
                    </div>
                    <div>
                      <dt className={`text-[11px] uppercase tracking-wide ${textMuted}`}>Zahlweg</dt>
                      <dd className={textPrimary}>
                        {beleg.zahlweg ? ZAHLWEG_TEXT[beleg.zahlweg] ?? beleg.zahlweg : '–'}
                      </dd>
                    </div>
                    <div>
                      <dt className={`text-[11px] uppercase tracking-wide ${textMuted}`}>Datum</dt>
                      <dd className={textPrimary}>{beleg.datum || '–'}</dd>
                    </div>
                  </dl>

                  {(beleg.lieferant_kandidaten ?? []).length > 0 && (() => {
                    const dienst = beleg.lieferant_kandidaten.find(
                      k => k.schluessel === beleg.lieferant_schluessel,
                    );
                    return (
                      <div
                        className={`mt-4 rounded-lg border p-3 ${
                          dienst
                            ? 'border-gray-200 dark:border-gray-700'
                            : 'border-amber-300 dark:border-amber-700'
                        }`}
                      >
                        <p className={`text-[11px] uppercase tracking-wide ${textMuted}`}>
                          {dienst ? `Dienst: ${dienst.anzeigename}` : 'Welcher Dienst?'}
                        </p>
                        <p className={`mt-1 text-xs ${textSecondary}`}>
                          Auf der Rechnung steht nur «{beleg.lieferant || 'Google'}».
                          Konto, Ordner und Buchungstext hängen am Dienst
                          {beleg.freigegeben_am ? '.' : ' — bis zur Freigabe umwählbar.'}
                        </p>
                        {!beleg.freigegeben_am && (
                          <div className="mt-2 flex flex-wrap gap-2">
                            {beleg.lieferant_kandidaten.map(k => (
                              <button
                                key={k.schluessel}
                                type="button"
                                disabled={lieferantLaeuft || k.schluessel === dienst?.schluessel}
                                aria-pressed={k.schluessel === dienst?.schluessel}
                                onClick={() => lieferantWaehlen(beleg, k.schluessel)}
                                className={`min-h-11 rounded-lg border px-3 py-2 text-sm disabled:cursor-default ${
                                  k.schluessel === dienst?.schluessel
                                    ? 'border-indigo-500 bg-indigo-50 font-medium dark:bg-indigo-950'
                                    : 'border-gray-200 hover:border-indigo-400 disabled:opacity-50 dark:border-gray-700'
                                } ${textPrimary}`}
                              >
                                {k.anzeigename}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })()}

                  {/* Die Kontenwahl. Bei 86 der 149 Lieferanten trägt die
                      Deklaration kein Konto -- ohne dieses Feld wäre «Freigeben»
                      dort gesperrt und es gäbe keinen Weg daran vorbei. */}
                  <div className="mt-4">
                    <label
                      htmlFor={`konto-${beleg.id}`}
                      className={`flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] uppercase tracking-wide ${textMuted}`}
                    >
                      Sollkonto
                      {beleg.sollkonto_herkunft === 'entscheid' && (
                        <span
                          className="flex items-center gap-1 normal-case tracking-normal"
                          title="Von Hand entschieden — übersteht jeden weiteren Abgleich"
                        >
                          <Lock className="h-3 w-3" /> entschieden
                        </span>
                      )}
                      {beleg.sollkonto_herkunft === 'vorschlag' && (
                        <span className="normal-case tracking-normal">
                          Vorschlag aus der Deklaration
                        </span>
                      )}
                      {geaendert && (
                        <span className="normal-case tracking-normal text-amber-600 dark:text-amber-400">
                          abweichend von {beleg.sollkonto}
                        </span>
                      )}
                    </label>
                    <select
                      id={`konto-${beleg.id}`}
                      value={kontoVon(beleg)}
                      onChange={e =>
                        setKontoWahl(vorher => ({ ...vorher, [beleg.id]: e.target.value }))
                      }
                      className={`mt-1 min-h-11 w-full rounded-lg border px-3 py-2 text-sm tabular-nums ${
                        kontoVon(beleg)
                          ? 'border-gray-200 dark:border-gray-700'
                          : 'border-amber-300 dark:border-amber-700'
                      } bg-white dark:bg-gray-800 ${textPrimary}`}
                    >
                      <option value="">— noch offen —</option>
                      {kontoFehltInListe && (
                        <option value={gewaehltesKonto}>
                          {gewaehltesKonto} (nicht im abgerufenen Kontenplan)
                        </option>
                      )}
                      {/* Die Kandidaten zuoberst: dort hängt das Konto an der
                          Rechnung, nicht am Lieferanten (Hosttech 6512 gegen 4200). */}
                      {beleg.sollkonto_kandidaten.length > 0 && (
                        <optgroup label="Für diese Rechnung zu entscheiden">
                          {beleg.sollkonto_kandidaten.map(k => (
                            <option key={`kand-${k}`} value={k}>
                              {k} {konten.find(c => c.konto_nr === k)?.konto ?? ''}
                            </option>
                          ))}
                        </optgroup>
                      )}
                      <optgroup label="Schon benutzt">
                        {konten.filter(k => k.buchungen > 0).map(k => (
                          <option key={`b-${k.konto_nr}`} value={k.konto_nr}>
                            {k.konto_nr} {k.konto}
                          </option>
                        ))}
                      </optgroup>
                      <optgroup label="Übrige Aufwandskonten">
                        {konten.filter(k => k.buchungen === 0).map(k => (
                          <option key={`u-${k.konto_nr}`} value={k.konto_nr}>
                            {k.konto_nr} {k.konto}
                          </option>
                        ))}
                      </optgroup>
                    </select>
                    {kontenFehler && (
                      <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
                        Der Kontenplan ist nicht abrufbar ({kontenFehler}) — wählbar
                        sind nur Vorschlag und Kandidaten.
                      </p>
                    )}
                  </div>

                  {/* Die Leistung steht in Buchungstext und Dateiname. Meist
                      kommt sie vom Lieferanten; bei Hosttech nennt jede
                      Rechnung ihre eigene Domain. */}
                  <div className="mt-4">
                    <label
                      htmlFor={`leistung-${beleg.id}`}
                      className={`flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] uppercase tracking-wide ${textMuted}`}
                    >
                      Leistung
                      {beleg.leistung_herkunft === 'lieferant' && !leistungGeaendert && (
                        <span className="normal-case tracking-normal">Vorgabe des Lieferanten</span>
                      )}
                      {beleg.leistung_herkunft === 'beleg' && !leistungGeaendert && (
                        <span className="normal-case tracking-normal">an diesem Beleg erfasst</span>
                      )}
                      {leistungGeaendert && (
                        <span className="normal-case tracking-normal text-amber-600 dark:text-amber-400">
                          {beleg.leistung ? `abweichend von «${beleg.leistung}»` : 'neu erfasst'}
                        </span>
                      )}
                    </label>
                    <input
                      id={`leistung-${beleg.id}`}
                      type="text"
                      value={leistungVon(beleg)}
                      placeholder="z. B. Monatsabo, Jahresabo, Domain beispiel.ch"
                      onChange={e =>
                        setLeistungWahl(vorher => ({ ...vorher, [beleg.id]: e.target.value }))
                      }
                      className={`mt-1 min-h-11 w-full rounded-lg border px-3 py-2 text-sm ${
                        gewaehlteLeistung
                          ? 'border-gray-200 dark:border-gray-700'
                          : 'border-amber-300 dark:border-amber-700'
                      } bg-white dark:bg-gray-800 ${textPrimary}`}
                    />
                  </div>

                  {/* Was in Bexio und im Archiv stehen wird -- vom Server gebildet,
                      damit die Norm an einer Stelle lebt. */}
                  <dl className={`mt-4 space-y-2 rounded-lg bg-gray-50 p-3 text-sm dark:bg-gray-800/60`}>
                    <div>
                      <dt className={`text-[11px] uppercase tracking-wide ${textMuted}`}>Buchungstext</dt>
                      <dd className={`break-words ${textPrimary}`}>
                        {leistungGeaendert
                          ? <span className={textMuted}>wird mit «{gewaehlteLeistung}» gebildet</span>
                          : beleg.buchungstext || '–'}
                      </dd>
                    </div>
                    <div>
                      <dt className={`text-[11px] uppercase tracking-wide ${textMuted}`}>Name im Archiv</dt>
                      <dd className={`break-all ${textPrimary}`}>
                        {leistungGeaendert
                          ? <span className={textMuted}>wird mit «{gewaehlteLeistung}» gebildet</span>
                          : beleg.dateiname_ziel || '–'}
                      </dd>
                    </div>
                  </dl>

                  {/* Die Normprüfung: dasselbe, was der Klick prüft, bevor er bucht. */}
                  {!beleg.nur_ablegen && (
                    <div className="mt-4 space-y-2">
                      <p className={`text-[11px] uppercase tracking-wide ${textMuted}`}>Normprüfung</p>
                      {!vorschauSchluessel && (
                        <p className={`text-xs ${textMuted}`}>Erst Konto und Leistung — dann wird geprüft.</p>
                      )}
                      {vorschauSchluessel && !aktuellePruefung?.p && !aktuellePruefung?.fehler && (
                        <p className={`text-xs ${textMuted}`}>Prüfe gegen Bexio und BAZG…</p>
                      )}
                      {aktuellePruefung?.fehler && (
                        <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950/30 dark:text-red-300">
                          Prüfung nicht möglich: {aktuellePruefung.fehler}
                        </p>
                      )}
                      {aktuellePruefung?.p && <PruefungAnzeige p={aktuellePruefung.p} styleCtx={styleCtx} />}
                    </div>
                  )}

                  {/* Abweichungen sind Fragen, keine Fehler. */}
                  {beleg.abweichungen.length > 0 && (
                    <ul className="mt-4 space-y-2">
                      {beleg.abweichungen.map(a => (
                        <li
                          key={a}
                          className={`flex items-start gap-2 text-xs ${textSecondary}`}
                        >
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
                          <span>{a}</span>
                        </li>
                      ))}
                    </ul>
                  )}

                  {beleg.zurueckgestellt && beleg.grund && (
                    <p className={`mt-4 text-xs italic ${textMuted}`}>
                      Zurückgestellt: {beleg.grund}
                    </p>
                  )}

                  {/* Die drei Handlungen */}
                  <div className="mt-5 flex flex-wrap gap-2">
                    <button
                      onClick={() => freigeben(beleg)}
                      disabled={!freigebbar || freigabeLaeuft}
                      title={
                        !gewaehltesKonto
                          ? 'Erst ein Sollkonto wählen — ohne Konto hätte die Buchung kein Ziel'
                          : !gewaehlteLeistung
                            ? 'Erst die Leistung erfassen — sie steht in Buchungstext und Dateiname'
                            : !freigebbar
                              ? 'Die Normprüfung hält die Buchung an — Gründe oben'
                              : `Bucht auf ${gewaehltesKonto} und legt ab — danach nicht mehr änderbar`
                      }
                      className="flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-lg bg-emerald-500 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
                    >
                      <Check className="h-4 w-4" />
                      {freigabeLaeuft
                        ? 'Bucht und legt ab…'
                        : gewaehltesKonto ? `Freigeben auf ${gewaehltesKonto}` : 'Freigeben'}
                    </button>
                    <button
                      onClick={() => maskeOeffnen(beleg)}
                      disabled={beleg.beleg_id == null}
                      title={beleg.beleg_id == null ? 'Noch nicht gelesen — nichts zu korrigieren' : undefined}
                      className="flex min-h-11 items-center justify-center gap-1.5 rounded-lg border border-gray-200 px-4 py-2 text-sm font-medium hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-800 lg:min-h-0"
                    >
                      <Pencil className="h-4 w-4" /> Korrigieren
                    </button>
                    {!beleg.zurueckgestellt && (
                      <button
                        onClick={() => zuruecklegen(beleg)}
                        className="flex min-h-11 items-center justify-center gap-1.5 rounded-lg border border-gray-200 px-4 py-2 text-sm font-medium hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800 lg:min-h-0"
                      >
                        <Undo2 className="h-4 w-4" /> Zurücklegen
                      </button>
                    )}
                  </div>

                  {/* Die Erwartung gilt für jede weitere Rechnung dieses
                      Lieferanten, nicht nur für diese -- darum ein eigener
                      Schritt und nicht Teil der Freigabe. */}
                  {beleg.lieferant_schluessel && !beleg.lieferant_bestaetigt && (
                    <button
                      onClick={() => lieferantBestaetigen(beleg)}
                      title={
                        beleg.sollkonto_kandidaten.length
                          ? 'Bestätigt nur die Erwartung — die Kandidaten bleiben offen, '
                            + 'weil das Konto hier an der Rechnung hängt'
                          : beleg.sollkonto_herkunft === 'vorschlag'
                            ? `Bestätigt die Erwartung samt dem deklarierten Konto ${beleg.sollkonto}`
                            : `Schreibt ${gewaehltesKonto || 'das Konto'} als Erwartung für jede `
                              + 'weitere Rechnung dieses Lieferanten fest'
                      }
                      className={`mt-3 flex min-h-11 w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-gray-300 px-4 py-2 text-xs font-medium dark:border-gray-600 lg:min-h-0 ${textSecondary}`}
                    >
                      <Check className="h-3.5 w-3.5" />
                      Erwartung zu «{beleg.lieferant_schluessel}» bestätigen
                    </button>
                  )}
                </div>

                {/* Das PDF. Ohne es ist «prüfen» ein Wort ohne Inhalt. */}
                <div className={`${cardClass} flex flex-col overflow-hidden`}>
                  {beleg.beleg_id != null ? (
                    <BelegPdf belegId={beleg.beleg_id} dateiname={beleg.dateiname} styleCtx={styleCtx} />
                  ) : (
                    <div className="flex flex-col items-center gap-2 px-4 py-16 text-center">
                      <FileText className={`h-7 w-7 ${textMuted}`} />
                      <p className={`text-sm ${textSecondary}`}>
                        Dieser Beleg ist registriert, aber noch nicht gelesen.
                      </p>
                      <p className={`text-xs ${textMuted}`}>
                        Die Datei kennt nur das Modul — ohne Extraktion gibt es
                        nichts anzuzeigen.
                      </p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Korrekturmaske, dieselbe wie im Reiter Rechnungen. */}
      {maskeOffen && maskenBeleg && beleg?.beleg_id != null && (
        <div
          className="modal-safe fixed inset-0 z-50 flex items-end justify-center bg-black/50 backdrop-blur-sm sm:items-center"
          onClick={() => setMaskeOffen(false)}
        >
          <div
            className="flex max-h-[min(95vh,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)))] w-full flex-col overflow-hidden rounded-t-2xl bg-white shadow-2xl sm:max-h-[85vh] sm:max-w-6xl sm:rounded-2xl dark:bg-gray-900"
            onClick={e => e.stopPropagation()}
          >
            <BelegMaske
              belegId={beleg.beleg_id}
              beleg={maskenBeleg}
              styleCtx={styleCtx}
              onClose={() => setMaskeOffen(false)}
              onGespeichert={frisch => { setMaskenBeleg(frisch); void laden(); }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/** Das PDF neben der Maske, angemeldet geladen -- ein `<iframe src>` auf die
 *  Route bekäme ohne Bearer-Header nur 403. */
function BelegPdf({ belegId, dateiname, styleCtx }: { belegId: number; dateiname: string; styleCtx: StyleCtx }) {
  const { url, fehler } = useBelegUrl(`/api/creditors/beleg/${belegId}/datei`);
  if (fehler) {
    return <p className="px-4 py-16 text-center text-sm text-red-700 dark:text-red-300">{fehler}</p>;
  }
  if (!url) {
    return <p className={`px-4 py-16 text-center text-sm ${styleCtx.textMuted}`}>Lade den Beleg…</p>;
  }
  return (
    <iframe
      src={url}
      title={`Beleg ${dateiname}`}
      className="h-[60vh] w-full border-0 2xl:h-full 2xl:min-h-[32rem]"
    />
  );
}
