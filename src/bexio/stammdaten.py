"""Kontenplan und Geschaeftsjahre als eigene Tabellen.

Beides sind Stammdaten, und beide beantworten fuer sich genommen keine Frage. Sie
stehen trotzdem im Datenraum, weil ohne sie **zwei stille Fehler** unvermeidlich
sind:

* Das Journal fuehrt Konten nur als Kennung. Ohne Kontenplan ist
  ``debit_account_id: 227`` eine nackte Zahl, und die Frage «wofuer geben wir Geld
  aus» aus der Buchhaltung heraus nicht beantwortbar.
* Ein Geschaeftsjahr ist entweder abgeschlossen oder offen. Am 03.09.2026 stand
  2025 mit 401'459 CHF Aufwand als **abgeschlossenes** Jahr da, 2026 mit 266'476
  CHF als **offenes** Jahr von acht Monaten. Wer beide Zahlen nebeneinanderstellt,
  sieht einen Einbruch von einem Drittel, wo ein Teiljahr steht. Der Vergleich ist
  arithmetisch richtig und inhaltlich falsch -- die gefaehrlichste Kombination.

Beide Tabellen sind winzig (197 bzw. 9 Zeilen) und aendern sich selten. Sie kosten
nichts und nehmen dem Agenten zwei Annahmen ab, die er sonst raten muesste.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("taskpilot.bexio.stammdaten")

# Kontoklasse aus der ersten Ziffer der Kontonummer -- Schweizer KMU-Kontenrahmen.
#
# Das ist eine Bezugnahme auf eine deklarierte Norm, keine Worterkennung: die erste
# Ziffer *ist* die Klasse, sie wird nicht aus dem Kontonamen erschlossen. Die Menge
# ist geschlossen und aendert sich nicht, weil ein neues Konto dazukommt.
KONTOKLASSEN: dict[str, str] = {
    "1": "aktiven",
    "2": "passiven",
    "3": "ertrag",
    "4": "aufwand_material",
    "5": "personalaufwand",
    "6": "betriebsaufwand",
    "7": "nebenerfolg",
    "8": "ausserordentlich",
    "9": "abschluss",
}

# Welche Klassen als Aufwand gelten, wenn das Konto auf der **Sollseite** steht.
#
# Die Grenze verlaeuft bei 4000 bis 8999. Klasse 7 und 8 koennen je nach Buchungs-
# seite Ertrag oder Aufwand sein; auf der Sollseite sind sie Aufwand. Deshalb
# entscheidet nicht die Klasse allein, sondern Klasse **und** Seite -- und genau
# so ist ``ist_aufwand`` im Journal definiert.
AUFWANDSKLASSEN: frozenset[str] = frozenset({
    "aufwand_material", "personalaufwand", "betriebsaufwand",
    "nebenerfolg", "ausserordentlich",
})


def konto_beschriftung(eintrag: dict | None) -> str:
    """Ein Konto als «Nummer Name», z.B. ``6570 Software``.

    Die Nummer gehoert dazu, und zwar in **jeder** Tabelle gleich. Sie ordnet
    (4000 vor 6500), sie ist eindeutig, wo zwei Konten aehnlich heissen, und sie
    macht die Spalte im Journal formgleich mit ``bexio_kreditoren.konto``. Zwei
    Schreibweisen fuer denselben Gegenstand waeren genau die Sorte Unterschied,
    die ein Modell fuer einen Bedeutungsunterschied haelt.
    """
    if not eintrag:
        return ""
    teile = (str(eintrag.get("konto_nr") or ""), str(eintrag.get("konto") or ""))
    return " ".join(t for t in teile if t)


def kontoklasse(nummer: str) -> str:
    """Klasse eines Kontos aus seiner Nummer.

    Unbekannte oder nicht numerische Nummern ergeben ``unbekannt`` statt einer
    geratenen Einordnung. Ein Konto falsch einzusortieren waere schlimmer, als es
    gar nicht einzusortieren: die Summe bliebe plausibel.
    """
    ziffer = (nummer or "").strip()[:1]
    return KONTOKLASSEN.get(ziffer, "unbekannt")


async def kontenplan_laden(client) -> tuple[list[dict], dict[int, dict]]:
    """Kontenplan als Tabellenzeilen **und** als Nachschlagewerk fuer das Journal.

    Zwei Rueckgaben aus einem Abruf, weil beide dieselbe Antwort brauchen: die
    Tabelle beantwortet «welche Aufwandskonten gibt es», das Nachschlagewerk loest
    die Kennungen im Journal auf. Zweimal abzurufen waere Verschwendung, und zwei
    Abrufe koennten auseinanderlaufen.
    """
    konten = await client.list_accounts(limit=2000)

    zeilen: list[dict] = []
    verzeichnis: dict[int, dict] = {}
    for k in konten:
        kennung = k.get("id")
        if kennung is None:
            continue
        nummer = str(k.get("account_no") or "").strip()
        name = str(k.get("name") or "").strip()
        eintrag = {
            "konto_id": int(kennung),
            "konto_nr": nummer,
            "konto": name,
            "klasse": kontoklasse(nummer),
            "aktiv": bool(k.get("is_active")),
            "gesperrt": bool(k.get("is_locked")),
        }
        zeilen.append(eintrag)
        verzeichnis[int(kennung)] = eintrag

    zeilen.sort(key=lambda z: z["konto_nr"])
    return zeilen, verzeichnis


async def geschaeftsjahre_laden(client) -> list[dict]:
    """Geschaeftsjahre mit Abschlussstand.

    ``ist_abgeschlossen`` wird als eigene Wahrheitsspalte gefuehrt und nicht dem
    Leser ueberlassen, der sonst ``status`` gegen eine Zeichenkette vergleichen
    muesste, die er nicht kennt.
    """
    jahre = await client.get_business_years()

    zeilen: list[dict] = []
    for j in jahre:
        von = str(j.get("start") or "")[:10]
        status = str(j.get("status") or "").strip().lower() or "unbekannt"
        zeilen.append({
            "jahr": int(von[:4]) if von[:4].isdigit() else None,
            "von": von or None,
            "bis": str(j.get("end") or "")[:10] or None,
            "status": status,
            "ist_abgeschlossen": status == "closed",
            "abgeschlossen_am": str(j.get("closed_at") or "")[:10] or None,
        })

    zeilen.sort(key=lambda z: z["von"] or "")
    if not any(z["ist_abgeschlossen"] for z in zeilen):
        logger.warning(
            "Kein Geschaeftsjahr als abgeschlossen gemeldet -- Jahresvergleiche "
            "koennen offene Teiljahre gegen volle Jahre stellen"
        )
    return zeilen
