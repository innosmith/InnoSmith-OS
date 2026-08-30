"""Die zwei Abbildungen des Satzes «zu dieser Mail gibt es offene Arbeit».

Die Wahrheit steht in der Datenbank: ein Task mit ``email_message_id`` und
``is_completed = false``. Im Postfach wird sie zweifach abgebildet, und die beiden
Abbildungen tun bewusst Verschiedenes:

- **Die Fahne ist der Nachweis.** Sie klebt an der Mail und übersteht jeden
  Verschiebevorgang, auch die des Menschen. In Outlook auf jedem Gerät durchsuchbar,
  unabhängig davon, in welchem Ordner die Mail liegt.
- **Der Ordner ``Posteingang/Tasks`` ist die Ordnung.** Er hält den Posteingang leer
  und zeigt auf einen Blick, was gesichtet und noch offen ist.

Deshalb liegen beide in **einer** Funktion und werden von **einer** Funktion
aufgelöst. Franst das aus, kann «Fahne gesetzt» zwei Dinge bedeuten -- und ein
Merkmal mit zwei Bedeutungen ist kein Merkmal mehr, sondern eine Vermutung.

Die tragende Regel für alles hier:

    TaskPilot verschiebt nur vorwärts, Richtung Archiv. Rückwärts nur der Mensch.

Wer eine Mail bewusst ins Archiv legt, hat entschieden. Ein System, das das
stillschweigend zurückdreht, macht sich unberechenbar -- und ein Ordner, dem man
nicht glaubt, ist wertlos. Möglich wird dieser Verzicht erst durch die Fahne: rutscht
eine Mail aus dem Ordner, bleibt sie trotzdem als offen erkennbar, weil das Merkmal
mitreist.

Fehlt der Ordner, wird **nur** die Fahne gesetzt und gewarnt. ``get_or_create_folder``
legt bewusst keine Ordner an; ein halber Zustand ist schlimmer als ein bekannter.
"""

import logging
from typing import NamedTuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.principal import get_owner_settings
from app.models import Task
from app.services.email_identity import (
    fetch_internet_message_id,
    resolve_message_id,
    sync_message_id,
)
from app.services.graph import get_graph_client

logger = logging.getLogger("taskpilot.email_projection")

DEFAULT_TASKS_FOLDER = "Tasks"
SETTINGS_KEY = "tasks_mail_folder"


async def get_tasks_folder_name(db: AsyncSession) -> str:
    """Der Ordnername für offene Arbeit, aus den Owner-Settings.

    Konfigurierbar, weil der Ordner im Postfach des Menschen liegt und dieser ihn
    benennt -- nicht der Code. Leerer Wert heisst «Ordner nicht verwenden, nur
    Fahne», und das ist ein zulässiger Betriebsmodus.
    """
    settings = await get_owner_settings(db)
    value = settings.get(SETTINGS_KEY)
    if value is None:
        return DEFAULT_TASKS_FOLDER
    return str(value).strip()


async def mark_open_work(db: AsyncSession, task: Task) -> None:
    """Setzt beide Abbildungen: Fahne gesetzt, Mail in den Tasks-Ordner.

    Aufzurufen genau dann, wenn aus einer Mail nachweislich offene Arbeit geworden
    ist -- also bei einer Menschenentscheidung (Vorschlag bestätigt, Task aus der
    Inbox erstellt). Bewusst nicht schon bei der Task-Erstellung durch den Agenten:
    ``move_target()`` unterdrückt Moves bei ``needs_review``, damit ein Fehlgriff des
    Agenten keine echte Kundenmail aus dem Blick schafft.
    """
    await _project(db, task, flagged=True, archive=False)


async def release_open_work(db: AsyncSession, task: Task) -> None:
    """Löst beide Abbildungen auf: Fahne weg, Mail ins Archiv.

    Das ist die Antwort auf «wer entfernt die Fahne wieder?»: TaskPilot, beim
    Erledigen des Tasks. Ohne diesen Gegenpart bliebe die Fahne im Archiv liegen und
    die Outlook-Suche nach markierten Mails würde mit der Zeit wertlos.
    """
    await _project(db, task, flagged=False, archive=True)


