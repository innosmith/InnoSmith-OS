"""Tests fuer die deterministische Meeting-Response-Override (triage.py).

Reine Entscheidungslogik ``is_meeting_response`` -- liest das strukturierte Graph-
Feld ``meetingMessageType``. Verhindert den haeufigsten Fehlgriff
"Terminzusage -> Aufgabe", ohne das LLM zu bemuehen. Kein DB/Graph noetig.
"""

from app.services.triage import is_meeting_response, MEETING_RESPONSE_TYPES


class TestIsMeetingResponse:
    def test_accepted_is_response(self):
        assert is_meeting_response({"meetingMessageType": "meetingAccepted"}) is True

    def test_declined_is_response(self):
        assert is_meeting_response({"meetingMessageType": "meetingDeclined"}) is True

    def test_tentative_is_response(self):
        assert is_meeting_response(
            {"meetingMessageType": "meetingTentativelyAccepted"}
        ) is True

    def test_meeting_request_is_not_response(self):
        # Echte Einladung -> normaler LLM-Pfad (ggf. Kalenderpruefung/Antwort).
        assert is_meeting_response({"meetingMessageType": "meetingRequest"}) is False

    def test_meeting_cancelled_is_not_response(self):
        # Absage kann zeitkritisch sein -> normaler Pfad, nicht auto-verschieben.
        assert is_meeting_response({"meetingMessageType": "meetingCancelled"}) is False

    def test_normal_email_is_not_response(self):
        assert is_meeting_response({"meetingMessageType": "none"}) is False
        assert is_meeting_response({"subject": "Offerte"}) is False
        assert is_meeting_response({"meetingMessageType": None}) is False
        assert is_meeting_response({}) is False

    def test_response_set_membership(self):
        assert "meetingAccepted" in MEETING_RESPONSE_TYPES
        assert "meetingRequest" not in MEETING_RESPONSE_TYPES
        assert "meetingCancelled" not in MEETING_RESPONSE_TYPES


class TestKalenderIsMovedOnlyHere:
    """``Inbox/Kalender`` wird ausschliesslich von diesem Pfad befuellt.

    Das LLM-Label ``Kalender`` loest keinen Move aus (siehe ``LABEL_FOLDERS``), damit
    Einladungen, Veranstalter-Absagen und Terminkonflikt-Meldungen von Kunden in der
    Inbox sichtbar bleiben. Verschoben werden nur echte Terminantworten -- und die
    erkennt Exchange strukturiert, nicht das Modell.
    """

    def test_llm_label_does_not_move(self):
        from app.services.triage_labels import move_target

        assert move_target("Kalender", "fyi", "other") is None

    def test_deterministic_path_still_targets_kalender(self):
        """Der Move haengt an einem woertlichen Ordnernamen, nicht an LABEL_FOLDERS.

        Sonst haette das Entfernen von ``Kalender`` aus der Label-Karte auch die
        Terminantworten stillgelegt.
        """
        import inspect
        from app.services.triage import _handle_meeting_response

        quelle = inspect.getsource(_handle_meeting_response)
        assert 'move_to_folder(message_id, "Kalender")' in quelle
        assert 'set_categories(message_id, ["Kalender"])' in quelle
