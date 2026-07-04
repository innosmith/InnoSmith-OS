"""Tests fuer die reinen Helfer der semantischen Suche (ohne DB/LLM/Graph).

- chunk_text: überlappendes Chunking, Grenzverhalten, Mindestlänge
- is_indexable_file: MIME-/Endungs-Whitelist
- rrf_fuse: Reciprocal Rank Fusion (deterministische Rangkombination)
"""

from app.services.semantic_index import chunk_text, is_indexable_file
from app.services.semantic_search import rrf_fuse


class TestChunkText:
    def test_empty_returns_empty(self):
        assert chunk_text("", 1000, 150) == []
        assert chunk_text("   \n  ", 1000, 150) == []

    def test_short_text_single_chunk(self):
        out = chunk_text("Dies ist ein kurzer Satz über Steuern.", 1000, 150)
        assert len(out) == 1
        assert "Steuern" in out[0]

    def test_long_text_multiple_overlapping_chunks(self):
        body = "Absatz. " * 500  # ~4000 Zeichen
        out = chunk_text(body, 1000, 150)
        assert len(out) >= 4
        # Jeder Chunk hält sich grob ans Fenster (weiche Grenze erlaubt etwas Puffer)
        assert all(len(c) <= 1200 for c in out)

    def test_overlap_creates_continuity(self):
        body = "".join(f"Wort{i} " for i in range(400))
        out = chunk_text(body, 500, 100)
        assert len(out) >= 2

    def test_min_chunk_length_filters_tiny_tail(self):
        out = chunk_text("a", 1000, 150)
        assert out == []  # unter _MIN_CHUNK_CHARS


class TestIsIndexableFile:
    def test_documents_whitelisted(self):
        for name in ("report.pdf", "notes.DOCX", "sheet.xlsx", "readme.md", "data.csv", "page.html"):
            assert is_indexable_file(name) is True

    def test_binary_media_rejected(self):
        for name in ("photo.jpg", "clip.mp4", "audio.mp3", "archive.zip", "app.exe", "noext"):
            assert is_indexable_file(name) is False


class TestRrfFuse:
    def test_item_in_both_lists_ranks_first(self):
        semantic = [
            {"source_type": "onedrive", "source_id": "A", "snippet": "s-a"},
            {"source_type": "email", "source_id": "B", "snippet": "s-b"},
        ]
        keyword = [
            {"source_type": "email", "source_id": "B", "snippet": "k-b"},
            {"source_type": "onedrive", "source_id": "C", "snippet": "k-c"},
        ]
        fused = rrf_fuse(semantic, keyword)
        # B erscheint in beiden Listen -> höchster kombinierter Score
        assert (fused[0]["source_type"], fused[0]["source_id"]) == ("email", "B")
        # Alle drei eindeutigen Quellen sind enthalten
        keys = {(f["source_type"], f["source_id"]) for f in fused}
        assert keys == {("onedrive", "A"), ("email", "B"), ("onedrive", "C")}

    def test_score_is_descending(self):
        semantic = [{"source_type": "x", "source_id": str(i), "snippet": ""} for i in range(5)]
        fused = rrf_fuse(semantic, [])
        scores = [f["score"] for f in fused]
        assert scores == sorted(scores, reverse=True)

    def test_semantic_snippet_preferred_over_keyword(self):
        semantic = [{"source_type": "email", "source_id": "B", "snippet": "semantik-passage"}]
        keyword = [{"source_type": "email", "source_id": "B", "snippet": "keyword-headline"}]
        fused = rrf_fuse(semantic, keyword)
        assert fused[0]["snippet"] == "semantik-passage"

    def test_empty_inputs(self):
        assert rrf_fuse([], []) == []
