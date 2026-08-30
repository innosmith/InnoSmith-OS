-- Exportiert Regeln und Mailbestand als ein JSON-Objekt für den Trockenlauf.
--
-- Gegenstück zu ``dry_run_deterministic_rules.py``: Die Datenbank liefert die Daten,
-- das Python-Skript wertet sie mit dem echten ``evaluate_conditions`` aus. Die
-- Trennung erspart dem Trockenlauf Zugangsdaten -- ``psql`` läuft im Container, wo
-- die Verbindung ohnehin besteht.
--
--   docker exec -i taskpilot-postgres-prod psql -U taskpilot -d taskpilot_prod \
--     -tA < scripts/eval/dry_run_export.sql \
--   | .venv/bin/python scripts/eval/dry_run_deterministic_rules.py

SELECT jsonb_build_object(
    'regeln', (
        SELECT coalesce(jsonb_agg(r ORDER BY r.priority, r.created_at), '[]'::jsonb)
        FROM (
            SELECT id, priority, rule_text, match_conditions, action, created_at
            FROM learned_rules
            WHERE rule_type = 'deterministic' AND status = 'active'
        ) r
    ),
    'mails', (
        SELECT coalesce(jsonb_agg(m ORDER BY m.received_at), '[]'::jsonb)
        FROM (
            SELECT message_id, subject, from_address, received_at,
                   suggested_action->>'label' AS bisheriges_label
            FROM email_triage
        ) m
    )
);
