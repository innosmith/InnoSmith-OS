"""Live-Test des Sammel-Laufs am bekannten Fehlerfall vom 03.08.2026.

Der Fall: Ein Entwurf nannte «14h Sympholio-Budget für Juli verfügbar» -- am
3. August, und für August war gar keine Kapazität geplant. Die Zahl stammte aus
einer Mail vom 02.07.2026. Zwei Ursachen: das Trefferobjekt trug kein Datum, und
der Agent hatte kein Fachsystem, das ihm den Augustwert nennen konnte.

Dieses Skript stellt genau diese Frage an das echte Modell, mit den echten
Werkzeug-Implementierungen gegen die **Dev-Datenbank** (Produktion bleibt
unberührt). Erwartet wird: der Agent ruft ``get_capacity_overview`` auf und
schreibt die 14h -- wenn überhaupt -- nur mit Juli-Bezug ins Dossier.

Voraussetzungen (Testdaten):

    docker exec -i taskpilot-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
        < scripts/seed_capacity_dev.sql
    docker exec -i taskpilot-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
        < scripts/seed_semantic_dev.sql

Aufruf:  python scripts/live_gather_capacity.py [--wide]
"""

import asyncio
import importlib.util
import json
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src", "backend"))
sys.path.insert(0, os.path.join(_ROOT, "src", "capacity"))
sys.path.insert(0, os.path.join(_ROOT, "src", "toggl"))

from app.services.draft_prompt import render_gather_task  # noqa: E402


def _load(alias: str, subdir: str):
    """Beide MCP-Server heissen ``server.py`` -- daher unter eigenem Namen laden."""
    path = os.path.join(_ROOT, "src", subdir, "server.py")
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


tp_server = _load("tp_server", "mcp-taskpilot")
cap_server = _load("cap_server", "mcp-capacity")

LITELLM = "http://localhost:4000/v1/chat/completions"
MODEL = "ollama/qwen3.6:latest"
TODAY = "Montag, 03.08.2026"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "semantic_search_documents",
            "description": (
                "Durchsucht den lokalen semantischen Index über Anthonys E-Mails und "
                "OneDrive-Dokumente (Bedeutung + Stichwort, hybrid)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_capacity_overview",
            "description": next(
                t.description for t in cap_server.TOOLS if t.name == "get_capacity_overview"
            ),
            "parameters": next(
                t.inputSchema for t in cap_server.TOOLS if t.name == "get_capacity_overview"
            ),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_absences",
            "description": next(
                t.description for t in cap_server.TOOLS if t.name == "get_absences"
            ),
            "parameters": next(
                t.inputSchema for t in cap_server.TOOLS if t.name == "get_absences"
            ),
        },
    },
]


def wide_tool_ballast() -> list[dict]:
    """Die echten Werkzeug-Definitionen der übrigen Fachsysteme.

    Für die Messung «schmal gegen breit» zählt der Prompt-Ballast, also die echten
    Beschreibungen und Schemata -- nicht die Antworten. Ausgeführt werden diese
    Werkzeuge hier nicht; sie melden sich als nicht verfügbar zurück. Gemessen wird
    damit genau die Frage, ob mehr Auswahl die Werkzeugwahl verschlechtert.
    """
    ballast: list[dict] = []
    for subdir in ("mcp-pipedrive", "mcp-bexio", "mcp-toggl", "mcp-signa"):
        try:
            module = _load(f"ballast_{subdir}", subdir)
        except Exception as exc:  # noqa: BLE001 - Messung darf daran nicht scheitern
            print(f"  (Ballast {subdir} nicht ladbar: {exc})")
            continue
        for tool in getattr(module, "TOOLS", []):
            ballast.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                },
            })
    return ballast


async def run_tool(name: str, args: dict) -> str:
    """Ruft die echte Implementierung auf; Ballast-Werkzeuge melden sich ab."""
    if name == "semantic_search_documents":
        pool = await tp_server.get_pool()
        result = await tp_server._semantic_search_documents(
            pool, {**args, "limit": args.get("limit", 5)}
        )
        return result[0].text
    if name in ("get_capacity_overview", "get_absences"):
        result = await cap_server.call_tool(name, args)
        return result[0].text
    return json.dumps(
        {"error": f"{name} ist in diesem Testlauf nicht angebunden."}, ensure_ascii=False
    )


