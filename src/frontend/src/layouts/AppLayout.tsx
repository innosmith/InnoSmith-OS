import { useState, useEffect, useCallback, useMemo } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';
import { Abspielleiste, TonspurProvider } from '@signa/reader';
import { Sidebar } from '../components/Sidebar';
import { MobileHeader } from '../components/MobileHeader';
import { SearchDialog } from '../components/SearchDialog';
import { TaskDetailDialog } from '../components/TaskDetailDialog';
import { NotificationBell } from '../components/NotificationBell';
import { NotificationPanel } from '../components/NotificationPanel';
import { PullToRefresh } from '../components/PullToRefresh';
import { UpdateBanner } from '../components/UpdateBanner';
import { useBadgeData, BadgeProvider } from '../hooks/useBadges';
import { useNotifications } from '../hooks/useNotifications';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { api } from '../api/client';
import { erstelleZugang } from '../lib/signaZugang';
import { useAuth } from '../contexts/AuthContext';

interface AppSettings {
  sidebar_collapsed?: boolean;
  app_logo_url?: string | null;
  sidebar_color?: string | null;
}

export function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0);
  const [appSettings, setAppSettings] = useState<AppSettings>({});
  const navigate = useNavigate();
  const { isOwner } = useAuth();

  const badges = useBadgeData(sidebarRefreshKey);
  const notifications = useNotifications();
  const [notifPanelOpen, setNotifPanelOpen] = useState(false);

  useEffect(() => {
    if (isOwner) {
      api.get<AppSettings>('/api/settings').then((s) => {
        if (s.sidebar_collapsed) setSidebarCollapsed(true);
        setAppSettings(s);
      }).catch(() => {});
    } else {
      api.get<AppSettings>('/api/settings/branding').then((s) => {
        setAppSettings(s);
      }).catch(() => {});
    }
  }, [isOwner]);

  useEffect(() => {
    if (!isOwner) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === '/' && !searchOpen && !(e.target instanceof HTMLInputElement) && !(e.target instanceof HTMLTextAreaElement)) {
        e.preventDefault();
        setSearchOpen(true);
      }
    };
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [searchOpen, isOwner]);

  const toggleCollapse = useCallback(() => {
    const next = !sidebarCollapsed;
    setSidebarCollapsed(next);
    if (isOwner) {
      api.patch('/api/settings', { sidebar_collapsed: next }).catch(() => {});
    }
  }, [sidebarCollapsed, isOwner]);

  const handleTaskClick = useCallback(
    (taskId: string) => {
      setSearchOpen(false);
      setSelectedTaskId(taskId);
    },
    [],
  );

  const handleProjectClick = useCallback(
    (projectId: string) => {
      setSearchOpen(false);
      navigate(`/projects/${projectId}`);
    },
    [navigate],
  );

  const refreshSidebar = useCallback(() => {
    setSidebarRefreshKey((k) => k + 1);
  }, []);

  const refreshAppSettings = useCallback(() => {
    const url = isOwner ? '/api/settings' : '/api/settings/branding';
    api.get<AppSettings>(url).then((s) => {
      setAppSettings(s);
    }).catch(() => {});
  }, [isOwner]);

  const openSidebar = useCallback(() => setSidebarOpen(true), []);
  const openSearch = useCallback(() => setSearchOpen(true), []);

  const isMobile = useMediaQuery('(max-width: 1023px)');

  // Der Zugang zu Signa wird einmal gebaut. Er haengt hier oben und nicht in der
  // Signale-Seite, weil der Podcast den Seitenwechsel ueberleben muss: Wer ihn im
  // Cockpit startet und danach ein Projekt oeffnet, hoert weiter.
  const signa = useMemo(() => erstelleZugang(), []);

  return (
    <TonspurProvider api={signa}>
    <BadgeProvider value={badges}>
      <div className="app-shell flex h-dvh overflow-hidden bg-gradient-to-br from-slate-50 via-indigo-50/30 to-sky-50/40 dark:from-gray-950 dark:via-indigo-950/20 dark:to-gray-950">
        <Sidebar
          isOpen={sidebarOpen}
          collapsed={sidebarCollapsed}
          onClose={() => setSidebarOpen(false)}
          onToggleCollapse={toggleCollapse}
          onSearchOpen={openSearch}
          refreshKey={sidebarRefreshKey}
          appLogoUrl={appSettings.app_logo_url}
          sidebarColor={appSettings.sidebar_color}
        />

        <div className="flex flex-1 flex-col overflow-hidden">
          <MobileHeader
            onMenuOpen={openSidebar}
            onSearchOpen={openSearch}
            notificationCount={notifications.unreadCount}
            onNotificationOpen={() => setNotifPanelOpen((v) => !v)}
          />

          {/* Desktop notification bell */}
          {!isMobile && (
            <div className="relative flex justify-end px-4 pt-2">
              <NotificationBell
                unreadCount={notifications.unreadCount}
                onClick={() => setNotifPanelOpen((v) => !v)}
              />
              <NotificationPanel
                isOpen={notifPanelOpen}
                items={notifications.items}
                loading={notifications.loading}
                onClose={() => setNotifPanelOpen(false)}
                onFetchItems={notifications.fetchItems}
                onMarkAsRead={notifications.markAsRead}
                onMarkAllAsRead={notifications.markAllAsRead}
                onDismiss={notifications.dismiss}
              />
            </div>
          )}

          {/* Mobile notification panel */}
          {isMobile && (
            <NotificationPanel
              isOpen={notifPanelOpen}
              items={notifications.items}
              loading={notifications.loading}
              onClose={() => setNotifPanelOpen(false)}
              onFetchItems={notifications.fetchItems}
              onMarkAsRead={notifications.markAsRead}
              onMarkAllAsRead={notifications.markAllAsRead}
              onDismiss={notifications.dismiss}
              mobile
            />
          )}

          <PullToRefresh>
            <main className="flex-1 overflow-hidden" style={isMobile ? { paddingTop: 'calc(3rem + env(safe-area-inset-top, 0px))' } : undefined}>
              <Outlet context={{ refreshSidebar, refreshAppSettings }} />
            </main>
          </PullToRefresh>
        </div>

        {isOwner && (
          <SearchDialog
            isOpen={searchOpen}
            onClose={() => setSearchOpen(false)}
            onTaskClick={handleTaskClick}
            onProjectClick={handleProjectClick}
          />
        )}

        {selectedTaskId && (
          <TaskDetailDialog
            taskId={selectedTaskId}
            onClose={() => setSelectedTaskId(null)}
            onUpdated={() => setSelectedTaskId(null)}
            onOpenTask={setSelectedTaskId}
          />
        )}

        <UpdateBanner />
        {/* Die Leiste erscheint erst, wenn eine Folge geladen ist, und liegt ueber
            allem -- auch ueber der Seitenleiste. */}
        <Abspielleiste />
      </div>
    </BadgeProvider>
    </TonspurProvider>
  );
}
