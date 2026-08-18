import { useEffect, useRef } from 'react';
import { getToken } from '../api/client';

type SSEHandler = (event: string, data: string) => void;

const EVENTS = [
  'tasks_changed',
  'agent_jobs_changed',
  'email_triage_changed',
  'notifications_changed',
  'learned_rules_changed',
] as const;

/**
 * Wartezeit, in der gleichartige Events zu einem zusammengefasst werden.
 *
 * Die Events entstehen aus einem Postgres-Trigger, der pro Zeile feuert. Ein
 * Bulk-Update trifft die Oberfläche darum als Lawine: am 18.08.2026 löste eine
 * Migration über 216 Tasks 216 Events aus, und weil jeder Konsument daraufhin
 * alles neu lud, entstanden 2858 HTTP-Anfragen in 36 Sekunden -- genug, um das
 * Postfach bei Microsoft in die Drosselung zu treiben.
 */
const COALESCE_MS = 500;

/** Mindestabstand zwischen zwei Refreshes desselben Event-Typs. */
const MIN_INTERVAL_MS = 2000;

/** Reconnect-Wartezeiten in Millisekunden (letzter Wert gilt dauerhaft). */
const RECONNECT_BACKOFF_MS = [1000, 3000, 5000, 10000, 30000];

/*
 * Eine Verbindung für die ganze App, nicht eine pro Konsument.
 *
 * Auf dem Cockpit hängen Badges, Benachrichtigungen, Briefings und die Seite
 * selbst am Stream. Jeder eigene EventSource kostete serverseitig eine eigene
 * Postgres-Verbindung (der SSE-Endpunkt macht pro Client ein LISTEN) -- ein
 * halbes Dutzend pro offenem Browser-Tab. Geteilt ist es eine, und das Bündeln
 * der Events wirkt für alle Konsumenten gemeinsam statt jeweils einzeln.
 */
const subscribers = new Set<SSEHandler>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const lastEmit = new Map<string, number>();

let source: EventSource | null = null;
let reconnectAttempt = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

/** Bündelt Events desselben Typs und respektiert den Mindestabstand. */
function emit(event: string, data: string) {
  const bestehend = timers.get(event);
  if (bestehend) clearTimeout(bestehend);

  const seitLetztem = Date.now() - (lastEmit.get(event) ?? 0);
  const wartezeit = Math.max(COALESCE_MS, MIN_INTERVAL_MS - seitLetztem);

  timers.set(
    event,
    setTimeout(() => {
      timers.delete(event);
      lastEmit.set(event, Date.now());
      for (const handler of subscribers) handler(event, data);
    }, wartezeit),
  );
}

function connect() {
  const token = getToken();
  if (!token || source) return;

  const es = new EventSource(`/api/sse/events?token=${encodeURIComponent(token)}`);
  source = es;

  for (const name of EVENTS) {
    es.addEventListener(name, (e) => emit(name, (e as MessageEvent).data));
  }

  es.onopen = () => {
    reconnectAttempt = 0;
  };

  es.onerror = () => {
    es.close();
    if (source === es) source = null;
    if (subscribers.size === 0) return;
    const index = Math.min(reconnectAttempt, RECONNECT_BACKOFF_MS.length - 1);
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(connect, RECONNECT_BACKOFF_MS[index]);
  };
}

function disconnect() {
  source?.close();
  source = null;
  reconnectAttempt = 0;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  for (const timer of timers.values()) clearTimeout(timer);
  timers.clear();
}

export function useSSE(onEvent: SSEHandler) {
  const handlerRef = useRef(onEvent);

  // Nach jedem Render nachziehen: die Anmeldung unten läuft einmalig, greift
  // aber über den Ref immer auf den aktuellen Callback zu.
  useEffect(() => {
    handlerRef.current = onEvent;
  });

  useEffect(() => {
    const handler: SSEHandler = (event, data) => handlerRef.current(event, data);
    subscribers.add(handler);
    connect();
    return () => {
      subscribers.delete(handler);
      if (subscribers.size === 0) disconnect();
    };
  }, []);
}
