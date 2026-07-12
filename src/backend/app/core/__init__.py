"""app.core — verbleibende Core-Naht innerhalb von TaskPilot.

Der wiederverwendbare, app-neutrale Kern ist als eigenständiges Paket **``ai9``**
ausgegliedert (Schwester-Repo ``../AI9``, via Editable-/Wheel-Dependency
eingebunden). TaskPilot importiert diese Bausteine direkt aus ``ai9`` (z. B.
``from ai9.embeddings import embed_text``); die Verdrahtung des Core-Settings-
Providers geschieht im Composition Root ``app/__init__.py``.

Hier verbleibt bewusst nur, was **noch nicht** extrahiert werden kann:

- ``principal`` — die Auflösung des handelnden Prinzipals. Sie hängt am TaskPilot-
  ORM (``app.models.User``) und ist zugleich die Kernfrage des Mehrbenutzer-Umbaus
  (Phase D). Sie wandert erst mit dem Identity-Modell in den Core.

Leitinvariante bleibt: App → Core (``ai9``), nie zurück.
"""
