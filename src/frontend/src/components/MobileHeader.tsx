import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { NotificationBell } from './NotificationBell';

const PAGE_TITLES: Record<string, string> = {
  '/cockpit': 'Cockpit',
  '/pipeline': 'Agenda',
  '/agenten': 'Agenten',
  '/agenten/chat': 'Chat',
  '/projects': 'Projekte',
  '/inbox': 'Posteingang',
  '/signale': 'Signale',
  '/finanzen': 'Finanzen',
  '/finanzen/analysen': 'Analysen',
  '/debitoren': 'Debitoren',
  '/kreditoren': 'Kreditoren',
  '/kapazitaet': 'Kapazität',
  '/mindmaps': 'Mind-Maps',
  '/settings': 'Einstellungen',
};

interface MobileHeaderProps {
  onMenuOpen: () => void;
  onSearchOpen: () => void;
  notificationCount?: number;
  onNotificationOpen?: () => void;
}

function resolveTitle(pathname: string, projectName: string | null): string {
  if (PAGE_TITLES[pathname]) return PAGE_TITLES[pathname];
  if (pathname.startsWith('/agenten/chat/')) return 'Chat';
  if (pathname.startsWith('/projects/')) return projectName || 'Projekt';
  if (pathname.startsWith('/mindmaps/')) return 'Mind-Map';
  if (pathname.startsWith('/finanzen/')) return 'Finanzen';
  return 'InnoSmith OS';
}

export function MobileHeader({ onMenuOpen, onSearchOpen, notificationCount = 0, onNotificationOpen }: MobileHeaderProps) {
  const { pathname } = useLocation();
  const { isOwner } = useAuth();
  const projectId = pathname.match(/^\/projects\/([^/]+)/)?.[1] ?? null;
  const [projectName, setProjectName] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setProjectName(null);
      return;
    }
    let cancelled = false;
    api.get<{ name: string }>(`/api/projects/${projectId}`)
      .then((p) => {
        if (!cancelled) setProjectName(p.name);
      })
      .catch(() => {
        if (!cancelled) setProjectName(null);
      });
    return () => { cancelled = true; };
  }, [projectId]);

  const title = resolveTitle(pathname, projectName);

  return (
    <header className="fixed inset-x-0 top-0 z-30 border-b border-white/20 bg-white/70 backdrop-blur-xl dark:border-gray-800/60 dark:bg-gray-950/70 lg:hidden"
      style={{ paddingTop: 'env(safe-area-inset-top)', paddingLeft: 'env(safe-area-inset-left)', paddingRight: 'env(safe-area-inset-right)' }}
    >
      <div className="flex h-12 items-center px-3">
        <button
          onClick={onMenuOpen}
          className="flex h-11 w-11 items-center justify-center rounded-xl text-gray-600 transition-colors active:bg-gray-200/60 dark:text-gray-300 dark:active:bg-gray-800/60"
          aria-label="Menü öffnen"
        >
          <MenuIcon className="h-5 w-5" />
        </button>

        <h1 className="min-w-0 flex-1 truncate px-1 text-center text-[15px] font-semibold text-gray-900 dark:text-white">
          {title}
        </h1>

        <div className="flex items-center">
          {onNotificationOpen && (
            <NotificationBell
              unreadCount={notificationCount}
              onClick={onNotificationOpen}
            />
          )}
          {isOwner ? (
            <button
              onClick={onSearchOpen}
              className="flex h-11 w-11 items-center justify-center rounded-xl text-gray-600 transition-colors active:bg-gray-200/60 dark:text-gray-300 dark:active:bg-gray-800/60"
              aria-label="Suche öffnen"
            >
              <SearchIcon className="h-5 w-5" />
            </button>
          ) : (
            <div className="h-11 w-11" />
          )}
        </div>
      </div>
    </header>
  );
}

function MenuIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
    </svg>
  );
}

function SearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
    </svg>
  );
}