def chat(messages: list[dict], with_tools: bool = True) -> dict:
    payload: dict = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "top_p": 0.8,
        "extra_body": {"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}},
        "reasoning_effort": "none",
    }
    if with_tools:
        payload["tools"] = TOOLS + _BALLAST
    req = urllib.request.Request(
        LITELLM,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-litellm-local"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())["choices"][0]["message"]


async def run_gather(case: dict, wide: bool, max_rounds: int = 5) -> tuple[str, list[str]]:
    extra_systems = ""
    if wide:
        extra_systems = (
            "- Verkaufschancen, Deals, Angebotsstand → **search_deals**\n"
            "- Rechnungen, Offerten, Buchhaltung → **search_invoices**\n"
        )
    prompt = render_gather_task(
        today=TODAY,
        subject=case["subject"],
        from_name=case["from_name"],
        from_addr=case["from_addr"],
        body_block=case["body"],
        briefing_block=case.get("briefing", ""),
        sender_org=case.get("org", ""),
        topic_hint=case.get("topic", case["subject"]),
        max_rounds=max_rounds,
        extra_systems=extra_systems,
    )
    messages = [{"role": "user", "content": prompt}]
    calls: list[str] = []

    for round_no in range(max_rounds + 1):
        last_round = round_no == max_rounds
        msg = chat(messages, with_tools=not last_round)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return msg.get("content") or "", calls
        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
        )
        for tc in tool_calls:
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"] or "{}")
            output = await run_tool(name, args)
            calls.append(f"{name}({json.dumps(args, ensure_ascii=False)})")
            print(f"    → {name} {args}")
            print(f"      {output[:260].replace(chr(10), ' ')}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})

    return "(Rundenlimit erreicht)", calls


CASES = [
    {
        "name": "OneMBA Website-Priorisierung — der Fall mit der falschen Budgetzahl",
        "subject": "Re: Website Priorisierung",
        "from_name": "Simone Meier",
        "from_addr": "simone@onemba.example",
        "org": "onemba",
        "topic": "Website Priorisierung Budget",
        "body": (
            "Hallo Anthony\n\nQM Pilot ist kein verlässlicher Bezugspunkt mehr. Wir gehen "
            "die Zielsetzung der Website nochmals durch und priorisieren dann. Wie viel "
            "Zeit hast du diesen Monat noch für uns?\n\nLiebe Grüsse Simone"
        ),
        "briefing": (
            "\n---\n\n## BRIEFING AUS DER KLASSIFIKATION\n"
            "- Einordnung: Kunde\n"
            "- Weshalb eine Antwort nötig ist: Kundin fragt nach verfügbarer Zeit "
            "diesen Monat und nach dem weiteren Vorgehen bei der Priorisierung.\n"
        ),
    },
]


_BALLAST: list[dict] = []


async def main() -> None:
    global _BALLAST
    wide = "--wide" in sys.argv
    if wide:
        _BALLAST = wide_tool_ballast()
    print(
        f"Werkzeug-Umfang: {'breit' if wide else 'schmal'} | "
        f"{len(TOOLS) + len(_BALLAST)} Werkzeuge | Heute: {TODAY}"
    )
    runs = 1
    for arg in sys.argv[1:]:
        if arg.startswith("--runs="):
            runs = int(arg.split("=", 1)[1])

    for case in CASES:
        print(f"\n{'=' * 78}\nFALL: {case['name']}\n{'=' * 78}")
        tally = {"fachsystem": 0, "markdown": 0, "juli_bezug": 0, "wiederholt": 0, "runden": 0}
        for run in range(runs):
            dossier, calls = await run_gather(case, wide)
            if runs > 1:
                print(f"  --- Lauf {run + 1}/{runs} ---")
            print(f"  Werkzeug-Aufrufe ({len(calls)}):")
            for call in calls:
                print(f"    - {call}")
            if runs == 1:
                print(f"\n--- DOSSIER ---\n{dossier}\n")

            low = dossier.lower()
            # Markdown-Form ist keine Kosmetik: das Dossier geht als Text in den
            # Schreib-Prompt. Ein JSON-Blob dort ist schlechter lesbar fuer das Modell.
            markdown = "**" in dossier and not dossier.strip().startswith("{")
            tally["fachsystem"] += any("capacity" in c for c in calls)
            tally["markdown"] += markdown
            tally["juli_bezug"] += "juli" in low or "2026-07" in low
            tally["wiederholt"] += len(calls) != len(set(calls))
            tally["runden"] += len(calls)
            if runs > 1:
                print(f"    Markdown: {markdown} | Juli-Bezug: {'juli' in low or '2026-07' in low}")

        print(f"\n--- BILANZ über {runs} Lauf/Läufe ({'breit' if wide else 'schmal'}) ---")
        print(f"  Fachsystem aufgerufen: {tally['fachsystem']}/{runs}")
        print(f"  Dossier als Markdown:  {tally['markdown']}/{runs}")
        print(f"  Altfakt mit Juli-Bezug: {tally['juli_bezug']}/{runs}")
        print(f"  Läufe mit Doppelabfrage: {tally['wiederholt']}/{runs}")
        print(f"  Werkzeug-Aufrufe im Mittel: {tally['runden'] / runs:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
