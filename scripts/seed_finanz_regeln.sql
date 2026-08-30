-- Deterministische Triage-Regeln für wiederkehrende Maschinen-Rechnungsabsender.
--
-- Warum überhaupt Regeln und nicht bloss eine bessere Anweisung an das Modell?
-- Weil die Menge dieser Absender geschlossen und stabil ist. Ein Automat, der jeden
-- Monat dieselbe Rechnung schickt, ist keine Auslegungsfrage -- ihn dem Modell
-- vorzulegen heisst, jedes Mal neu zu würfeln. Gemessen kam bei genau denselben
-- Absendern in einem Monat dreimal `System`, zweimal `Finanzen` und einmal `Rechnung`
-- heraus; `invoice@metanet.ch` traf es bei zehn Mails nicht ein einziges Mal richtig.
-- `System` ist dabei der teure Fehlgriff, denn dieses Label zieht einen Move nach
-- `Inbox/System` nach sich: die Rechnung verschwindet aus der Inbox.
--
-- Aufnahmekriterium, an den Daten gemessen und nicht geschätzt: Es wird nur ein
-- Absender aufgenommen, dessen **gesamter** gemessener Verkehr Geldthemen betrifft.
-- Eine Absenderregel greift auf jede Mail dieser Adresse, nicht nur auf die mit
-- «Rechnung» im Betreff -- sie ist deshalb nur dort zulässig, wo der Absender nichts
-- anderes tut. Bewusst NICHT aufgenommen, obwohl sie Rechnungen schicken:
--
--   noreply@notify.cloudflare.com   4 von 9 Mails (der Rest sind Access-Login-Codes)
--   hello@1password.com             2 von 3   (2FA-Hinweise sind System)
--   microsoft-noreply@microsoft.com 2 von 4   (Konto- und Abo-Meldungen)
--   no-reply@swissmade.host         2 von 6   (Support-Tickets)
--
-- Für diese vier bleibt das Modell zuständig, weil die Entscheidung tatsächlich am
-- Inhalt hängt. Genau diese Grenze ist der Sinn der Trennung: Geschlossene Mengen
-- gehören in Daten, offene ins Modell.
--
-- Idempotent: `ON CONFLICT` gibt es hier nicht (kein Unique-Index auf `rule_text`),
-- deshalb löscht das Skript seine eigenen Regeln am `evidence->>'seed'`-Merkmal und
-- legt sie neu an. Wiederholtes Ausführen ändert nichts, ausser dass `applied_count`
-- zurückgesetzt wird -- das ist eine Anzeige, keine Wahrheit.
--
-- Ausführen:
--   docker exec -i taskpilot-postgres-prod psql -U taskpilot -d taskpilot_prod \
--     < scripts/seed_finanz_regeln.sql

BEGIN;

DELETE FROM learned_rules WHERE evidence->>'seed' = 'finanz_regeln_2026_08';

INSERT INTO learned_rules (
    user_id, scope, rule_text, evidence, status, rule_type,
    match_conditions, action, priority, approved_at
)
SELECT
    (SELECT id FROM users WHERE role = 'owner' ORDER BY created_at LIMIT 1),
    'triage',
    r.rule_text,
    jsonb_build_object(
        'seed', 'finanz_regeln_2026_08',
        'gemessen_am', '2026-08-30',
        'treffer_im_bestand', r.treffer,
        'begruendung', r.begruendung
    ),
    'active',
    'deterministic',
    r.bedingungen,
    r.aktion,
    r.prio,
    now()
FROM (VALUES
    -- Prioritäten ab 30, damit die bestehende n8n-Regel (10) unberührt vorne bleibt.
    -- Sammelmuster zuerst (niedrige Priorität = früher geprüft): Die Adressform
    -- `invoice+statements@` ist eine Konvention von Stripe und den Diensten, die
    -- darüber abrechnen (Anthropic, Toggl, Render). Ein Muster deckt sie alle ab,
    -- statt für jeden neuen Dienst eine Zeile zu brauchen -- und es ist kein
    -- Wortraten, sondern eine Adresskonvention des Absenders.
    (
        'Absender enthält "invoice+statements" -> Finanzen (Abrechnungspost via Stripe)',
        '[{"field": "sender", "op": "contains", "value": "invoice+statements"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Finanzen"}'::jsonb,
        30, 7,
        '7 von 7 Mails im Bestand sind Belege (Anthropic, Toggl, Render); Labels waren gemischt System/Finanzen/leer'
    ),
    (
        'Absender enthält "failed-payments" -> Finanzen (fehlgeschlagene Zahlung)',
        '[{"field": "sender", "op": "contains", "value": "failed-payments"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Finanzen"}'::jsonb,
        31, 4,
        '4 von 4 Mails betreffen fehlgeschlagene Kartenzahlungen (Cursor via Stripe)'
    ),
    -- Einzelne Adressen danach.
    (
        'Absender invoice@metanet.ch -> Finanzen (Hosting-Rechnung)',
        '[{"field": "sender", "op": "equals", "value": "invoice@metanet.ch"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Finanzen"}'::jsonb,
        32, 10,
        '10 von 10 Mails sind Rechnungen; kein einziges Mal als Finanzen erkannt (System, Rechnung, fyi, leer)'
    ),
    (
        'Absender no-reply@salt.ch -> Finanzen (Telefonrechnung)',
        '[{"field": "sender", "op": "equals", "value": "no-reply@salt.ch"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Finanzen"}'::jsonb,
        33, 3,
        '3 von 3 Mails sind Rechnungen; zweimal als System eingeordnet und damit aus der Inbox verschoben'
    ),
    (
        'Absender office@spusu.ch -> Finanzen (Mobilfunkrechnung)',
        '[{"field": "sender", "op": "equals", "value": "office@spusu.ch"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Finanzen"}'::jsonb,
        34, 3,
        '3 von 3 Mails sind Rechnungen'
    ),
    (
        'Absender payments-noreply@google.com -> Finanzen (Google-Cloud-Abrechnung)',
        '[{"field": "sender", "op": "equals", "value": "payments-noreply@google.com"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Finanzen"}'::jsonb,
        35, 3,
        '3 von 3 Mails sind Zahlungsbestätigungen; einmal wurde "payment_confirmation" als Label erfunden'
    ),
    (
        'Absender Rechnungswesen@t-r.ch -> Finanzen (Treuhand-Rechnungswesen)',
        '[{"field": "sender", "op": "equals", "value": "rechnungswesen@t-r.ch"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Finanzen"}'::jsonb,
        36, 1,
        'Funktionspostfach des Rechnungswesens -- die einzige t-r.ch-Adresse, bei der die Herkunft das Thema garantiert'
    ),
    -- Der Gegenbeweis zur gestrichenen Domain-Regel: dieselbe Domain, anderes Label.
    (
        'Absender newsletter@t-r.ch -> Newsletter (TaxFlash)',
        '[{"field": "sender", "op": "equals", "value": "newsletter@t-r.ch"}]'::jsonb,
        '{"triage_class": "fyi", "category": "Newsletter", "folder": "Newsletter"}'::jsonb,
        37, 3,
        'TaxFlash-Versand an viele; einmal als Finanzen einsortiert, weil die alte Domain-Regel t-r.ch pauschal auf Finanzen zog'
    )
) AS r(rule_text, bedingungen, aktion, prio, treffer, begruendung);

COMMIT;

-- Kontrolle
SELECT priority, rule_text, action->>'category' AS kategorie, action->>'folder' AS ordner
FROM learned_rules
WHERE evidence->>'seed' = 'finanz_regeln_2026_08'
ORDER BY priority;
