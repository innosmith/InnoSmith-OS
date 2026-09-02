/**
 * Das Signalbriefing im Cockpit -- ein Knopf und ein Satz.
 *
 * Die erste Fassung listete den Aufmacher plus fünf Themenzeilen mit Zustandsmarken. Das
 * war die Signale-Seite in klein: dieselbe Leseaufgabe auf weniger Platz, und weil der
 * Text ohnehin abgeschnitten war, blieb von jedem Thema nur eine Überschrift ohne
 * Aussage. Fünf Überschriften ohne Aussage sind schlechter als keine.
 *
 * Diese Fassung fragt stattdessen, was jemand um sieben Uhr im Cockpit damit tun kann.
 * Die Antwort ist: anhören, während er etwas anderes macht. Also steht der Abspielknopf
 * links und gross, daneben die Dauer -- «habe ich jetzt drei Minuten?» ist die einzige
 * Frage, die vor dem Druck zu beantworten ist. Der Aufmacher steht darunter als
 * Begründung, warum es sich lohnt. Alles Weitere ist einen Klick entfernt.
 *
 * Der Ton läuft nicht in dieser Kachel, sondern in der Tonspur der Rahmenseite. Sonst
 * bräche er ab, sobald jemand ins Projekt wechselt -- und genau dorthin geht man,
 * während man zuhört.
 */

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { texte, uhr, useTonspur } from '@signa/reader';
import type { Briefing } from '@signa/reader';
import { erstelleZugang } from '../lib/signaZugang';

interface SignalbriefingCardProps {
  cardClass: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  hasBg: boolean;
}

function tagesdatum(iso: string): string {
  return new Date(iso).toLocaleDateString('de-CH', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  });
}

/** Ob der Text von heute ist. Sonst hat der Morgenlauf nicht stattgefunden. */
function vonHeute(iso: string): boolean {
  const d = new Date(iso);
  const jetzt = new Date();
  return (
    d.getFullYear() === jetzt.getFullYear() &&
    d.getMonth() === jetzt.getMonth() &&
    d.getDate() === jetzt.getDate()
  );
}

export function SignalbriefingCard({
  cardClass,
  textPrimary,
  textSecondary,
  textMuted,
  hasBg,
}: SignalbriefingCardProps) {
  const navigate = useNavigate();
  const ton = useTonspur();
  const [briefing, setBriefing] = useState<Briefing | null>(null);

  const lade = useCallback(() => {
    // Ein Ausfall von Signa bleibt hier still: Die Kachel verschwindet, das Cockpit
    // bleibt. Sichtbar wird der Ausfall auf der Signale-Seite, wo er hingehoert.
    erstelleZugang()
      .getBriefings({ limit: 1 })
      .then((fassungen) => setBriefing(fassungen[0] ?? null))
      .catch(() => {});
  }, []);

  useEffect(lade, [lade]);

  if (!briefing) return null;

  const oeffne = () => navigate('/signale?tab=briefing');
  const aktuell = vonHeute(briefing.covers_to);
  const folge = briefing.podcast;
  const klingt = ton?.laeuft?.id === briefing.id && ton.spielt;
  // Der laufende Stand schlägt den gespeicherten: Wer gerade zuhört, sieht die Zahl
  // mitlaufen, statt den Stand von vor fünf Sekunden.
  const stand =
    ton?.laeuft?.id === briefing.id ? ton.position_s : (folge?.position_s ?? 0);

  const spiele = () => {
    if (!ton || !folge) return;
    void ton.wechsle({
      id: briefing.id,
      titel: texte.folgeTitel(tagesdatum(briefing.covers_to)),
      dauer_s: folge.duration_s,
    });
  };

  return (
    <section className={`rounded-xl border p-4 ${cardClass}`}>
      <div className="mb-3 flex items-center justify-between">
        <h2 className={`text-sm font-semibold uppercase tracking-wider ${textSecondary}`}>
          Signalbriefing
          {/* Ein alter Text darf nicht wie ein frischer aussehen: Der Morgenlauf kann
              ausgefallen sein, und dann ist das Datum die einzige Warnung. */}
          {!aktuell && (
            <span className={`ml-2 text-[11px] font-normal normal-case ${textMuted}`}>
              {tagesdatum(briefing.covers_to)}
            </span>
          )}
        </h2>
        <button
          onClick={oeffne}
          className={`text-xs font-medium ${hasBg ? 'text-white/60 hover:text-white' : 'text-indigo-600 hover:text-indigo-800 dark:text-indigo-400'}`}
        >
          Nachlesen →
        </button>
      </div>

      <div className="flex items-start gap-3">
        {folge && ton && (
          <button
            onClick={spiele}
            aria-label={klingt ? 'Pause' : 'Podcast abspielen'}
            className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-full transition-colors ${hasBg ? 'bg-white/15 text-white hover:bg-white/25' : 'bg-indigo-600 text-white hover:bg-indigo-700'}`}
          >
            {klingt ? (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8 5h3v14H8zM13 5h3v14h-3z" />
              </svg>
            ) : (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8 5.14v13.72L19 12z" />
              </svg>
            )}
          </button>
        )}

        <div className="min-w-0 flex-1">
          <p
            className={`cursor-pointer text-[15px] leading-relaxed ${textPrimary}`}
            onClick={oeffne}
          >
            {briefing.aufmacher}
          </p>

          <p className={`mt-2 text-[11px] ${textMuted}`}>
            {/* Drei Angaben, jede beantwortet eine Frage: Wie lange dauert es, bin ich
                schon durch, worauf beruht es. */}
            {folge ? texte.folgeDauer(folge.duration_s) : 'Keine Folge'}
            {folge?.heard && ` · ${texte.folgeGehoert}`}
            {folge && !folge.heard && stand > 10 && ` · weiter bei ${uhr(stand)}`}
            {' · '}
            {texte.briefingGrundlage(briefing.signal_count, briefing.abschnitte.length)}
          </p>
        </div>
      </div>
    </section>
  );
}
