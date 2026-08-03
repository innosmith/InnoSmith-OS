import { useState } from "react";
import { api } from "../api/client";

export interface TraceStep {
  type: "tool_call" | "tool_result" | "reasoning" | "assistant_message";
  tool?: string;
  call_id?: string;
  arguments?: Record<string, string>;
  tool_name?: string;
  chars?: number;
  is_error?: boolean;
  preview?: string;
  text?: string;
  /** Zugehoeriger Agenten-Lauf: classify | gather | draft. */
  pass?: string | null;
}

const PASS_LABEL: Record<string, string> = {
  classify: "Einordnung",
  gather: "Recherche",
  draft: "Entwurf",
};

export interface TraceData {
  job_id: string;
  status: string;
  session_found: boolean;
  session_file?: string;
  duration_seconds?: number | null;
  summary?: {
    total_tool_calls: number;
    total_tool_results: number;
    errors: number;
    tools_used: string[];
  };
  steps: TraceStep[];
  parse_error?: string;
}

export function TracePanel({
  jobId,
  compact,
}: {
  jobId: string;
  compact?: boolean;
}) {
  const [trace, setTrace] = useState<TraceData | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const loadTrace = async () => {
    if (trace) {
      setOpen(!open);
      return;
    }
    setLoading(true);
    try {
      const data = await api.get<TraceData>(`/api/agent-jobs/${jobId}/trace`);
      setTrace(data);
      setOpen(true);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  };

  if (compact) {
    return (
      <>
        <button
          onClick={loadTrace}
          disabled={loading}
          className="inline-flex min-h-9 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 lg:min-h-0 dark:text-gray-500 dark:hover:bg-gray-800 dark:hover:text-gray-300"
          title="Trace anzeigen"
        >
          {loading ? (
            <div className="h-2.5 w-2.5 animate-spin rounded-full border border-gray-400 border-t-transparent" />
          ) : (
            <TraceIcon className="h-3 w-3" />
          )}
          Trace
        </button>
        {open && trace && <TraceContent trace={trace} />}
      </>
    );
  }

  return (
    <div className="mt-2">
      <button
        onClick={loadTrace}
        disabled={loading}
        className="flex min-h-9 items-center gap-1.5 rounded-lg border border-gray-200 px-2.5 py-1 text-[11px] font-medium text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-700 lg:min-h-0 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
      >
        {loading ? (
          <div className="h-3 w-3 animate-spin rounded-full border border-gray-400 border-t-transparent" />
        ) : (
          <TraceIcon className="h-3 w-3" />
        )}
        {open ? "Trace ausblenden" : "Trace anzeigen"}
      </button>

      {open && trace && <TraceContent trace={trace} />}
    </div>
  );
}

function TraceContent({ trace }: { trace: TraceData }) {
  if (!trace.session_found) {
    return (
      <div className="mt-2 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-800/50">
        <p className="text-xs text-gray-400 italic">
          Keine Session-Datei gefunden
        </p>
      </div>
    );
  }

  return (
    <div className="mt-2 rounded-lg border border-gray-200 bg-gray-50 p-3 dark:border-gray-700 dark:bg-gray-800/50">
      <div className="mb-2 flex flex-wrap items-center gap-3 text-[11px] text-gray-500 dark:text-gray-400">
        {trace.duration_seconds != null && (
          <span>Dauer: {trace.duration_seconds}s</span>
        )}
        {trace.summary && (
          <>
            <span>{trace.summary.total_tool_calls} Tool-Aufrufe</span>
            {trace.summary.errors > 0 && (
              <span className="font-medium text-red-500">
                {trace.summary.errors} Fehler
              </span>
            )}
            <span className="text-gray-400">
              Tools: {trace.summary.tools_used.join(", ")}
            </span>
          </>
        )}
      </div>
      <div className="max-h-80 space-y-1 overflow-y-auto">
        {/* `wrap-anywhere` wird geerbt und senkt die Min-Content-Breite der
            Textspalte, damit Tool-Parameter und Fehler-Previews die Zeile nicht
            über den Container hinaus aufziehen. */}
        {trace.steps.map((step, i) => (
          <div key={i}>
            {/* Trennlinie beim Pass-Wechsel: erst dadurch ist erkennbar, was der
                Agent recherchiert und was er beim Schreiben getan hat. */}
            {step.pass && step.pass !== trace.steps[i - 1]?.pass && (
              <div className="mt-2 mb-1 flex items-center gap-2 first:mt-0">
                <span className="text-[10px] font-semibold tracking-wide text-gray-400 uppercase dark:text-gray-500">
                  {PASS_LABEL[step.pass] ?? step.pass}
                </span>
                <span className="h-px flex-1 bg-gray-200 dark:bg-gray-700" />
              </div>
            )}
            <div className="flex gap-2 text-[11px] wrap-anywhere">
              {step.type === "tool_call" && (
                <>
                  <span className="shrink-0 font-mono text-blue-600 dark:text-blue-400">
                    →
                  </span>
                  <span>
                    <span className="font-semibold text-blue-700 dark:text-blue-300">
                      {step.tool}
                    </span>
                    <span className="ml-1 text-gray-400">
                      (
                      {Object.entries(step.arguments || {})
                        .map(([k, v]) => `${k}=${String(v).slice(0, 40)}`)
                        .join(", ")}
                      )
                    </span>
                  </span>
                </>
              )}
              {step.type === "tool_result" && (
                <>
                  <span
                    className={`shrink-0 font-mono ${step.is_error ? "text-red-500" : "text-green-600 dark:text-green-400"}`}
                  >
                    {step.is_error ? "✗" : "←"}
                  </span>
                  <span
                    className={
                      step.is_error
                        ? "text-red-600 dark:text-red-400"
                        : "text-gray-500 dark:text-gray-400"
                    }
                  >
                    {step.tool_name}: {step.chars} Zeichen
                    {step.is_error && step.preview && (
                      <span className="ml-1 text-red-500">
                        ({step.preview.slice(0, 100)})
                      </span>
                    )}
                  </span>
                </>
              )}
              {step.type === "reasoning" && (
                <>
                  <span className="shrink-0 font-mono text-amber-500">◆</span>
                  <span className="text-gray-600 italic dark:text-gray-400">
                    {step.text}
                  </span>
                </>
              )}
              {step.type === "assistant_message" && (
                <>
                  <span className="shrink-0 font-mono text-gray-400">▸</span>
                  <span className="text-gray-600 dark:text-gray-300">
                    {step.text?.slice(0, 200)}
                  </span>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function TraceIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25H12"
      />
    </svg>
  );
}
