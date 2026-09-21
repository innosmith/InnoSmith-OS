"""Bexio Buchhaltungs-API Client (async, httpx-basiert).

Authentifizierung via Bearer Token (persönlicher API-Token).
API v2.0 für Kontakte, Aufträge, Projekte, Kontenplan.
API v3.0 für /users/me, Banking, Journal.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("taskpilot.bexio")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0

BASE_URL_V2 = "https://api.bexio.com/2.0"
BASE_URL_V3 = "https://api.bexio.com/3.0"
BASE_URL_V4 = "https://api.bexio.com/4.0"

# Wie viele Einzelabrufe gleichzeitig laufen duerfen. Die Kreditorenliste gibt
# weder Lieferantenkennung noch Kontierung her (siehe kreditoren.py), also braucht
# jede Rechnung einen eigenen Abruf. Acht parallel holen 435 Rechnungen in rund
# acht Sekunden, ohne dass Bexio mit 429 antwortet.
DETAIL_PARALLEL = 8


def decode_token_expiry(token: str) -> dict | None:
    """Liest das Ablaufdatum (exp-Claim) aus einem Bexio-Token.

    Bexio Personal Access Tokens (PAT) und OAuth2-Access-Tokens sind JWTs mit
    einem 'exp'-Claim. Ein PAT ist ab Erstellung 6 Monate gueltig; laeuft er ab,
    liefert die API stillschweigend 401 -- ohne jede Code-Aenderung.

    Diese Funktion dekodiert ausschliesslich den (nicht signierten) Payload-Teil,
    um den Ablauf-Zeitstempel zu lesen. Sie validiert die Signatur NICHT und gibt
    den Token-Wert selbst nicht zurueck.

    Returns:
        dict mit ``expires_at`` (ISO-8601), ``days_remaining`` (float) und
        ``is_expired`` (bool) -- oder ``None``, wenn der Token kein JWT mit
        exp-Claim ist (z.B. ein alter statischer API-Token).
    """
    if not token or token.count(".") != 2:
        return None
    payload_seg = token.split(".")[1]
    payload_seg += "=" * (-len(payload_seg) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_seg))
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    return {
        "expires_at": expires_at.isoformat(),
        "days_remaining": round(delta.total_seconds() / 86400, 1),
        "is_expired": delta.total_seconds() <= 0,
    }


def _status_kennung(status: str | int) -> int:
    """Rechnungsstatus als Zahl -- akzeptiert Kennung oder Beschriftung.

    Die Zuordnung steht in ``rechnungen.py`` und nur dort; hier wird sie nur
    nachgeschlagen. Zwei Tabellen waeren zwei Wahrheiten.
    """
    if isinstance(status, int) or str(status).isdigit():
        return int(status)
    from rechnungen import RECHNUNGSSTATUS

    gesucht = str(status).strip().lower()
    for kennung, (beschriftung, _) in RECHNUNGSSTATUS.items():
        if beschriftung == gesucht:
            return kennung
    erlaubt = ", ".join(b for b, _ in RECHNUNGSSTATUS.values())
    raise ValueError(f"Unbekannter Rechnungsstatus '{status}'. Erlaubt: {erlaubt} oder die Kennung")


@dataclass
class BexioConfig:
    api_token: str = ""

    @classmethod
    def from_env(cls) -> "BexioConfig":
        return cls(api_token=os.environ.get("TP_BEXIO_API_TOKEN", ""))

    @property
    def is_configured(self) -> bool:
        return bool(self.api_token)


class BexioClient:
    """Async Bexio API Client mit Rate-Limit-Retry."""

    def __init__(self, config: BexioConfig | None = None):
        self.config = config or BexioConfig.from_env()
        self._http: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self.config.api_token}",
                    "Accept": "application/json",
                },
                timeout=30.0,
            )
        return self._http

    async def _request(
        self, method: str, url: str, params: dict | None = None, json_body: dict | list | None = None
    ) -> dict | list:
        client = await self._ensure_client()
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.request(method, url, params=params, json=json_body)
                if resp.status_code == 429:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning("Bexio Rate-Limit (429), Retry in %.1fs", delay)
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                if resp.status_code == 204:
                    return {}
                return resp.json()
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                logger.warning("Bexio HTTP-Fehler: %s, Retry %d", exc, attempt + 1)
                await asyncio.sleep(RETRY_BASE_DELAY)
        return {}

    async def _get_v2(self, path: str, params: dict | None = None) -> dict | list:
        return await self._request("GET", f"{BASE_URL_V2}{path}", params=params)

    async def _post_v2(self, path: str, body: dict | list) -> dict | list:
        return await self._request("POST", f"{BASE_URL_V2}{path}", json_body=body)

    async def _get_v3(self, path: str, params: dict | None = None) -> dict | list:
        return await self._request("GET", f"{BASE_URL_V3}{path}", params=params)

    async def _get_v4(self, path: str, params: dict | None = None) -> dict | list:
        return await self._request("GET", f"{BASE_URL_V4}{path}", params=params)

    # ── Token-Ablauf ─────────────────────────────────────────

    def token_expiry(self) -> dict | None:
        """Ablauf-Info des konfigurierten Tokens (siehe ``decode_token_expiry``)."""
        return decode_token_expiry(self.config.api_token)

    # ── Verbindungstest ──────────────────────────────────────

    async def test_connection(self) -> dict:
        """Verbindung testen via /3.0/users/me (nicht in v2 vorhanden)."""
        try:
            data = await self._get_v3("/users/me")
            if isinstance(data, dict):
                return {
                    "ok": bool(data.get("id")),
                    "name": f"{data.get('firstname', '')} {data.get('lastname', '')}".strip(),
                    "email": data.get("email", ""),
                }
        except Exception:
            pass
        return {"ok": False}

    # ── Kontakte ─────────────────────────────────────────────

    async def list_contacts(self, limit: int = 50, offset: int = 0) -> list[dict]:
        params = {"limit": str(limit), "offset": str(offset)}
        data = await self._get_v2("/contact", params)
        return data if isinstance(data, list) else []

    async def get_contact(self, contact_id: int) -> dict:
        data = await self._get_v2(f"/contact/{contact_id}")
        return data if isinstance(data, dict) else {}

    async def create_contact(self, payload: dict) -> dict:
        data = await self._post_v2("/contact", payload)
        return data if isinstance(data, dict) else {}

    async def search_contact_by_name(self, name: str) -> list[dict]:
        """Kontakte per POST /contact/search nach Name suchen.

        ``like`` sucht bei Bexio bereits als Teiltreffer, ohne dass Platzhalter
        noetig waeren. Gesucht wird in ``name_1`` (Firma bzw. Nachname) und
        ``name_2`` (Vorname); die Treffer werden ueber die Kennung vereinigt, damit
        eine Person nicht doppelt erscheint.
        """
        if not (name or "").strip():
            raise ValueError("search_contact_by_name braucht einen Namen")
        treffer: dict[int, dict] = {}
        for feld in ("name_1", "name_2"):
            data = await self._post_v2(
                "/contact/search", [{"field": feld, "value": name, "criteria": "like"}]
            )
            for k in data if isinstance(data, list) else []:
                if k.get("id") is not None:
                    treffer[int(k["id"])] = k
        return list(treffer.values())

    async def search_contact_by_email(self, email: str) -> list[dict]:
        """Kontakte per POST /contact/search nach E-Mail suchen."""
        search_body = [
            {"field": "mail", "value": email, "criteria": "like"}
        ]
        data = await self._post_v2("/contact/search", search_body)
        return data if isinstance(data, list) else []

    # ── Aufträge (kb_order) ──────────────────────────────────

    async def list_orders(
        self,
        contact_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Auftraege laden; bei ``contact_id`` ueber die Suche.

        Derselbe Grund wie bei ``list_invoices``: der GET-Parameter ``contact_id``
        wird von Bexio entgegengenommen und ignoriert. Wer ihm traut, bekommt alle
        Auftraege und haelt sie fuer die eines Kunden.
        """
        if contact_id:
            data = await self._post_v2(
                "/kb_order/search",
                [{"field": "contact_id", "value": str(contact_id), "criteria": "="}],
            )
            return data if isinstance(data, list) else []
        data = await self._get_v2(
            "/kb_order", {"limit": str(limit), "offset": str(offset)}
        )
        return data if isinstance(data, list) else []

    async def get_order(self, order_id: int) -> dict:
        data = await self._get_v2(f"/kb_order/{order_id}")
        return data if isinstance(data, dict) else {}

    async def alle_auftraege(self, seite: int = 500) -> list[dict]:
        """Alle Auftraege ueber alle Seiten laden.

        ``list_orders`` liefert bewusst nur eine Seite. Dass es heute 36 Auftraege
        sind und eine Seite genuegt, ist ein Zustand des Bestands und keine
        Eigenschaft der Schnittstelle -- ohne Schleife faellt das Abschneiden
        spaeter niemandem auf.

        Der Offset ist hier wirksam (am 21.09.2026 gemessen: ``offset=0`` ergibt
        die Kennungen 2 und 3, ``offset=2`` die Kennungen 4 und 5). Bei den
        Kreditoren ist er es nicht -- deshalb gemessen statt uebernommen.
        """
        alle: list[dict] = []
        offset = 0
        while True:
            batch = await self.list_orders(limit=seite, offset=offset)
            alle.extend(batch)
            if len(batch) < seite:
                return alle
            offset += seite

    # ── Rechnungen (kb_invoice) ──────────────────────────────

    async def list_invoices(
        self,
        contact_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Eine Seite Rechnungen laden.

        ``contact_id`` wird bewusst **nicht** als GET-Parameter gesendet: Bexio nimmt
        ihn entgegen und ignoriert ihn. Am 02.09.2026 gemessen -- 652 Rechnungen
        ungefiltert, 652 "gefiltert", waehrend ``POST /kb_invoice/search`` mit
        derselben Kennung korrekt 159 lieferte. Ein wirkungsloser Filter ist
        schlimmer als keiner: die Summe sieht richtig aus und ist es nicht.
        Die Einschraenkung laeuft deshalb ueber die Suche.
        """
        if contact_id:
            return await self.search_invoices(contact_id=contact_id)
        params: dict[str, str] = {"limit": str(limit), "offset": str(offset)}
        data = await self._get_v2("/kb_invoice", params)
        return data if isinstance(data, list) else []

    async def search_invoices(
        self,
        status: str | int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        contact_id: int | None = None,
    ) -> list[dict]:
        """Rechnungen filtern (POST /kb_invoice/search).

        ``status`` akzeptiert die Kennung (7/8/9) oder die Beschriftung
        ('entwurf', 'offen', 'bezahlt'). Frueher wurde der Name unveraendert gegen
        das numerische Feld ``kb_item_status_id`` gestellt -- das trifft nie.

        Ohne Kriterien wurde bisher stillschweigend auf ``list_invoices(limit=200)``
        ausgewichen. Bei 652 Rechnungen fehlte damit zwei Dritteln des Bestands,
        ohne dass es irgendwo auffiel. Jetzt wird sauber ueber alle Seiten geblaettert.
        """
        criteria: list[dict] = []
        if contact_id:
            criteria.append({"field": "contact_id", "value": str(contact_id), "criteria": "="})
        if status is not None and status != "":
            criteria.append({
                "field": "kb_item_status_id",
                "value": str(_status_kennung(status)),
                "criteria": "=",
            })
        if from_date:
            criteria.append({"field": "is_valid_from", "value": from_date, "criteria": ">="})
        if to_date:
            criteria.append({"field": "is_valid_from", "value": to_date, "criteria": "<="})
        if not criteria:
            return await self.alle_rechnungen()
        data = await self._post_v2("/kb_invoice/search", criteria)
        return data if isinstance(data, list) else []

    async def alle_rechnungen(self, seite: int = 500) -> list[dict]:
        """Alle Rechnungen ueber alle Seiten laden.

        ``list_invoices`` liefert bewusst nur eine Seite. Fuer jede Auswertung ist
        das zu wenig, und der Fehler ist unsichtbar -- das Ergebnis wirkt vollstaendig.
        """
        alle: list[dict] = []
        offset = 0
        while True:
            batch = await self.list_invoices(limit=seite, offset=offset)
            alle.extend(batch)
            if len(batch) < seite:
                return alle
            offset += seite

    async def get_invoice(self, invoice_id: int) -> dict:
        data = await self._get_v2(f"/kb_invoice/{invoice_id}")
        return data if isinstance(data, dict) else {}

    async def get_invoice_positions(self, invoice_id: int) -> list[dict]:
        """Die Positionen einer Rechnung -- Produktzeilen und Textzeilen.

        Bexio liefert die Positionen **nicht** mit der Rechnung, sondern je Art
        unter einem eigenen Pfad. Gelesen werden dieselben beiden wie in
        ``admin_core.get_invoice_with_positions``:

        ``kb_position_custom``
            Die Produktzeilen. Hier stehen ``amount`` (Menge), ``unit_price``,
            ``unit_id``, ``tax_id`` und ``text`` -- also alles, was eine
            Stundenzahl traegt.
        ``kb_position_text``
            Reine Textzeilen ohne Betrag. Hier steht der Uebertragssatz
            («Per ... sind ...h verrechnet aber noch nicht geleistet»).

        Bexio kennt weitere Arten (``kb_position_article``,
        ``kb_position_subtotal``, ``kb_position_discount``,
        ``kb_position_pagebreak``). Sie werden hier **nicht** gelesen, weil der
        Altbestand sie nicht verwendet. Das ist eine bewusste Einschraenkung und
        keine Vollstaendigkeitsaussage: taucht eine solche Zeile auf, fehlt sie
        stillschweigend. Wer die Positionen zu Summen verrechnet, muss das wissen.

        Jede Zeile bekommt ``positionsart`` ('custom' oder 'text'), weil die
        Rohantworten sonst nicht auseinanderzuhalten sind -- beide tragen ``id``
        und ``text``, und eine Textzeile ohne Menge saehe aus wie eine
        Produktzeile mit Menge null.

        Anders als die Vorlage wird ein Fehler **nicht** verschluckt. Dort stand
        ``except requests.exceptions.RequestException: return None``, und ein
        abgelaufener Token ergab damit eine Rechnung ohne Positionen -- also eine
        Rechnung ohne Stunden, die jede Pruefung als leer durchwinkt.
        """
        positionen: list[dict] = []
        for art in ("custom", "text"):
            data = await self._get_v2(f"/kb_invoice/{invoice_id}/kb_position_{art}")
            for zeile in data if isinstance(data, list) else []:
                positionen.append({**zeile, "positionsart": art})

        # Nach der Reihenfolge auf der Rechnung sortieren. Ohne das stuenden erst
        # alle Produktzeilen und dann alle Textzeilen -- der Uebertragssatz
        # verloere den Bezug zu der Position, auf die er sich bezieht.
        def _rang(zeile: dict) -> tuple[int, int]:
            wert = zeile.get("pos")
            try:
                return (0, int(wert))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return (1, 0)

        positionen.sort(key=_rang)
        return positionen

    async def update_invoice_position(
        self, invoice_id: int, position_id: int, positionsart: str, daten: dict
    ) -> dict:
        """Eine Position einer Rechnung aendern (PUT).

        ``positionsart`` ist 'custom' oder 'text' und bestimmt den Pfad -- es
        sind zwei verschiedene Ressourcen, und eine Textzeile unter dem
        custom-Pfad ergibt eine 404 auf eine Position, die es gibt.

        **Bexio verlangt beim PUT die Pflichtfelder mit.** Wer nur ``amount``
        schickt, bekommt eine Position ohne Preis zurueck. Welche Felder das
        sind, entscheidet der Aufrufer -- hier wird nichts ergaenzt und nichts
        mit Vorgabewerten aufgefuellt, denn ein erfundener ``unit_price`` steht
        danach auf einer Rechnung beim Kunden.

        Der Aufruf ist auf **Entwuerfe** gemuenzt. Bexio laesst das Aendern einer
        ausgestellten Rechnung teils zu; ob eine Rechnung noch Entwurf ist,
        gehoert vor den Aufruf und nicht hier hinein.
        """
        if positionsart not in ("custom", "text"):
            raise ValueError(f"Unbekannte Positionsart: {positionsart!r}")
        data = await self._request(
            "PUT",
            f"{BASE_URL_V2}/kb_invoice/{invoice_id}/kb_position_{positionsart}/{position_id}",
            json_body=daten,
        )
        return data if isinstance(data, dict) else {}

    async def delete_invoice_position(
        self, invoice_id: int, position_id: int, positionsart: str
    ) -> bool:
        """Eine Position einer Rechnung entfernen (DELETE).

        Anders als in der Vorlage (``admin_core.delete_invoice_position``) wird
        ein Fehler nicht zu ``False`` verschluckt: dort sah ein abgelaufener
        Token aus wie eine Position, die sich nicht loeschen laesst, und der
        Lauf machte weiter.
        """
        if positionsart not in ("custom", "text"):
            raise ValueError(f"Unbekannte Positionsart: {positionsart!r}")
        await self._request(
            "DELETE",
            f"{BASE_URL_V2}/kb_invoice/{invoice_id}/kb_position_{positionsart}/{position_id}",
        )
        return True

    async def create_invoice_from_order(self, order_id: int) -> dict:
        """Aus einem Auftrag eine Rechnung erzeugen (POST /kb_order/{id}/invoice).

        Uebernommen werden **alle** Positionen des Auftrags -- derselbe Weg wie
        «Auftraege -> Rechnungen generieren» in der Oberflaeche. Die neue
        Rechnung ist ein Entwurf und geht nicht an die Kundschaft.

        **Der Aufruf geht ohne Rumpf.** Ein leeres ``{}`` als JSON quittiert
        Bexio mit ``415 Unsupported Media Type`` -- eine Fehlermeldung, die auf
        den Inhaltstyp zeigt, obwohl der stimmt. Am 21.09.2026 gemessen: kein
        Rumpf ergibt 200, ``{}`` ergibt 415. Ein Test gegen einen nachgebildeten
        Server faellt darauf nicht herein, weil der jeden Rumpf annimmt.

        Eine Teilmenge von Positionen zu verrechnen laesst die Schnittstelle zu
        (``{"positions": [...]}``), hier aber bewusst nicht: der Rechnungslauf
        verrechnet immer den ganzen Auftrag, und ein ungeprueftes Stueck
        Schreibcode auf der Buchhaltung ist schlechter als keines.

        **Das Datum setzt Bexio selbst, und zwar auf heute.** Fuer die
        Umsatzabgrenzung muss es der letzte Tag des Leistungsmonats sein -- der
        Lauf findet aber am ersten oder zweiten des Folgemonats statt. Das
        Verschieben gehoert deshalb zwingend dazu; ``update_invoice`` macht es.
        Wer nur diese Methode aufruft, erzeugt Rechnungen mit dem falschen Monat.
        """
        data = await self._request(
            "POST", f"{BASE_URL_V2}/kb_order/{order_id}/invoice"
        )
        return data if isinstance(data, dict) else {}

    async def update_invoice(self, invoice_id: int, **felder) -> dict:
        """Kopfdaten einer Rechnung aendern (POST /kb_invoice/{id}).

        Es ist ein POST und kein PATCH -- so nennt Bexio das Bearbeiten. Gesendet
        wird **nur**, was uebergeben wurde: ein mitgeschicktes Feld mit
        Vorgabewert ueberschriebe stillschweigend, was auf der Rechnung steht.

        Gebraucht wird es fuer ``is_valid_from`` und ``is_valid_to``. Beide
        gehoeren zusammen verschoben, siehe ``rechnungsdatum_setzen``.
        """
        if not felder:
            raise ValueError("update_invoice ohne Felder aufgerufen")
        data = await self._post_v2(f"/kb_invoice/{invoice_id}", felder)
        return data if isinstance(data, dict) else {}

    async def issue_invoice(self, invoice_id: int) -> bool:
        """Eine Entwurfsrechnung ausstellen (POST /kb_invoice/{id}/issue).

        Danach steht die Rechnung auf **offen** (``kb_item_status_id`` 8) und
        zaehlt als Forderung. Der Schritt gehoert ans Ende des Laufs, nach der
        Bestaetigung, dass die Mail raus ist -- vorher waere eine Forderung
        gebucht, die die Kundschaft nie gesehen hat.

        **Bexio kennt drei Uebergaenge, und zwei davon sind hier falsch:**

        ``issue``
            Entwurf -> offen, **ohne** Kommunikation. Das ist dieser hier.
        ``mark_as_sent``
            Vermerkt in Bexio eine Zustellung. Bewusst nicht umgesetzt: der
            Versand geschieht in Outlook, und ein zweiter Vermerk an anderer
            Stelle waere eine zweite Wahrheit ueber denselben Vorgang.
        ``send``
            Stellt aus **und verschickt selbst eine E-Mail an die Kundschaft**.
            Diese Methode darf es hier nie geben. Externe Kommunikation ist im
            Pflichtenheft L1, und ein Tippfehler im Pfad waere der Unterschied
            zwischen einem Statuswechsel und zwanzig ungeprueften Mails.

        Zurueck geht es ueber ``revert_issue`` -- in der Bexio-Oberflaeche und
        per Schnittstelle. Hier ist das bewusst nicht eingebaut: eine
        Ruecknahme ist ein Einzelfall und gehoert dorthin, wo man sieht, was
        man tut.

        **Eine bereits ausgestellte Rechnung ist kein Fehler, sondern nichts zu
        tun.** Bexio verlangt fuer ``issue`` den Entwurfsstatus und quittiert
        sonst mit einem Fehler. Im Sammellauf hiesse das: ein Wiederholen nach
        einem Abbruch scheitert an den Rechnungen, die schon durch sind. Darum
        wird der Status vorher gelesen; der Rueckgabewert sagt, ob dieser
        Aufruf etwas geaendert hat.
        """
        rechnung = await self.get_invoice(invoice_id)
        status = rechnung.get("kb_item_status_id")
        if status != 7:
            logger.info(
                "Rechnung %s steht auf Status %s und nicht auf Entwurf -- "
                "nichts auszustellen", invoice_id, status,
            )
            return False

        await self._request("POST", f"{BASE_URL_V2}/kb_invoice/{invoice_id}/issue")
        return True

    async def get_invoice_pdf(self, invoice_id: int) -> bytes:
        """Das Rechnungs-PDF als Bytes (GET /kb_invoice/{id}/pdf).

        **Die Antwort ist JSON, nicht das PDF.** Bexio liefert ein Objekt mit
        ``name``, ``size``, ``mime`` und ``content`` -- und ``content`` ist das
        Base64-kodierte PDF. Wer die Antwort als Rumpf speichert, legt eine
        Datei ab, die wie ein PDF heisst und keines ist; der Fehler faellt erst
        auf, wenn jemand sie oeffnet -- moeglicherweise die Kundschaft.

        Geprueft wird deshalb die **Signatur**: jedes PDF beginnt mit ``%PDF``.
        Ein leeres oder falsch kodiertes Feld waere sonst eine Datei von null
        Bytes, und null Bytes sehen auf jedem Verzeichnislisting aus wie ein
        Ergebnis.

        Das PDF traegt den Stand der Rechnung im Augenblick des Abrufs. Wer
        Positionen aendert, muss neu abrufen -- das alte PDF bleibt sonst
        gueltig aussehend und falsch.
        """
        data = await self._get_v2(f"/kb_invoice/{invoice_id}/pdf")
        if not isinstance(data, dict) or not data.get("content"):
            raise ValueError(
                f"Bexio lieferte kein PDF zu Rechnung {invoice_id}: {str(data)[:200]}"
            )
        try:
            roh = base64.b64decode(data["content"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"PDF zu Rechnung {invoice_id} ist nicht Base64: {exc}"
            ) from exc
        if not roh.startswith(b"%PDF"):
            raise ValueError(
                f"PDF zu Rechnung {invoice_id} traegt keine PDF-Signatur "
                f"({len(roh)} Bytes)"
            )
        return roh

    # ── Lieferantenrechnungen (purchase/bills, v4) ───────────

    async def list_bills(self, seite: int = 1, limit: int = 100) -> tuple[list[dict], dict]:
        """Eine Seite Lieferantenrechnungen samt Blaetter-Angabe.

        **Geblaettert wird ueber ``page``, nicht ueber ``offset``.** Bexio nimmt
        ``offset`` entgegen und ignoriert es: am 03.09.2026 lieferte ``offset=50``
        exakt dieselben 50 Zeilen wie ``offset=0``. Dieselbe Gattung Fehler wie der
        wirkungslose ``contact_id``-Filter auf ``kb_invoice`` -- eine plausible
        Antwort statt einer Fehlermeldung. Wer hier ``offset`` verwendet, laedt die
        erste Seite so oft, wie er zu blaettern glaubt.

        Der zweite Rueckgabewert ist ``paging`` mit ``item_count`` und
        ``page_count``. Damit ist Vollstaendigkeit pruefbar statt vermutet.
        """
        antwort = await self._get_v4(
            "/purchase/bills", {"page": str(seite), "limit": str(limit)}
        )
        if not isinstance(antwort, dict):
            return [], {}
        daten = antwort.get("data")
        return (daten if isinstance(daten, list) else []), (antwort.get("paging") or {})

    async def alle_lieferantenrechnungen(self, limit: int = 100) -> tuple[list[dict], int]:
        """Alle Lieferantenrechnungen ueber alle Seiten, mit gemeldeter Gesamtzahl.

        Gibt zusaetzlich ``item_count`` der ersten Seite zurueck. Der Aufrufer
        vergleicht ihn mit der Anzahl eingesammelter Zeilen -- ein unvollstaendiger
        Abzug ist sonst nicht von einem kleinen Bestand zu unterscheiden.
        """
        alle: list[dict] = []
        gemeldet = 0
        seite = 1
        while True:
            zeilen, paging = await self.list_bills(seite=seite, limit=limit)
            if seite == 1:
                gemeldet = int(paging.get("item_count") or 0)
            if not zeilen:
                return alle, gemeldet
            alle.extend(zeilen)
            seite += 1

    async def get_bill(self, bill_id: str) -> dict:
        """Einzelne Lieferantenrechnung -- die Liste allein genuegt nicht.

        Nur der Einzelabruf traegt ``supplier_id`` (die stabile Kennung statt des
        frei geschriebenen Lieferantennamens), ``line_items`` mit der Kontierung
        und ``base_currency_amount`` -- den CHF-Gegenwert einer Fremdwaehrungs-
        rechnung. In der Liste steht nur der Fremdwaehrungsbetrag; wer ihn ohne
        Umrechnung summiert, addiert Euro zu Franken.
        """
        data = await self._get_v4(f"/purchase/bills/{bill_id}")
        return data if isinstance(data, dict) else {}

    async def alle_lieferantenrechnungen_detailliert(
        self, kennungen: list[str]
    ) -> tuple[dict[str, dict], list[str]]:
        """Einzelabrufe gebuendelt, begrenzt parallel.

        Gibt die gelungenen Abrufe und die Kennungen der misslungenen zurueck.
        Fehlschlaege werden **nicht** verschluckt: eine Rechnung ohne Detail hat
        keinen Lieferanten und keine Kontierung, und das muss als Luecke sichtbar
        sein statt als leere Spalte.
        """
        zaehler = asyncio.Semaphore(DETAIL_PARALLEL)
        gelungen: dict[str, dict] = {}
        misslungen: list[str] = []

        async def einer(kennung: str) -> None:
            async with zaehler:
                try:
                    gelungen[kennung] = await self.get_bill(kennung)
                except Exception as exc:  # noqa: BLE001 -- Luecke wird gemeldet, nicht geworfen
                    logger.warning("Lieferantenrechnung %s nicht abrufbar: %s", kennung, exc)
                    misslungen.append(kennung)

        await asyncio.gather(*(einer(k) for k in kennungen))
        return gelungen, misslungen

    # ── Bankkonten ────────────────────────────────────────────

    async def list_bank_accounts(self) -> list[dict]:
        """Alle Bankkonten abrufen (v3 Banking API)."""
        data = await self._get_v3("/banking/accounts")
        return data if isinstance(data, list) else []

    async def get_bank_account(self, account_id: int) -> dict:
        """Einzelnes Bankkonto mit Saldo."""
        data = await self._get_v3(f"/banking/accounts/{account_id}")
        return data if isinstance(data, dict) else {}

    # ── Kontenplan (Accounting, v2) ──────────────────────────

    async def list_accounts(self, limit: int = 500) -> list[dict]:
        """Kontenplan (Chart of Accounts) laden."""
        data = await self._get_v2("/accounts", {"limit": str(limit)})
        return data if isinstance(data, list) else []

    async def search_accounts(self, criteria: list[dict]) -> list[dict]:
        """Konten suchen (POST /2.0/accounts/search)."""
        data = await self._post_v2("/accounts/search", criteria)
        return data if isinstance(data, list) else []

    # ── Journal (Accounting, v3) ──────────────────────────────

    async def list_journal(
        self,
        from_date: str,
        to_date: str,
        limit: int = 2000,
        offset: int = 0,
    ) -> list[dict]:
        """Eine einzelne Seite des Buchungsjournals -- ohne eigenes Blaettern.

        Getrennt von ``get_journal``, weil sich nur so pruefen laesst, **ob**
        ``offset`` ueberhaupt wirkt: dazu muessen zwei Seiten gezielt angefordert
        und verglichen werden. ``/4.0/purchase/bills`` nimmt ``offset`` entgegen
        und ignoriert ihn -- diese Annahme will man hier nicht ungeprueft erben.
        """
        data = await self._get_v3(
            "/accounting/journal",
            {"from": from_date, "to": to_date, "limit": str(limit), "offset": str(offset)},
        )
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict)]

    async def get_journal(
        self,
        from_date: str,
        to_date: str,
        limit: int = 2000,
        offset: int = 0,
    ) -> list[dict]:
        """Buchhaltungsjournal laden (alle Buchungen im Zeitraum).

        Jede Buchung enthaelt: debit_account_id, credit_account_id,
        amount, base_currency_amount, date, ref_class, description.

        Geblaettert wird ueber ``offset``, und der Abbruch haengt **nicht** allein
        an der Seitengroesse: die Schleife endet auch, sobald eine Seite keine neue
        Buchungskennung mehr bringt. Ohne diesen Waechter liefe sie endlos, falls
        Bexio ``offset`` ignoriert -- genau das tut die Kreditorenschnittstelle.
        Ein Stillstand waere die teuerste Art, diese Falle zu erben.
        """
        alle: list[dict] = []
        gesehen: set = set()
        aktuell = offset
        while True:
            seite = await self.list_journal(from_date, to_date, limit, aktuell)
            neu = [e for e in seite if e.get("id") not in gesehen]
            if not neu:
                return alle
            gesehen.update(e.get("id") for e in neu)
            alle.extend(neu)
            if len(seite) < limit:
                return alle
            aktuell += limit

    async def get_business_years(self) -> list[dict]:
        """Geschaeftsjahre laden (Start/Ende/Status)."""
        data = await self._get_v3("/accounting/business_years")
        return data if isinstance(data, list) else []

    # ── Projekte ─────────────────────────────────────────────

    async def list_projects(self, limit: int = 50) -> list[dict]:
        params = {"limit": str(limit)}
        data = await self._get_v2("/pr_project", params)
        return data if isinstance(data, list) else []

    async def get_project(self, project_id: int) -> dict:
        data = await self._get_v2(f"/pr_project/{project_id}")
        return data if isinstance(data, dict) else {}
