"""Durchreiche zur Signa-2.0-API.

Signa ist ein eigenstaendiger Dienst mit eigener Datenhaltung und eigener Strecke. Er
kennt keine Benutzer -- Mandantentrennung geschieht dort ueber getrennte Instanzen.
Damit die Signale-Seite trotzdem nicht offen steht, laeuft der Zugriff durch dieses
Backend: Hier greift die Anmeldung von TaskPilot, und der Signa-Dienst bleibt nach
aussen zu.

Bewusst eine schlanke Durchreiche und keine Nachbildung der Endpunkte. Jede hier
nachgebaute Fachlichkeit muesste bei jeder Aenderung an Signa nachgezogen werden --
und faellt einem Kunden mit eigener Instanz ohnehin nicht zu.

Der Router ist von `signa.py` (Altsystem, liest die ISI-Datenbank) unabhaengig. Beide
laufen nebeneinander, bis die neue Strecke nachweislich traegt.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.deps import require_role
from app.config import get_settings
from app.models import User

router = APIRouter(prefix="/api/signa2", tags=["signa2"])
logger = logging.getLogger("taskpilot.signa2")

# Nur diese Pfade sind erreichbar. Ohne Liste waere jeder kuenftige Endpunkt von Signa
# automatisch offen -- auch die Verwaltung der Zugangsdaten.
#
# `v1/sources` schliesst die Unterpfade ein (`/discover`, `/probe`, `/import-opml`) und ist
# schreibend: Die Quellenpflege gehoert zum Produkt, nicht zu einer Nebenoberflaeche.
# Schreibrechte hat nur, wer in TaskPilot die Rolle `owner` traegt -- siehe unten.
#
# `v1/takt` und `v1/jetzt-holen` steuern, wie oft Signa von selbst holt. Aus demselben
# Grund wie die Quellen: Wer liest, muss einstellen koennen, wie oft nachgeschaut wird.
#
# `v1/interpretations` ist das taegliche Signalbriefing -- lesend fuer die Cockpit-Kachel,
# schreibend fuer den Knopf «jetzt schreiben». Das Schreiben kostet Rappen und dauert eine
# Minute; es steht in `LANGSAM`, weil es sonst am Proxy abbricht, waehrend Signa noch
# arbeitet. Der Pfad schliesst die Unterpfade der Folge ein: Ton abholen, sprechen lassen,
# Hoerstand melden.
#
# Unter `v1/settings` stehen zwei Dinge nebeneinander: die Podcast-Einstellungen, die zum
# Produkt gehoeren, und die Zugangsdaten, die es nicht tun. Deshalb sind hier die beiden
# Unterpfade einzeln aufgefuehrt und nicht `v1/settings` als Ganzes -- sonst reichte die
# Signale-Seite bis an die API-Schluessel.
ERLAUBT = (
    "v1/reading-list",
    "v1/radars",
    "v1/signals",
    "v1/feedback",
    "v1/state",
    "v1/sources",
    "v1/takt",
    "v1/jetzt-holen",
    "v1/profile",
    "v1/interpretations",
    "v1/settings/podcast",
    "v1/settings/voices",
)

# Der Vorschlagslauf befragt ein Sprachmodell und prueft anschliessend jede genannte
# Adresse einzeln. Das dauert laenger als jeder andere Aufruf; mit dreissig Sekunden
# braeche der Proxy ab, waehrend Signa noch arbeitet.
ZEITGRENZE = httpx.Timeout(30.0, connect=5.0)
ZEITGRENZE_LANG = httpx.Timeout(180.0, connect=5.0)
LANGSAM = (
    "v1/sources/suggest",
    "v1/sources/discover",
    "v1/sources/probe",
    "v1/interpretations",
)

# Die Vertonung braucht laenger als alles andere: erst schreibt ein Modell das Drehbuch,
# dann spricht ein zweites vierhundert Woerter. Gemessen waren gut zwei Minuten fuer einen
# Monolog; ein Gespraech ueber sechs Themen liegt darueber. Mit den drei Minuten von
# `ZEITGRENZE_LANG` braeche der Proxy mitten im Sprechen ab -- und der Ton entstuende
# trotzdem, nur saehe der Anwender einen Fehler.
ZEITGRENZE_VERTONUNG = httpx.Timeout(600.0, connect=5.0)


def _ist_erlaubt(pfad: str) -> bool:
    return any(pfad == e or pfad.startswith(f"{e}/") for e in ERLAUBT)


def _zeitgrenze(pfad: str) -> httpx.Timeout:
    """Wie lange auf Signa gewartet wird.

    Der Vergleich ist genau und nicht nach Praefix: `v1/interpretations` ist langsam, weil
    dort geschrieben wird -- `v1/interpretations/{id}/podcast.wav` liefert nur eine Datei
    aus und braucht keine drei Minuten.
    """
    if pfad.endswith("/podcast"):
        return ZEITGRENZE_VERTONUNG
    return ZEITGRENZE_LANG if pfad in LANGSAM else ZEITGRENZE


@router.api_route("/{pfad:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def durchreichen(
    pfad: str,
    request: Request,
    user: User = Depends(require_role("owner")),
) -> Response:
    """Eine Anfrage an Signa weiterreichen und die Antwort unveraendert zurueckgeben."""
    if not _ist_erlaubt(pfad):
        raise HTTPException(status_code=404, detail="Unbekannter Signa-Endpunkt")

    einstellungen = get_settings()
    ziel = f"{einstellungen.signa_base_url.rstrip('/')}/api/{pfad}"

    try:
        async with httpx.AsyncClient(timeout=_zeitgrenze(pfad)) as klient:
            antwort = await klient.request(
                request.method,
                ziel,
                params=request.query_params,
                content=await request.body(),
                headers={"Content-Type": request.headers.get("content-type", "application/json")},
            )
    except httpx.ConnectError:
        # Ein nicht laufender Dienst ist eine andere Lage als ein Fehler in ihm. Die
        # Oberflaeche zeigt diese Meldung an, deshalb muss sie sagen, was zu tun ist.
        logger.warning("Signa nicht erreichbar unter %s", einstellungen.signa_base_url)
        raise HTTPException(
            status_code=503,
            detail=f"Signa ist nicht erreichbar ({einstellungen.signa_base_url}). Laeuft der Dienst?",
        ) from None
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Signa antwortet nicht rechtzeitig.") from None

    return Response(
        content=antwort.content,
        status_code=antwort.status_code,
        media_type=antwort.headers.get("content-type"),
    )
