import { useRegisterSW } from 'virtual:pwa-register/react';

export function UpdateBanner() {
  const {
    needRefresh: [needRefresh],
    updateServiceWorker,
  } = useRegisterSW({
    // Sofort nach Registrierung und beim Fokus prüfen — sonst bleibt nach
    // `make prod` oft noch Stunden der alte Precache (index.html + JS) aktiv.
    immediate: true,
    onRegisteredSW(_url, registration) {
      if (!registration) return;
      void registration.update();
      setInterval(() => registration.update(), 60 * 1000);
      const onFocus = () => { void registration.update(); };
      window.addEventListener('focus', onFocus);
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') onFocus();
      });
    },
  });

  if (!needRefresh) return null;

  const handleUpdate = () => {
    updateServiceWorker(true);
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-50 flex items-center justify-center p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))] lg:bottom-4 lg:left-auto lg:right-4 lg:inset-x-auto">
      <div className="flex items-center gap-3 rounded-xl border border-indigo-200 bg-white/95 px-4 py-2.5 shadow-lg backdrop-blur-md dark:border-indigo-700/50 dark:bg-gray-900/95">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-indigo-100 dark:bg-indigo-900/50">
          <svg className="h-4 w-4 text-indigo-600 dark:text-indigo-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
            <path d="M3 3v5h5" />
            <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
            <path d="M16 21h5v-5" />
          </svg>
        </div>
        <span className="text-sm font-medium text-gray-700 dark:text-gray-200">
          Neue Version verfügbar
        </span>
        <button
          onClick={handleUpdate}
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-indigo-700 active:bg-indigo-800"
        >
          Aktualisieren
        </button>
      </div>
    </div>
  );
}