async def _project(
    db: AsyncSession, task: Task, *, flagged: bool, archive: bool
) -> None:
    """Der eine Schreibpfad auf Fahne und Ordner.

    Reihenfolge ist wesentlich: **erst** die Fahne, **dann** der Move. Ein Move
    vergibt eine neue Graph-ID; würde zuerst verschoben, müsste die Fahne mit einem
    Handle gesetzt werden, das gerade veraltet ist. Umgekehrt reist die Fahne mit.

    Best-effort in jedem Schritt: Ein Ausfall darf den Request nie sprengen, und ein
    Teilerfolg (Fahne ja, Move nein) ist der bekannte Rückfall auf den heutigen
    Zustand -- die Mail bleibt liegen, wo sie ist, und bleibt als offen erkennbar.
    """
    if not task.email_message_id and not task.internet_message_id:
        return
    client = get_graph_client()
    if client is None:
        return
    try:
        if not task.internet_message_id:
            task.internet_message_id = await fetch_internet_message_id(
                client, task.email_message_id
            )

        message_id = await _usable_handle(client, task)
        if not message_id:
            logger.info(
                "Projektion übersprungen: Mail nicht auffindbar (task=%s)", task.id
            )
            return

        try:
            await client.set_flag(message_id, flagged)
        except Exception:  # noqa: BLE001 - best-effort
            logger.warning(
                "Fahne konnte nicht %s werden (task=%s)",
                "gesetzt" if flagged else "entfernt", task.id,
            )

        moved = await _move(db, client, task, message_id, archive=archive)
        if moved:
            task.email_message_id = moved
            await sync_message_id(
                db,
                internet_message_id=task.internet_message_id,
                new_message_id=moved,
            )
    except Exception:  # noqa: BLE001 - darf den Request nie sprengen
        # Mit Stacktrace, nicht als blosser Satz: Dieser Block fängt alles, auch
        # Programmierfehler. Ein Text ohne Ausnahme sagt "irgendwas ging schief" und
        # ist damit von "Graph nicht erreichbar" nicht zu unterscheiden.
        logger.exception("Projektion fehlgeschlagen (task=%s)", task.id)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


RECONCILE_PAGE_SIZE = 100


async def reconcile_tasks_folder(db: AsyncSession) -> int:
    """Gleicht den Tasks-Ordner gegen die Wahrheit ab -- ausschliesslich vorwärts.

    Zwei Graph-Anfragen, unabhängig von der Zahl der Abweichungen: Ordner-ID
    auflösen, Ordner listen. Die Vorgeschichte vom 18.08.2026 (2858 Anfragen in 36
    Sekunden) ist der Grund, warum hier nicht pro Abweichung nachgefragt wird.

    Vier Fälle, und nur einer führt zu einer Aktion:

    - Mail liegt im Ordner, ihr Task ist **erledigt** -> archivieren und entflaggen.
      Das ist die eigentliche Reparatur: das Auflösen beim Erledigen ist best-effort
      und kann an einem Graph-Ausfall gescheitert sein.
    - Mail liegt im Ordner, ihr Task ist offen -> richtig so, nichts tun.
    - Mail liegt im Ordner, es gibt **keinen** Task dazu -> liegen lassen und
      protokollieren. Hier weicht die Umsetzung bewusst vom ersten Entwurf ab, der
      solche Mails archiviert hätte: Wer eine Mail selbst in den Ordner zieht, hat
      eine Absicht, und ein System, das sie eine Viertelstunde später wegräumt, ist
      genau die Unberechenbarkeit, die der Ordner vermeiden soll. Phase 4 greift
      diesen Fall über die Fahne auf.
    - Task ist offen, seine Mail liegt **nicht** im Ordner -> nur protokollieren,
      nie zurückholen. Die Fahne reist mit der Mail, es geht nichts verloren.
    """
    folder = await get_tasks_folder_name(db)
    if not folder:
        return 0
    client = get_graph_client()
    if client is None:
        return 0
    try:
        try:
            folder_info = await client.get_or_create_folder(folder)
        except ValueError:
            logger.info("Abgleich übersprungen: Ordner '%s' existiert nicht", folder)
            return 0
        data = await client.list_emails(
            folder=folder_info["id"], top=RECONCILE_PAGE_SIZE
        )
        mails = data.get("value", [])

        index = await _task_index(db)

        repaired = 0
        for mail in mails:
            identity = mail.get("internetMessageId")
            task = index.by_identity.get(identity) if identity else None
            if task is None:
                task = index.by_handle.get(mail.get("id"))
            if task is None:
                logger.info(
                    "Abgleich: Mail im Ordner '%s' ohne Task -- bleibt liegen (%s)",
                    folder, str(mail.get("subject") or "")[:60],
                )
                continue
            if not task.is_completed:
                continue
            await _project(db, task, flagged=False, archive=True)
            repaired += 1

        in_folder = {m.get("internetMessageId") for m in mails}
        verirrt = [i for i in index.open_identities if i not in in_folder]
        if verirrt:
            logger.info(
                "Abgleich: %d offene Task-Mail(s) liegen nicht im Ordner '%s' -- "
                "unangetastet, die Fahne trägt die Information",
                len(verirrt), folder,
            )
        return repaired
    except Exception:  # noqa: BLE001 - darf den Poll-Loop nie stoppen
        # Vorfall 30.08.2026: Hier stand ``logger.warning`` ohne Stacktrace. Dahinter
        # verbarg sich ein fehlender ``select``-Import, also ein ``NameError`` bei
        # jedem Lauf -- rund 90 Mal am Tag, wochenlang, und aus dem Logtext nicht von
        # einem Graph-Ausfall zu unterscheiden. Der Abgleich hat in Produktion nie
        # eine einzige Mail archiviert.
        logger.exception("Abgleich des Tasks-Ordners fehlgeschlagen")
        return 0
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


