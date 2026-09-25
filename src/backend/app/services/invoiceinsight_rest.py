"""Der Kreditorenbestand über die HTTP-Schnittstelle von InvoiceInsight.

Die Route ``/rechnungen`` reicht ihren Bestand durch dieselbe Kernfunktion
``export_page()``, die auch der Abzug für den Agenten benutzt, und liefert
deshalb dieselben Spalten. Sie liest den Bestand aber bei **jedem** Aufruf
frisch aus der Datenbank, statt sich aus einem einmal aufgebauten Datenrahmen
zu bedienen -- für einen Abgleich-Worker, der eine Parquet-Datei schreibt, ist
genau das die richtige Eigenschaft.

Die Vollständigkeit wird **geprüft, nicht angenommen**: ``total`` aus der ersten
Seite ist die Sollzahl, und weicht die Anzahl geholter Zeilen davon ab, steht
das im Befund und wandert in den Katalog. Ohne diesen Abgleich sieht ein
abgebrochener Abzug aus wie ein kleiner Bestand.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger("taskpilot.invoiceinsight.rest")

SEITENGROESSE = 500

# Notbremse gegen einen Server, der nie eine leere Seite liefert. Eine
# Endlosschleife im Abgleich-Worker wäre schlimmer als ein unvollständiger
# Abzug, denn sie fällt erst auf, wenn nichts mehr geht.
HOECHSTZAHL_SEITEN = 200

ZEITGRENZE = httpx.Timeout(120.0, connect=10.0)


async def rechnungen_holen(
    basis_url: str,
    token: str,
    *,
    seitengroesse: int = SEITENGROESSE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Den ganzen Rechnungsbestand blätternd holen -- Zeilen und Befund.

    Args:
        basis_url: Wurzel der Schnittstelle, etwa ``http://127.0.0.1:8056``.
        token: Bearer-Token der Schnittstelle.
        seitengroesse: Zeilen je Abruf.

    Returns:
        Die Zeilen und den Befund. Der Befund führt ``gemeldet``, ``geholt``
        und ``stand``, dazu die Spaltenbedeutung, die nicht auswertbaren Belege
        und -- falls die Zahlen auseinanderlaufen -- ``unvollstaendig``.

    Raises:
        RuntimeError: Wenn die Schnittstelle nicht antwortet, mit einem Fehler
            antwortet oder etwas anderes als eine Bestandsseite liefert. Ein
            stilles Ausweichen gäbe es hier nicht: eine Quelle, die nichts
            liefert, muss im Katalog als gescheitert stehen.
    """
    wurzel = basis_url.rstrip("/")
    kopf = {"Authorization": f"Bearer {token}"}

    zeilen: list[dict[str, Any]] = []
    soll = 0
    stand = ""
    bedeutung: dict[str, str] = {}
    fehlend: dict[str, int] = {}
    einzeln: list[dict[str, Any]] | None = None
    offset = 0

    async with httpx.AsyncClient(timeout=ZEITGRENZE, headers=kopf) as client:
        for _ in range(HOECHSTZAHL_SEITEN):
            seite = await _seite_holen(client, wurzel, offset, seitengroesse)

            if offset == 0:
                soll = int(seite.get("total") or 0)
                stand = str(seite.get("as_of") or "")
                bedeutung = (seite.get("schema") or {}).get("meaning") or {}
                fehlend = seite.get("nicht_auswertbar") or {}
                einzeln = seite.get("nicht_auswertbare_belege")

            teil = seite.get("rows") or []
            if not teil:
                break
            zeilen.extend(teil)
            offset += len(teil)
        else:
            logger.warning(
                "InvoiceInsight-Abzug nach %d Seiten abgebrochen", HOECHSTZAHL_SEITEN
            )

    befund: dict[str, Any] = {"gemeldet": soll, "geholt": len(zeilen), "stand": stand}
    if bedeutung:
        befund["spalten_bedeutung"] = bedeutung
    if fehlend:
        # Belege, die es gibt und die nicht im Bestand stehen. Gehört in den
        # Katalog, nicht ins Protokoll: ein blinder Fleck, den niemand nennt,
        # sieht aus wie Vollständigkeit.
        befund["nicht_auswertbare_belege"] = fehlend
    if einzeln is not None:
        # Auch leer: eine ältere Schnittstelle kennt die Liste nicht, und dann
        # darf der Eingang nicht «nichts unlesbar» behaupten.
        befund["nicht_auswertbar_einzeln"] = einzeln
    if soll and len(zeilen) < soll:
        befund["unvollstaendig"] = f"{len(zeilen)} von {soll} Zeilen"
        logger.warning(
            "InvoiceInsight-Abzug unvollständig: %d von %d Zeilen", len(zeilen), soll
        )
    elif soll and len(zeilen) > soll:
        # Mehr Zeilen als angekündigt heisst nicht «unvollständig», sondern
        # «der Bestand hat sich während des Blätterns bewegt» -- am 22.09.2026
        # beobachtet, als über OneDrive neue Belege eintrafen. Beides in einen
        # Topf zu werfen wäre eine Meldung, die das Gegenteil dessen sagt, was
        # geschehen ist. Wer über Seiten blättert, während vorne eingefügt
        # wird, kann ausserdem eine Zeile überspringen: der Hinweis gehört in
        # den Katalog, damit eine überraschende Zahl eine Spur hat.
        befund["bestand_bewegt"] = f"{len(zeilen)} geholt, {soll} angekündigt"
        logger.info(
            "InvoiceInsight-Bestand wuchs während des Abzugs: %d geholt, %d angekündigt",
            len(zeilen), soll,
        )
    return zeilen, befund


