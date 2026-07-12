"""TaskPilot-Backend-Paket.

Composition Root: Hier verdrahtet die App den AI9-Core mit ihren konkreten
Abhängigkeiten. Weil Python beim Import eines Submoduls zuerst dieses Paket-
``__init__`` ausführt, ist das der garantierte, einmalige Punkt, an dem der Core
seinen Settings-Provider erhält — bevor irgendein Core-Aufruf ``get_core_settings()``
nutzt.

Richtung bleibt gewahrt: die App (hier) kennt den Core (``ai9``); der Core kennt
die App nie.
"""

from ai9.config import configure as _configure_core

from app.config import get_settings as _get_settings

_configure_core(_get_settings)