class _TaskIndex(NamedTuple):
    """Zwei Nachschlagewege auf dieselben Tasks, plus die offenen Identitäten.

    ``by_identity`` ist der richtige Weg: die RFC-5322-Identität überlebt jeden Move.
    ``by_handle`` ist die Rückfallebene für Altbestand ohne Identität -- ein Handle
    ist unzuverlässig, aber ein unzuverlässiger Treffer ist mehr als keiner.
    """

    by_identity: dict[str, Task]
    by_handle: dict[str, Task]
    open_identities: list[str]


async def _task_index(db: AsyncSession) -> _TaskIndex:
    """Alle Tasks mit Mail-Bezug, auf zwei Wege nachschlagbar.

    Erledigte Tasks gehören dazu, denn genau sie sind der Reparaturfall.

    Zu einer Mail können mehrere Tasks gehören (aufgeteilte Arbeit). Dann gewinnt der
    **offene** -- solange noch etwas aussteht, darf die Mail nicht ins Archiv wandern.
    Ohne diese Regel würde die Reihenfolge der Datenbankzeilen entscheiden, und das
    ist keine Regel, sondern ein Zufall.

    Der Handle-Index kam am 30.08.2026 dazu: 87 von 100 Mail-Aufgaben trugen keine
    Identität, weil ``backfill_identities`` sie bewusst nur für **offene** Aufgaben
    nachholt -- der Reparaturfall hier sind aber die **erledigten**. Über die
    Identität allein hätte der Abgleich den Altbestand nie gesehen.
    """
    rows = (
        await db.execute(
            select(Task).where(
                or_(
                    Task.internet_message_id.is_not(None),
                    Task.email_message_id.is_not(None),
                )
            )
        )
    ).scalars().all()

    def _eintragen(index: dict[str, Task], schluessel: str | None, task: Task) -> None:
        if not schluessel:
            return
        vorhanden = index.get(schluessel)
        if vorhanden is None or (vorhanden.is_completed and not task.is_completed):
            index[schluessel] = task

    by_identity: dict[str, Task] = {}
    by_handle: dict[str, Task] = {}
    for task in rows:
        _eintragen(by_identity, task.internet_message_id, task)
        _eintragen(by_handle, task.email_message_id, task)
    open_identities = [
        identity for identity, t in by_identity.items() if not t.is_completed
    ]
    return _TaskIndex(by_identity, by_handle, open_identities)


async def _usable_handle(client, task: Task) -> str | None:
    """Ein Handle, mit dem Graph-Aufrufe tatsächlich durchgehen.

    Bevorzugt das gespeicherte; schlägt es fehl, wird über die Identität neu
    aufgelöst. Beides ohne Wertung: ein veraltetes Handle ist der Regelfall, sobald
    Mails wandern, und keine Ausnahme.
    """
    if task.email_message_id:
        try:
            await client.get_email(task.email_message_id)
            return task.email_message_id
        except Exception:  # noqa: BLE001 - Handle veraltet oder Mail gelöscht
            pass
    return await resolve_message_id(client, task.internet_message_id)


async def _move(
    db: AsyncSession, client, task: Task, message_id: str, *, archive: bool
) -> str | None:
    """Verschiebt die Mail ins Archiv oder in den Tasks-Ordner. Gibt das neue Handle.

    Fehlt der Tasks-Ordner, bleibt die Mail liegen und es wird gewarnt -- die Fahne
    allein trägt die Information dann weiter.
    """
    if archive:
        try:
            result = await client.archive_email(message_id)
        except Exception:  # noqa: BLE001 - best-effort
            logger.warning("Archivieren fehlgeschlagen (task=%s)", task.id)
            return None
        return (result or {}).get("id")

    folder = await get_tasks_folder_name(db)
    if not folder:
        return None
    try:
        result = await client.move_to_folder(message_id, folder)
    except ValueError:
        # get_or_create_folder legt bewusst keine Ordner an. Der Mensch benennt und
        # erstellt den Ordner in seinem eigenen Postfach.
        logger.warning(
            "Ordner '%s' fehlt unter Posteingang -- nur die Fahne wurde gesetzt "
            "(task=%s)", folder, task.id,
        )
        return None
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("Move nach '%s' fehlgeschlagen (task=%s)", folder, task.id)
        return None
    return (result or {}).get("id")