async def beleg_holen(basis_url: str, token: str, beleg_id: int) -> dict[str, Any]:
    """Einen einzelnen Beleg mit allen gelesenen Werten holen.

    Die Maske füllt sich hieraus und nicht aus der Liste: die Liste zeigt eine
    Projektion mit umbenannten Spalten, der Einzelabruf die Felder, die eine
    Korrektur auch wieder entgegennimmt. Aus einer Projektion heraus zu
    bearbeiten hiesse, die Namen zweimal zu übersetzen.
    """
    wurzel = basis_url.rstrip("/")
    ziel = f"{wurzel}/rechnungen/{beleg_id}"
    kopf = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=ZEITGRENZE, headers=kopf) as client:
        try:
            antwort = await client.get(ziel)
        except httpx.HTTPError as fehler:
            raise RuntimeError(f"InvoiceInsight unter {ziel} nicht erreichbar: {fehler}") from fehler
    _status_pruefen(antwort, ziel)
    return antwort.json()


async def beleg_korrigieren(
    basis_url: str,
    token: str,
    beleg_id: int,
    felder: dict[str, Any],
    geprueft_von: str,
) -> dict[str, Any]:
    """Einzelne Werte eines Belegs richtigstellen.

    Gesendet wird **nur**, was der Mensch geändert hat. Die Gegenseite
    unterscheidet zwischen «nicht mitgeschickt» und «auf leer gesetzt»; eine
    Maske, die alle Felder mitschickt, löschte deshalb jede Angabe, die sie
    selbst nicht kennt.

    ``geprueft_von`` setzt der Aufrufer aus der angemeldeten Person und nie aus
    der Eingabe. Ein Urheber, den man frei wählen kann, ist keine Spur.
    """
    wurzel = basis_url.rstrip("/")
    ziel = f"{wurzel}/rechnungen/{beleg_id}"
    kopf = {"Authorization": f"Bearer {token}"}
    rumpf = {**felder, "geprueft_von": geprueft_von}

    async with httpx.AsyncClient(timeout=ZEITGRENZE, headers=kopf) as client:
        try:
            antwort = await client.patch(ziel, json=rumpf)
        except httpx.HTTPError as fehler:
            raise RuntimeError(f"InvoiceInsight unter {ziel} nicht erreichbar: {fehler}") from fehler
    _status_pruefen(antwort, ziel)
    return antwort.json()


