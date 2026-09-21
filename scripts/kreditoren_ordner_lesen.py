"""Liest die Lieferantenordner aus dem Kreditorenarchiv auf OneDrive.

Einmaliges Hilfsmittel fuer den Stammdaten-Vorschlag: es sammelt die Ordnernamen
unter ``Finanzen/Kreditoren`` und dazu, in welchen Jahren je Lieferant Belege
liegen. Daraus entsteht der Ordner-Alias in ``docs/kreditorenlieferanten.yaml``.

Es blaettert **selbst**, weil ``list_drive_items`` dem ``@odata.nextLink`` nicht
folgt und sonst still bei 20 Eintraegen endet -- bei rund 150 Lieferanten waere
das ein Datenverlust, den niemand bemerkt. Die Behebung im Graph-Client steht
als eigene Aufgabe an; bis dahin blaettert dieses Skript von Hand.

Nur lesend.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent / "src" / "backend"))
sys.path.insert(0, str(HIER.parent / "src" / "email-graph"))

WURZEL = "Finanzen/Kreditoren"
FELDER = "id,name,file,folder,lastModifiedDateTime"


async def blaettern(client, pfad: str) -> list[dict]:
    """Holt alle Eintraege eines Ordners und folgt dabei ``@odata.nextLink``."""
    endpoint = f"{client._user_path}/drive/root:/{pfad}:/children"  # noqa: SLF001
    daten = await client._get(endpoint, {"$top": "200", "$select": FELDER})  # noqa: SLF001
    alle = list(daten.get("value", []))
    weiter = daten.get("@odata.nextLink")
    while weiter:
        daten = await client._get(weiter)  # noqa: SLF001
        alle.extend(daten.get("value", []))
        weiter = daten.get("@odata.nextLink")
    return alle


async def main() -> None:
    from app.services.graph import get_graph_client

    client = get_graph_client()
    if client is None:
        raise SystemExit("Graph ist nicht konfiguriert")

    oberste = await blaettern(client, WURZEL)
    ordner = sorted(e["name"] for e in oberste if "folder" in e)
    lose = [e["name"] for e in oberste if "folder" not in e]
    print(f"{len(ordner)} Ordner, {len(lose)} lose Dateien unter {WURZEL}")
    if lose:
        print("  lose Dateien:", ", ".join(lose[:10]))

    ergebnis: dict[str, dict] = {}
    for i, name in enumerate(ordner, 1):
        kinder = await blaettern(client, f"{WURZEL}/{name}")
        jahre = sorted(k["name"] for k in kinder if "folder" in k)
        flach = [k["name"] for k in kinder if "folder" not in k]
        letzte = max(
            (k.get("lastModifiedDateTime", "") for k in kinder), default=""
        )
        ergebnis[name] = {
            "jahre": jahre,
            "flache_dateien": len(flach),
            "zuletzt": letzte[:10],
            "sonderordner": name.startswith("_"),
        }
        print(
            f"[{i}/{len(ordner)}] {name}: {', '.join(jahre) or '(keine Jahresordner)'}"
            + (f"  + {len(flach)} flach" if flach else "")
        )

    ziel = HIER / "kreditoren_ordner.json"
    ziel.write_text(json.dumps(ergebnis, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGeschrieben: {ziel}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
