"""Tests für die reine Fusions-/Dedup-Logik der Dokumentsuche (ohne DB/LLM/Graph).

Deckt ab:
- ``_dedupe_file_hits``: OneDrive-Live-Treffer derselben Datei (mehrere IDs) → 1 Zeile,
  bestes (längstes) Snippet gewinnt.
- ``_merge_documents``: RRF-Fusion beliebig vieler Ranglisten (Live-Mail, Live-Drive,
  Index je Quelltyp). Kern-Invariante: **Präsenz durch Retrieval** -- eine Quelle in
  einer eigenen Rangliste kann nicht durch die Menge einer anderen verdrängt werden.
"""

from app.routers.search import (
    FileHit,
    _dedupe_file_hits,
    _file_candidates,
    _index_candidates,
    _merge_documents,
)


def _fh(id_, name, snippet=None, size=100, is_folder=False, mime="application/pdf", url=None):
    return FileHit(
        id=id_, name=name, size=size, is_folder=is_folder,
        snippet=snippet, mime_type=mime, web_url=url or f"https://drv/{id_}",
    )


def _idx(source_type, source_id, title, snippet="s", url="u", mime="application/pdf",
         matched_keyword=False):
    """Roh-Index-Treffer, wie ihn ``hybrid_search`` liefert (vor Normalisierung)."""
    return {
        "source_type": source_type, "source_id": source_id, "title": title,
        "url": url, "mime": mime, "snippet": snippet, "score": 0.5,
        "matched_keyword": matched_keyword,
    }


class TestDedupeFileHits:
    def test_same_name_different_ids_collapses(self):
        hits = [
            _fh("A", "RE-00604.pdf", snippet="kurz"),
            _fh("B", "RE-00604.pdf", snippet="ein deutlich längeres Snippet hier"),
            _fh("C", "RE-00604.pdf", snippet=None),
        ]
        out = _dedupe_file_hits(hits)
        assert len(out) == 1
        # Bestes (längstes) Snippet gewinnt, erster (relevanz-höchster) Treffer bleibt.
        assert out[0].id == "A"
        assert out[0].snippet == "ein deutlich längeres Snippet hier"

    def test_case_insensitive_name(self):
        out = _dedupe_file_hits([_fh("A", "Bericht.docx"), _fh("B", "bericht.DOCX")])
        assert len(out) == 1

    def test_different_size_kept_separate(self):
        # Gleicher Name, aber unterschiedliche Grösse -> vermutlich verschiedene Dateien.
        out = _dedupe_file_hits([_fh("A", "foo.pdf", size=100), _fh("B", "foo.pdf", size=200)])
        assert len(out) == 2

    def test_order_preserved(self):
        out = _dedupe_file_hits([_fh("A", "a.pdf"), _fh("B", "b.pdf"), _fh("C", "a.pdf")])
        assert [h.name for h in out] == ["a.pdf", "b.pdf"]


class TestMergeDocuments:
    def test_live_and_index_same_file_merge(self):
        live = _file_candidates([_fh("LIVE", "Report.pdf", snippet="kurz", url="https://drv/LIVE")])
        index = _index_candidates([
            _idx("onedrive", "IDX", "Report.pdf",
                 snippet="deutlich längere Index-Passage als Vorschau",
                 url="https://idx/report"),
        ])
        out = _merge_documents(live, index)
        assert len(out) == 1
        d = out[0]
        # Live-URL (erstplatziert) hat Vorrang -- garantiert klickbar.
        assert d.url == "https://drv/LIVE"
        # Längstes Snippet gewinnt als informativste Vorschau.
        assert d.snippet == "deutlich längere Index-Passage als Vorschau"
        assert d.source_type == "onedrive"

    def test_invoice_flood_collapses(self):
        live = _file_candidates([_fh(f"L{i}", "RE-00675.pdf", snippet="x" * (i + 1)) for i in range(3)])
        index = _index_candidates([_idx("onedrive", "I1", "RE-00675.pdf")])
        out = _merge_documents(live, index)
        assert len(out) == 1

    def test_folders_excluded(self):
        live = _file_candidates([
            _fh("F", "Ordner", is_folder=True, mime=None),
            _fh("D", "Datei.pdf"),
        ])
        out = _merge_documents(live)
        assert [d.title for d in out] == ["Datei.pdf"]

    def test_ranking_by_rrf_score(self):
        # Treffer, der in beiden Listen früh auftaucht, muss oben landen.
        live = _file_candidates([_fh("A", "Top.pdf"), _fh("B", "Other.pdf")])
        index = _index_candidates([
            _idx("onedrive", "A2", "Top.pdf"),
            _idx("onedrive", "C2", "Third.pdf"),
        ])
        out = _merge_documents(live, index)
        assert out[0].title == "Top.pdf"
        assert out[0].score is not None


