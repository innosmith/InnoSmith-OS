-- Synthetische Suchdokumente für die DEV-Datenbank (taskpilot_dev).
--
-- Baut den Fall vom 03.08.2026 nach: Eine Juli-Mail nennt ein Juli-Budget. Der
-- Agent zog diesen Satz als heutigen Stand in einen August-Entwurf, weil das
-- Trefferobjekt kein Datum trug -- die Aktualität war für das Modell unprüfbar.
--
-- Zweck: die Suche muss zu jedem Treffer `date` und `from` liefern, damit der
-- Recherche-Prompt jeden Fakt datieren kann. Nur in DEV ausführen.
--
--   docker exec -i taskpilot-postgres sh -c \
--     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < scripts/seed_semantic_dev.sql

BEGIN;

DELETE FROM semantic_documents WHERE source_id LIKE 'testfall-%';

INSERT INTO semantic_documents
    (user_id, source_type, source_id, chunk_index, title, content_text, url, mime,
     metadata, source_modified_at)
SELECT
    u.id, 'email', 'testfall-juli', 0, 'Kapazität Juli',
    'Liebe Rahel. Im Wissen dass die Ressourcen knapp sind: Für Juli stehen noch '
    '14h Budget für Cheetah zur Verfügung. Sympholio können wir bis Ende Juli '
    'ebenfalls nutzen.',
    NULL, 'message/rfc822',
    '{"from": "anthony@innosmith.ch"}'::jsonb,
    '2026-07-02T09:00:00+02:00'::timestamptz
FROM users u WHERE u.role = 'owner' ORDER BY u.created_at LIMIT 1;

INSERT INTO semantic_documents
    (user_id, source_type, source_id, chunk_index, title, content_text, url, mime,
     metadata, source_modified_at)
SELECT
    u.id, 'email', 'testfall-august', 0, 'Website Priorisierung',
    'Hallo Anthony. QM Pilot ist kein verlässlicher Bezugspunkt mehr. Wir gehen die '
    'Zielsetzung der Website im Weekly nochmals durch und priorisieren dann.',
    NULL, 'message/rfc822',
    '{"from": "simone@onemba.example"}'::jsonb,
    '2026-08-01T14:30:00+02:00'::timestamptz
FROM users u WHERE u.role = 'owner' ORDER BY u.created_at LIMIT 1;

COMMIT;
