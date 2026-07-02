import { useEffect, useRef, useCallback } from 'react';
import { getToken } from '../api/client';

type SSEHandler = (event: string, data: string) => void;

export function useSSE(onEvent: SSEHandler) {
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  const connect = useCallback(() => {
    const token = getToken();
    if (!token) return null;

    const es = new EventSource(`/api/sse/events?token=${encodeURIComponent(token)}`);

    es.addEventListener('tasks_changed', (e) => {
      handlerRef.current('tasks_changed', e.data);
    });

    es.addEventListener('agent_jobs_changed', (e) => {
      handlerRef.current('agent_jobs_changed', e.data);
    });

    es.addEventListener('email_triage_changed', (e) => {
      handlerRef.current('email_triage_changed', e.data);
    });

    es.addEventListener('notifications_changed', (e) => {
      handlerRef.current('notifications_changed', e.data);
    });

    es.addEventListener('learned_rules_changed', (e) => {
      handlerRef.current('learned_rules_changed', e.data);
    });

    es.onerror = () => {
      es.close();
      setTimeout(() => connect(), 3000);
    };

    return es;
  }, []);

  useEffect(() => {
    const es = connect();
    return () => es?.close();
  }, [connect]);
}
