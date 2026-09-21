"""Der Mailentwurf zur Rechnung — Text, Empfänger, Anhang.

Portiert aus ``emailDraftCreator.py`` samt der beiden Vorlagen aus
``config.yml``. Der Wortlaut ist unverändert: er geht seit Jahren so an die
Kundschaft.

## Entwurf, nie Versand

Das Modul erzeugt **ausschliesslich** Entwürfe. Es gibt hier keinen Aufruf von
``send_draft`` und soll auch keinen geben — externe Kommunikation ist im
Pflichtenheft L1, und der Versand bleibt eine Handlung im Postfach. Wer diese
Grenze verschiebt, verschiebt sie für jede Rechnung gleichzeitig.

## Die Anrede ist ein Stammdatum, keine Fallunterscheidung

Die Vorlage hatte zwei Textbausteine (``default`` und ``wir_form``) und wählte
über ``project.template``. Hier steht die Wahl als ``anrede`` am Vertrag, weil
sie eine Eigenschaft des Mandats ist und nicht eine des Textes: bei MCCS wird
im Namen zweier Firmen gearbeitet, also «unsere Leistungen». Der Text folgt
daraus, nicht umgekehrt.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("taskpilot.debitoren.mail")

MONATE = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November",
    12: "Dezember",
}

_ABSENDER = "Anthony Smith\n\nInnoSmith GmbH\nSeelandstrasse 9\n3095 Spiegel b. Bern"

TEXTE = {
    "ich": (
        "Guten Tag\n\n"
        "Im Anhang finden Sie die Rechnung für meine Leistungen aus der "
        "letzten Abrechnungsperiode.\n\n"
        "Bei Fragen oder Anliegen stehe ich Ihnen gerne zur Verfügung.\n\n"
        "Besten Dank und freundliche Grüsse\n" + _ABSENDER
    ),
    "wir": (
        "Guten Tag\n\n"
        "Im Anhang finden Sie die Rechnung für unsere Leistungen aus der "
        "letzten Abrechnungsperiode.\n\n"
        "Bei Fragen oder Anliegen stehen wir Ihnen gerne zur Verfügung.\n\n"
        "Besten Dank und freundliche Grüsse\n" + _ABSENDER
    ),
}


def betreff(bezeichnung: str, jahr: int, monat: int) -> str:
    """«InnoSmith Rechnung Subventix August 2026» — wie seit Jahren."""
    return f"InnoSmith Rechnung {bezeichnung} {MONATE[monat]} {jahr}"


def dateiname(bezeichnung: str, nummer: str, jahr: int, monat: int) -> str:
    """Der Name, unter dem der Anhang im Postfach der Kundschaft ankommt.

    Die Rechnungsnummer steht vorn: sie ist das, wonach in der Buchhaltung der
    Gegenseite gesucht wird, und ein Name, der mit «InnoSmith» beginnt,
    sortiert alle Rechnungen aller Jahre untereinander.
    """
    sauber = bezeichnung.replace("/", "-").strip()
    return f"{nummer} InnoSmith Rechnung {sauber} {MONATE[monat]} {jahr}.pdf"


def text(anrede: str) -> str:
    """Der Mailtext. Eine unbekannte Anrede fällt auf «ich» zurück.

    Der Rückfall ist hier vertretbar und anderswo nicht: «ich» statt «wir» ist
    eine Formulierung, die der Mensch im Entwurf sieht und in zwei Sekunden
    ändert. Ein falscher Betrag wäre etwas anderes.
    """
    if anrede not in TEXTE:
        logger.warning("Unbekannte Anrede «%s» — Text in der Ich-Form", anrede)
    return TEXTE.get(anrede, TEXTE["ich"])


def als_html(roh: str) -> str:
    """Der Text als HTML, Zeilenumbrüche erhalten.

    Outlook stellt reinen Text in Entwürfen unterschiedlich dar, je nach
    Einstellung des Postfachs — die Vorlage setzte deshalb Calibri 11pt
    ausdrücklich, damit der Entwurf aussieht wie jede andere Mail von hier.
    """
    zeilen = roh.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<html><body style="font-family: Calibri, Arial, sans-serif; '
        'font-size: 11pt;">\n'
        + zeilen.replace("\n", "<br>\n")
        + "\n</body></html>"
    )


async def entwurf_anlegen(
    graph, *, vertrag, nummer: str, jahr: int, monat: int, dokument: bytes,
) -> str:
    """Einen Mailentwurf mit der Rechnung im Anhang anlegen. Liefert die Kennung.

    ``vertrag`` ist ein ``Vertragsdaten``-Eintrag; Empfänger und Aktenordner
    kommen über ``kunde_am`` aus der Kundschaft, damit für einen zurückliegenden
    Monat die damalige Gegenpartei gilt.

    **Ohne Empfänger wird kein Entwurf angelegt.** Ein Entwurf ohne Adresse
    sieht im Postfach fertig aus und lässt sich nicht senden — er würde erst
    beim Klick auffallen, also genau dann, wenn niemand mehr Zeit hat.

    **Der Anhang gehört zum Entwurf, nicht daneben.** Scheitert er, wird der
    angefangene Entwurf wieder entfernt: eine Mail ohne Rechnung ist
    gefährlicher als gar keine, weil sie versandfertig wirkt.
    """
    from datetime import date

    kunde = vertrag.kunde_am(date(jahr, monat, 28))
    empfaenger = (kunde.empfaenger or "").strip()
    if not empfaenger:
        raise ValueError(
            f"Für «{vertrag.bezeichnung}» ist bei der Kundschaft "
            f"«{kunde.schluessel}» kein Empfänger hinterlegt"
        )

    entwurf = await graph.create_draft(
        subject=betreff(vertrag.bezeichnung, jahr, monat),
        body_html=als_html(text(vertrag.anrede)),
        to_recipients=[empfaenger],
    )
    kennung = str(entwurf.get("id") or "")
    if not kennung:
        raise RuntimeError(
            f"Graph lieferte keine Kennung für den Entwurf zu {nummer}"
        )

    try:
        await graph.add_attachment(
            kennung, dateiname(vertrag.bezeichnung, nummer, jahr, monat), dokument
        )
    except Exception:
        logger.warning(
            "Anhang für %s gescheitert — Entwurf %s wird entfernt", nummer, kennung
        )
        try:
            await graph.delete_message(kennung)
        except Exception:  # noqa: BLE001 - der ursprüngliche Fehler zählt
            logger.error(
                "Entwurf %s liess sich nicht entfernen — er liegt ohne Anhang "
                "im Postfach", kennung,
            )
        raise

    return kennung
