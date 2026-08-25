"""Was gilt, wenn Text das Haus verlaesst.

Diese Datei hat einen Anlass. Am 24.08.2026 standen im Backend **drei**
ausgeschriebene Entitaetenlisten fuer denselben Zweck -- im Hermes-Worker, in der
Finanzanalyse und in der Content-API. Alle drei waren voneinander abgeschrieben,
alle drei nannten sechs Typen, und alle drei uebergingen ``AHV`` und ``UID``, die
contentConverter laengst erkannte. Niemand hatte sie abgewaehlt; sie waren nie
dazugekommen, weil eine ausgeschriebene Liste jede spaetere Erweiterung der
Vorgabe stillschweigend uebergeht.

Das ist die Fehlerart, gegen die dieses Modul steht: nicht eine falsche
Entscheidung, sondern eine Entscheidung, die an drei Orten getroffen wurde und
darum an zwei davon veraltet ist.

**Die tragende Unterscheidung** ist nicht «Cloud oder lokal», sondern
**«sieht ein Mensch den Text, bevor er hinausgeht?»**

- **Beaufsichtigt** (Finanzanalyse: der Prompt steht in der Vorschau, der Mensch
  gibt ihn frei) -- Restbestaende sind eine **Warnung**. Er kann sie ansehen und
  entscheiden.
- **Unbeaufsichtigt** (Agent-Job mit Cloud-Override, E-Mail-Entwurf: der Text
  geht raus, waehrend niemand hinsieht) -- Restbestaende sind ein **Abbruch**.
  Der Auftrag laeuft dann lokal.

Dieselben Werte, verschiedene Konsequenz. Wer beides gleich behandelt, macht
entweder den beaufsichtigten Weg unbrauchbar oder den unbeaufsichtigten unsicher.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ENTITAETEN = ["PERSON", "ORG", "LOCATION", "EMAIL", "PHONE", "IBAN", "AHV", "UID"]
"""Was maskiert wird, bevor Text das Haus verlaesst.

``AHV`` und ``UID`` gehoeren dazu, nicht in eine Zusatzoption: Wer anonymisiert,
ohne sie zu nennen, will sie erst recht nicht im Text stehen lassen. Beide tragen
eine Pruefziffer und sind damit praktisch fehlalarmfrei -- es gibt keinen Grund,
sie abzuwaehlen, und eine durchgerutschte AHV-Nummer bei einem Cloud-Anbieter ist
die eindeutige Kennung eines Menschen.
"""

SCHWELLE = 0.25
"""Erkennungsschwelle fuer den Weg nach draussen.

Tiefer als die Vorgabe (0.4), weil die Fehlerarten nicht gleichwertig sind: Ein
zu viel maskiertes Sachwort kostet das Modell etwas Kontext und ist in der
Vorschau sichtbar; ein uebersehener Name ist ein Datenabfluss, den niemand
bemerkt. Derselbe Grund, aus dem das GSW-Cockpit auf dem Profil ``recall`` steht.
"""


async def maskiere(text: str) -> tuple[str, str, list[dict], list[str]]:
    """Maskiert Text nach der Politik dieses Hauses.

    Liefert ``(maskierter_text, session_id, diff, restbestaende)``. Das Mapping
    liegt im ``mapping_store`` und wird nur ueber die Session-Kennung angefasst --
    die Klartextwerte reisen nicht durch Antwortkoerper oder Frontend-Zustand.

    ``restbestaende`` sind Bruchstuecke echter Werte, die im maskierten Text
    stehen geblieben sind -- der typische Fall ist die Teilnennung: «Egli
    Immobilien AG» wird ersetzt, das alleinstehende «Eglis» zwei Saetze weiter
    nicht, weil die Erkennung dort keine Firma sieht. Sie **automatisch**
    nachzuziehen hiesse, an fremdem Text zu raten; ein falsch zugeordneter Name
    waere schlimmer als ein gemeldeter.

    Was aus der Meldung folgt, entscheidet der Aufrufer -- siehe Modulkopf.

    Wirft, wenn die Maskierung selbst scheitert. Ein Aufrufer, der Text nach
    draussen gibt, darf das nicht auffangen und trotzdem senden.
    """
    from ai9 import content_converter as cc
    from ai9 import mapping_store

    ergebnis = await cc.call_tool(
        "anonymize_content",
        text=text,
        entities=",".join(ENTITAETEN),
        language="de",
        threshold=SCHWELLE,
    )
    if not isinstance(ergebnis, dict):
        raise RuntimeError("Anonymisierung lieferte kein Mapping")

    maskiert = ergebnis.get("anonymized_text") or ""
    if not maskiert:
        raise RuntimeError("Anonymisierung lieferte leeren Text")

    schluessel = ergebnis.get("mapping_keys", {})

    try:
        # call_tool_liste und nicht call_tool: Bei genau einem Fund liefert die
        # MCP-Bruecke eine Zeichenkette statt einer Liste. Ein str(r) je Element
        # zerlegte «Mueller» dann in sechs Buchstaben -- gemeldet wuerde etwas,
        # das niemand als den uebersehenen Namen erkennt.
        reste = await cc.call_tool_liste(
            "find_residual_originals", text=maskiert, mapping_keys=schluessel
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed
        # Nicht pruefbar ist nicht «sauber». Der Aufrufer soll dieselbe
        # Konsequenz ziehen wie bei einem echten Fund.
        logger.warning("Restbestandspruefung fehlgeschlagen (%s) -- gilt als Fund", exc)
        reste = ["(Pruefung fehlgeschlagen)"]

    session_id, diff = mapping_store.store_mapping(schluessel)
    return maskiert, session_id, diff, reste


async def bilde_zurueck(text: str, session_id: str) -> tuple[str, list[str]]:
    """Setzt die Originalwerte wieder ein und meldet Rueckstaende.

    Liefert ``(text, rueckstaende)``. Ein Rueckstand ist ein Ersatzname, der die
    Rueckbildung ueberlebt hat -- weil das Modell ihn gebeugt oder verkuerzt
    geschrieben hat. Er ist der stillste aller Fehler: ein erfundener Name, der
    genauso plausibel aussieht wie ein echter, und niemand hat Anlass zu zweifeln.

    Scheitert die Rueckbildung, wird der **maskierte** Text mit einem Rueckstand
    gemeldet statt stillschweigend ausgegeben. Ein Bericht voller plausibler
    fremder Namen ist schlimmer als ein Bericht mit einer Warnung darauf.
    """
    from ai9 import content_converter as cc
    from ai9 import mapping_store

    schluessel = mapping_store.get_mapping_keys(session_id) if session_id else None
    if not schluessel:
        # Der Store haelt Mappings nur begrenzt (TTL) und ueberlebt keinen
        # Neustart. Eine lange Analyse kann ihn ueberdauern -- dann steht hier
        # maskierter Text, und das muss sichtbar bleiben.
        return text, ["(Mapping nicht mehr verfuegbar)"]

    try:
        klar = await cc.call_tool("deanonymize_content", text=text, mapping_keys=schluessel)
        klar = klar if isinstance(klar, str) else str(klar)
    except Exception as exc:  # noqa: BLE001
        logger.error("Rueckbildung fehlgeschlagen: %s", exc)
        return text, ["(Rueckbildung fehlgeschlagen)"]

    try:
        rueckstaende = await cc.call_tool_liste(
            "find_residual_fakes", text=klar, mapping_keys=schluessel
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rueckstandspruefung fehlgeschlagen (%s) -- gilt als Fund", exc)
        rueckstaende = ["(Pruefung fehlgeschlagen)"]

    return klar, rueckstaende
