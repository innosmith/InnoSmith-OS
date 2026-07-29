import { EmailBody } from './EmailBody';
import { MarkdownView } from './MarkdownView';

const TRIAGE_LABEL_MAP: Record<string, string> = {
  triage_class: 'Klasse',
  label: 'Label',
  task_title: 'Aufgabe',
  task_description: 'Beschreibung',
  subject: 'Betreff',
  rationale: 'Begründung',
  deadline: 'Deadline',
  suggested_project: 'Projekt',
  reply_expected: 'Antwort erwartet',
  to: 'An',
  cc: 'CC',
  body_preview: 'Vorschau',
};

const HIDDEN_KEYS = new Set(['id', 'draft_id', 'conversation_id', 'email_message_id', 'pipedrive_person_id', 'pipedrive_deal_id']);

function extractJsonFence(text: string): { before: string; json: Record<string, unknown>; after: string } | null {
  const match = text.match(/```json\s*\n([\s\S]*?)\n\s*```/);
  if (!match) return null;
  try {
    const parsed = JSON.parse(match[1]);
    if (typeof parsed !== 'object' || parsed === null) return null;
    const idx = match.index!;
    return {
      before: text.slice(0, idx).trim(),
      json: parsed as Record<string, unknown>,
      after: text.slice(idx + match[0].length).trim(),
    };
  } catch {
    return null;
  }
}

function TriageFields({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(([k]) => !HIDDEN_KEYS.has(k));
  if (entries.length === 0) return null;

  return (
    <div className="space-y-1 text-sm wrap-anywhere">
      {entries.map(([key, val]) => {
        if (val === null || val === undefined || val === '') return null;
        const label = TRIAGE_LABEL_MAP[key] || key.replace(/_/g, ' ');
        const display = typeof val === 'boolean'
          ? (val ? 'Ja' : 'Nein')
          : typeof val === 'string'
            ? val
            : JSON.stringify(val);
        return (
          <p key={key}>
            <span className="font-medium capitalize">{label}:</span> {display}
          </p>
        );
      })}
    </div>
  );
}

export function FormattedOutput({ output }: { output: string }) {
  if (!output) return null;

  const fenced = extractJsonFence(output);
  if (fenced) {
    return (
      <div className="space-y-3">
        {fenced.before && <MarkdownView text={fenced.before} />}
        <TriageFields data={fenced.json} />
        {fenced.after && <MarkdownView text={fenced.after} />}
      </div>
    );
  }

  try {
    const parsed = JSON.parse(output);
    if (parsed.body_html) {
      return <EmailBody html={parsed.body_html} />;
    }
    if (parsed.body || parsed.text || parsed.message) {
      return <p className="whitespace-pre-wrap wrap-anywhere">{parsed.body || parsed.text || parsed.message}</p>;
    }
    if (parsed.draft_id || parsed.subject || parsed.triage_class) {
      return <TriageFields data={parsed} />;
    }
    const entries = Object.entries(parsed).filter(([k]) => !HIDDEN_KEYS.has(k));
    if (entries.length > 0) {
      return <TriageFields data={Object.fromEntries(entries)} />;
    }
  } catch {
    // not pure JSON
  }

  return <MarkdownView text={output} />;
}
