-- Synthetische Kapazitätsdaten für die DEV-Datenbank (taskpilot_dev).
--
-- Baut den bekannten Fehlerfall vom 03.08.2026 nach: Für Juli 2026 war Budget
-- geplant, für August nicht. Der Entwurf nannte trotzdem «14h verfügbar» als
-- heutigen Stand -- weil der Agent die Zahl aus einer Juli-Mail las und kein
-- Fachsystem hatte, das ihm den Augustwert gesagt hätte.
--
-- Zweck: der Kapazitäts-MCP-Server muss für August 0h ausweisen und für Juli den
-- Planwert. Nur in DEV ausführen.
--
--   docker exec -i taskpilot-postgres sh -c \
--     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < scripts/seed_capacity_dev.sql

BEGIN;

DELETE FROM capacity_allocations WHERE capacity_project_id IN (
    SELECT id FROM capacity_projects WHERE name IN ('Cheetah', 'Sympholio')
);
DELETE FROM capacity_projects WHERE name IN ('Cheetah', 'Sympholio');
DELETE FROM capacity_time_off WHERE label = 'Testferien';

INSERT INTO capacity_projects (id, name, client_name, status, toggl_project_id, hourly_rate)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'Cheetah', 'OneMBA', 'bestätigt', 901, 180),
    ('22222222-2222-2222-2222-222222222222', 'Sympholio', 'OneMBA', 'bestätigt', 902, 180);

-- Juli 2026: beide Projekte bewirtschaftet (3.5h pro Woche = 14h im Monat).
INSERT INTO capacity_allocations (capacity_project_id, week_start, minutes, allocation_type)
SELECT '11111111-1111-1111-1111-111111111111', d::date, 194, 'week'
FROM generate_series('2026-06-29'::date, '2026-07-27'::date, interval '7 day') d;

INSERT INTO capacity_allocations (capacity_project_id, week_start, minutes, allocation_type)
SELECT '22222222-2222-2222-2222-222222222222', d::date, 194, 'week'
FROM generate_series('2026-06-29'::date, '2026-07-27'::date, interval '7 day') d;

-- August 2026: bewusst NICHTS. Das ist der Kern des Falls.

INSERT INTO capacity_time_off (date, type, label, hours)
VALUES
    ('2026-08-10', 'ferien', 'Testferien', 8),
    ('2026-08-11', 'ferien', 'Testferien', 8);

COMMIT;