class TestEmailMerge:
    def test_live_and_index_same_email_merge(self):
        # Live-E-Mail (Graph) und Index-E-Mail teilen die Message-ID -> eine Zeile.
        live_mail = [{
            "source_type": "email", "id": "MSG-1", "title": "Betreff",
            "url": "https://outlook/MSG-1", "mime_type": "message/rfc822",
            "snippet": "Absender: kurzer Preview",
        }]
        index_mail = _index_candidates([
            _idx("email", "MSG-1", "Betreff",
                 snippet="längere semantische Passage aus dem Mail-Body",
                 url="https://idx/MSG-1", mime="message/rfc822"),
        ])
        out = _merge_documents(live_mail, index_mail)
        assert len(out) == 1
        d = out[0]
        assert d.source_type == "email"
        # Live-Link (erstplatziert) gewinnt -> Sprung in die Inbox via Message-ID.
        assert d.url == "https://outlook/MSG-1"
        assert d.id == "MSG-1"

    def test_distinct_emails_stay_separate(self):
        mails = [
            {"source_type": "email", "id": f"MSG-{i}", "title": f"Betreff {i}",
             "url": f"https://outlook/{i}", "mime_type": "message/rfc822",
             "snippet": "x"}
            for i in range(3)
        ]
        out = _merge_documents(mails)
        assert {d.id for d in out} == {"MSG-0", "MSG-1", "MSG-2"}


class TestPresenceThroughRetrieval:
    """Kern-Regression: E-Mails (eigene Rangliste) dürfen von einer beliebig grossen
    OneDrive-Menge (andere Rangliste) NICHT verdrängt werden -- ohne jede Quote.
    """

    def test_emails_survive_massive_onedrive_flood(self):
        drive = _index_candidates([
            _idx("onedrive", f"OD{i}", f"RE-{i:05d}.pdf") for i in range(500)
        ])
        mail = [{
            "source_type": "email", "id": f"MSG{i}", "title": f"Betreff {i}",
            "url": f"https://outlook/{i}", "mime_type": "message/rfc822", "snippet": "m",
        } for i in range(5)]

        out = _merge_documents(drive, mail)
        mail_ids = {d.id for d in out if d.source_type == "email"}
        assert mail_ids == {"MSG0", "MSG1", "MSG2", "MSG3", "MSG4"}

    def test_top_email_ranks_with_top_files(self):
        # Da jede Liste eigenständig ab Rang 0 zählt, hat die Top-Mail denselben
        # RRF-Beitrag wie das Top-Dokument -> sie steht weit oben, nicht am Ende.
        drive = _index_candidates([
            _idx("onedrive", f"OD{i}", f"Datei-{i}.pdf") for i in range(200)
        ])
        mail = [{
            "source_type": "email", "id": "MSG-TOP", "title": "Wichtige Mail",
            "url": "https://outlook/top", "mime_type": "message/rfc822", "snippet": "m",
        }]
        out = _merge_documents(mail, drive)
        pos = next(i for i, d in enumerate(out) if d.id == "MSG-TOP")
        assert pos == 0

    def test_empty_lists_are_ignored(self):
        out = _merge_documents([], [], _index_candidates([_idx("onedrive", "X", "a.pdf")]))
        assert len(out) == 1


class TestKeywordFirst:
    """Keyword-gedeckte Treffer stehen immer vor rein-semantischen -- unabhaengig vom
    RRF-Score (der nur innerhalb der Gruppe entscheidet). Nichts wird entfernt.
    """

    def test_keyword_hit_ranks_above_higher_scored_semantic_only(self):
        # Rein-semantischer Treffer mit KUENSTLICH hohem Score (in drei Ranglisten an
        # Rang 0), Keyword-Treffer nur einmal -> niedrigerer Score.
        noise = _index_candidates([_idx("onedrive", "NOISE", "Rohdaten.xlsx", matched_keyword=False)])
        real = _index_candidates([_idx("email", "REAL", "Vertrag Merz.pdf", matched_keyword=True)])
        out = _merge_documents(noise, noise, noise, real)
        assert out[0].title == "Vertrag Merz.pdf"
        assert out[0].matched_keyword is True
        assert out[-1].matched_keyword is False
        # Recall bleibt: beide Treffer sind vorhanden.
        assert {d.title for d in out} == {"Vertrag Merz.pdf", "Rohdaten.xlsx"}

    def test_live_sources_are_keyword_backed(self):
        assert _file_candidates([_fh("A", "Doc.pdf")])[0]["matched_keyword"] is True
        assert _index_candidates([_idx("onedrive", "B", "Other.pdf")])[0]["matched_keyword"] is False

    def test_keyword_flag_sticky_across_merge(self):
        # Dieselbe Datei live (keyword) UND als semantischer Index-Treffer -> keyword.
        idx = _index_candidates([_idx("onedrive", "B", "Report.pdf", matched_keyword=False)])
        live = _file_candidates([_fh("A", "Report.pdf")])
        out = _merge_documents(idx, live)  # Index zuerst (semantik-only), dann Live
        assert len(out) == 1
        assert out[0].matched_keyword is True

    def test_all_semantic_only_keeps_score_order(self):
        # Ohne jeden Keyword-Treffer bleibt die bisherige (Score-)Reihenfolge erhalten.
        a = _index_candidates([_idx("onedrive", "A", "A.pdf", matched_keyword=False)])
        b = _index_candidates([_idx("onedrive", "B", "B.pdf", matched_keyword=False)])
        out = _merge_documents(a, a, b)  # A hat hoeheren Score (zwei Listen)
        assert out[0].title == "A.pdf"
        assert all(d.matched_keyword is False for d in out)