async def belegdatei_holen(
    basis_url: str, token: str, beleg_id: int
) -> tuple[bytes, str, str]:
    """Die Originaldatei durchreichen -- Inhalt, Medientyp, Dateiname.

    Durchgereicht und nicht aus dem Dateisystem gelesen: das Backend läuft im
    Container, und der Archivpfad aus dem Abzug zeigt auf den Host. Ein Einhängen
    wäre nur eine zweite Abhängigkeit zur OneDrive-Ablage -- und genau die
    Synchronisation macht seit dem 21.09.2026 Mühe. Die Schnittstelle kennt die
    Datei, also holt sie sie.
    """
    wurzel = basis_url.rstrip("/")
    ziel = f"{wurzel}/rechnungen/{beleg_id}/beleg"
    kopf = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=ZEITGRENZE, headers=kopf) as client:
        try:
            antwort = await client.get(ziel)
        except httpx.HTTPError as fehler:
            raise RuntimeError(
                f"InvoiceInsight unter {ziel} nicht erreichbar: {fehler}"
            ) from fehler
    _status_pruefen(antwort, ziel)

    name = f"beleg-{beleg_id}.pdf"
    if verfuegung := antwort.headers.get("content-disposition"):
        # Der Dateiname ist die einzige lesbare Auskunft im Herunterladen-Dialog.
        for teil in verfuegung.split(";"):
            teil = teil.strip()
            if teil.startswith("filename="):
                name = teil.removeprefix("filename=").strip('"') or name
    typ = antwort.headers.get("content-type") or "application/octet-stream"
    return antwort.content, typ, name


class SchnittstellenFehler(RuntimeError):
    """Ein Fehler, den die Gegenseite benannt hat -- Status und Wortlaut bleiben.

    Ohne diese beiden Angaben wird aus «Feld x ist kein gültiges Datum» ein
    allgemeines «Fehler beim Speichern», und der Mensch vor der Maske weiss
    nicht, was er ändern soll.
    """

    def __init__(self, status: int, meldung: str, rohdetail: Any = None):
        super().__init__(meldung)
        self.status = status
        self.meldung = meldung
        # Bei 422 ist ``detail`` eine Liste mit ``loc`` je Feld. Nur in dieser
        # Form kann die Maske den Fehler neben das betroffene Feld schreiben;
        # einmal durch ``str()`` gedreht, ist die Zuordnung verloren.
        self.rohdetail = rohdetail if rohdetail is not None else meldung


def _status_pruefen(antwort: httpx.Response, ziel: str) -> None:
    """Fehlerstatus mit dem Wortlaut der Gegenseite weiterreichen."""
    if antwort.is_success:
        return
    try:
        detail = antwort.json().get("detail")
    except ValueError:
        detail = None
    if detail is None:
        detail = antwort.text or f"Status {antwort.status_code}"
    raise SchnittstellenFehler(antwort.status_code, str(detail), detail)


async def _seite_holen(
    client: httpx.AsyncClient, wurzel: str, offset: int, limit: int
) -> dict[str, Any]:
    """Eine Seite abrufen und auf ihre Form prüfen."""
    ziel = f"{wurzel}/rechnungen"
    try:
        antwort = await client.get(ziel, params={"offset": offset, "limit": limit})
        antwort.raise_for_status()
    except httpx.HTTPStatusError as fehler:
        raise RuntimeError(
            f"InvoiceInsight antwortete auf {ziel} mit "
            f"{fehler.response.status_code}"
        ) from fehler
    except httpx.HTTPError as fehler:
        raise RuntimeError(f"InvoiceInsight unter {ziel} nicht erreichbar: {fehler}") from fehler

    seite = antwort.json()
    if not isinstance(seite, dict) or "rows" not in seite:
        raise RuntimeError(
            f"{ziel} lieferte keine Bestandsseite -- läuft die passende Fassung "
            "der Schnittstelle?"
        )
    return seite
