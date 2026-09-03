"""Prüflauf: derselbe Codepfad wie die Oberfläche, ohne Browser und ohne Anmeldung.

Läuft im Backend-Container und ruft ``build_chat_agent`` direkt auf -- also genau
das, was ``send_agent_message`` nach der Rechteprüfung tut. Damit ist messbar, ob
eine Frage beim ersten Versuch trifft, über Umwege trifft oder scheitert, ohne dass
jemand fünf Mal von Hand tippt und die Läufe sich gegenseitig um die GPU streiten.

**Eine Anfrage nach der anderen.** Parallele Läufe teilen sich ein lokales Modell
und verfälschen sowohl die Dauer als auch das Ergebnis.

Aufruf im Container:

    python /app/scripts/agenten_pruefung.py            # alle Szenarien
    python /app/scripts/agenten_pruefung.py 1 3        # nur die genannten
    python /app/scripts/agenten_pruefung.py 1 --modell anthropic/claude-sonnet-5

Bei einem Cloud-Modell ist ein anderes Ergebnis zu erwarten und kein Fehler: der
Cloud-Pfad laeuft unter Default-Deny -- kein Gedaechtnis, keine Kontextdateien, und
nur ausdruecklich freigegebene MCP-Server (siehe ``build_chat_agent``).
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

# Mit Praefix, sonst gilt das Modell laut ``_is_local_model`` als Cloud-Modell und
# wird ueber den LiteLLM-Proxy geleitet. Das kostete den ersten Probelauf, als der
# Proxy nicht lief: «Connection error» statt einer Antwort.
MODELL = "ollama/qwen3.6:latest"
DENKMODUS = "kurz"
SERVER = ["datenraum", "sandbox"]
ZEITLIMIT = 600

# Bewusst verschiedene Bauarten, nicht fünf Abwandlungen derselben Frage.
SZENARIEN = [
    {
        "nr": 1,
        "titel": "Kürzel über den Schlüssel (die Frage, an der es scheiterte)",
        "frage": "Wie viel Umsatz habe ich mit AGG gemacht?",
        "erwartet": "227'789 CHF auf 49 Rechnungen, nicht 0 und nicht 600'000",
    },
    {
        "nr": 2,
        "titel": "Zwei Systeme verbinden (Stunden gegen Umsatz)",
        "frage": "Wie viele Stunden habe ich für MBA erfasst und wie viel davon fakturiert?",
        "erwartet": "2112.5 Stunden, 676'880 CHF fakturiert",
    },
    {
        "nr": 3,
        "titel": "Stille Null (offen_betrag ist überall 0)",
        "frage": "Wie viel schulde ich meinen Lieferanten aktuell?",
        "erwartet": "rund 4'496 CHF auf drei Rechnungen, nicht 0",
    },
    {
        "nr": 4,
        "titel": "Entwurfs-Falle (offen enthält einen Entwurf)",
        "frage": "Wer schuldet mir Geld und wie viel ist insgesamt offen?",
        "erwartet": "45'593 CHF auf 11 Rechnungen, nicht 53'593",
    },
    {
        "nr": 5,
        "titel": "Rückfall-Probe Ausgaben (Journal statt Kreditoren, CHF-Spalte)",
        "frage": "Was hat uns Cursor 2026 bisher gekostet?",
        "erwartet": "12'924 CHF, nicht 16'164 (Währungsfalle) und nicht 0",
    },
    {
        "nr": 7,
        "titel": "Offene Frage: der Agent fragt, statt zu raten",
        "frage": "Wie viel Umsatz habe ich mit dem Kanton Bern gemacht?",
        "erwartet": "Rückfrage, welches Amt gemeint ist -- keine erfundene Zahl",
    },
    {
        "nr": 6,
        "titel": "Ehrlichkeit statt Rateschluss (Kundschaft ohne Schlüssel)",
        "frage": "Wie viel Umsatz habe ich mit CURAVIVA gemacht?",
        "erwartet": "30'388 CHF -- oder eine ehrliche Auskunft, dass kein Schlüssel besteht",
    },
]


async def _lauf(szenario: dict, modell: str = MODELL) -> dict:
    from app.routers.chat import _build_agent_prompt
    from app.services.hermes_worker import build_chat_agent, ensure_runtime_ready

    if not await ensure_runtime_ready():
        return {"fehler": "Hermes-Runtime nicht verfügbar"}

    werkzeuge: list[str] = []
    code: list[str] = []

    def on_tool_start(*args, **kwargs):
        # Bewusst formfrei: Hermes ruft den Rueckruf je nach Werkzeug mit
        # unterschiedlicher Signatur auf. Eine feste Signatur schluckte im ersten
        # Anlauf jeden Aufruf still -- die Spalte «Werkzeuge» blieb leer, obwohl
        # der Agent nachweislich welche benutzt hatte.
        name = str(kwargs.get("name") or (args[0] if args else "?"))
        werkzeuge.append(name)
        rest = kwargs.get("args") or (args[1] if len(args) > 1 else None)
        if rest is not None:
            text = rest if isinstance(rest, str) else json.dumps(rest, ensure_ascii=False, default=str)
            code.append(f"{name}: {text[:1200]}")

    prompt = await _build_agent_prompt(szenario["frage"])
    agent = await asyncio.to_thread(
        build_chat_agent,
        modell,
        enabled_servers=SERVER,
        include_memory=False,
        on_tool_start=on_tool_start,
        session_id=f"pruefung-{uuid.uuid4()}",
    )

    from app.services import denkstufen

    overrides = denkstufen.request_overrides(DENKMODUS, modell)
    if overrides:
        agent.request_overrides = {**(getattr(agent, "request_overrides", None) or {}), **overrides}

    beginn = time.time()
    try:
        antwort = await asyncio.wait_for(
            asyncio.to_thread(agent.run_conversation, prompt), timeout=ZEITLIMIT
        )
    except asyncio.TimeoutError:
        try:
            agent.interrupt("Zeitlimit der Prüfung.")
        except Exception:  # noqa: BLE001
            pass
        antwort = f"<<ZEITLIMIT nach {ZEITLIMIT}s>>"
    except Exception as exc:  # noqa: BLE001
        antwort = f"<<FEHLER {type(exc).__name__}: {exc}>>"

    return {
        "dauer": round(time.time() - beginn, 1),
        "werkzeuge": werkzeuge,
        "code": code,
        "antwort": str(antwort),
    }


async def main() -> None:
    argumente = sys.argv[1:]
    modell = MODELL
    if "--modell" in argumente:
        stelle = argumente.index("--modell")
        modell = argumente[stelle + 1]
        argumente = argumente[:stelle] + argumente[stelle + 2:]
    gewuenscht = {int(a) for a in argumente if a.isdigit()}

    ziel = Path("/tmp/agenten_pruefung.jsonl")
    ziel.unlink(missing_ok=True)
    print(f"Modell: {modell} | Denkmodus: {DENKMODUS}", flush=True)

    for szenario in SZENARIEN:
        if gewuenscht and szenario["nr"] not in gewuenscht:
            continue
        print(f"\n{'=' * 78}\n[{szenario['nr']}] {szenario['titel']}", flush=True)
        print(f"    Frage:    {szenario['frage']}", flush=True)
        print(f"    Erwartet: {szenario['erwartet']}", flush=True)

        ergebnis = await _lauf(szenario, modell)
        ergebnis.update({"nr": szenario["nr"], "titel": szenario["titel"], "modell": modell,
                         "frage": szenario["frage"], "erwartet": szenario["erwartet"]})
        with ziel.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ergebnis, ensure_ascii=False) + "\n")

        print(f"    Dauer:    {ergebnis.get('dauer')}s", flush=True)
        print(f"    Werkzeuge: {' -> '.join(ergebnis.get('werkzeuge') or []) or 'keine'}", flush=True)
        print(f"    Antwort:  {ergebnis.get('antwort', '')[:900]}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
