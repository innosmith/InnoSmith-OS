"""Deeplinks auf externe Mail-Systeme -- bewusst ohne DB- und Framework-Abhängigkeiten.

Vorbild ist ``text_style.py``: Wird eine URL-Form an mehreren Orten gebraucht
(Router, Worker, Tests, Frontend über das API-Feld), gehört sie in **eine**
Funktion statt in mehrere ähnliche f-Strings.
"""

from urllib.parse import quote


def outlook_deeplink(message_id: str | None) -> str | None:
    """Baut einen Outlook-Web-Deeplink auf eine E-Mail aus ihrer Graph-Message-ID.

    Die ID enthält regelmässig ``+``, ``/`` und ``=``; ohne vollständiges Quoting
    zerfällt der Pfad. ``safe=''`` kodiert deshalb auch die Slashes.
    """
    if not message_id:
        return None
    return f"https://outlook.office.com/mail/deeplink/read/{quote(message_id, safe='')}"
