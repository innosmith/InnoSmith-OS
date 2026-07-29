/**
 * Selbstheilung bei veralteten Lazy-Chunks.
 *
 * Nach einem Deploy referenziert eine gecachte `index.html` Chunk-Hashes, die auf
 * dem Server nicht mehr existieren. Der dynamische Import scheitert dann mit
 * «Failed to fetch dynamically imported module» und riss bisher die ganze App in
 * den Error-Boundary. Statt dem Nutzer einen Fehler zu zeigen, verwerfen wir den
 * veralteten Service-Worker-Precache und laden genau einmal neu.
 */

const CHUNK_ERROR_PATTERN =
  /dynamically imported module|importing a module script failed|chunkloaderror|error loading dynamically imported module|failed to fetch dynamically/i;

const RELOAD_MARKER = 'tp_chunk_reload_at';
const RELOAD_COOLDOWN_MS = 30_000;

export function isChunkLoadError(error: unknown): boolean {
  if (!error) return false;
  const err = error as { name?: string; message?: string };
  if (err.name === 'ChunkLoadError') return true;
  return CHUNK_ERROR_PATTERN.test(err.message ?? '');
}

async function dropStaleServiceWorkerCaches(): Promise<void> {
  try {
    if ('serviceWorker' in navigator) {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map(r => r.unregister()));
    }
    if ('caches' in window) {
      const names = await caches.keys();
      // API-Antworten dürfen bleiben; nur die App-Shell ist veraltet.
      await Promise.all(
        names.filter(name => !name.startsWith('api-')).map(name => caches.delete(name)),
      );
    }
  } catch {
    /* Best-effort: im Zweifel trotzdem neu laden. */
  }
}

/**
 * Versucht, einen Chunk-Fehler durch einen einmaligen Reload zu beheben.
 * Gibt `true` zurück, wenn ein Reload ausgelöst wurde (Aufrufer soll dann keine
 * Fehlermeldung rendern).
 */
export function recoverFromChunkError(error: unknown): boolean {
  if (!isChunkLoadError(error)) return false;

  let lastAttempt = 0;
  try {
    lastAttempt = Number(sessionStorage.getItem(RELOAD_MARKER) ?? 0);
  } catch {
    /* Private-Mode ohne sessionStorage: einmaliger Versuch ist dann nicht garantiert. */
  }

  // Zweiter Fehler kurz nach einem Reload heisst: Reload hilft nicht — Fehler zeigen.
  if (lastAttempt && Date.now() - lastAttempt < RELOAD_COOLDOWN_MS) return false;

  try {
    sessionStorage.setItem(RELOAD_MARKER, String(Date.now()));
  } catch {
    /* siehe oben */
  }

  void dropStaleServiceWorkerCaches().then(() => window.location.reload());
  return true;
}

/** Fängt Chunk-Fehler ab, die ausserhalb des React-Renderings auftreten. */
export function installChunkErrorRecovery(): void {
  window.addEventListener('unhandledrejection', event => {
    if (recoverFromChunkError(event.reason)) event.preventDefault();
  });
}
