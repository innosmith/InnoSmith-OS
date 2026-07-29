import { useState, useEffect, useCallback, useRef } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { DndContext, closestCenter, PointerSensor, TouchSensor, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy, useSortable, arrayMove } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { BrainCircuit, CalendarClock } from 'lucide-react';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useTheme } from '../contexts/ThemeContext';
import { useBadges } from '../hooks/useBadges';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { ThemeToggle } from './ThemeToggle';
import { ProjectIcon } from './ProjectIcon';
import type { Project } from '../types';

const SIDEBAR_COLORS: Record<string, { light: string; dark: string }> = {
  default: { light: 'bg-white', dark: 'dark:bg-gray-950' },
  slate: { light: 'bg-slate-50', dark: 'dark:bg-slate-950' },
  zinc: { light: 'bg-zinc-50', dark: 'dark:bg-zinc-950' },
  stone: { light: 'bg-stone-50', dark: 'dark:bg-stone-950' },
  indigo: { light: 'bg-indigo-50', dark: 'dark:bg-indigo-950' },
  blue: { light: 'bg-blue-50', dark: 'dark:bg-blue-950' },
  sky: { light: 'bg-sky-50', dark: 'dark:bg-sky-950' },
  emerald: { light: 'bg-emerald-50', dark: 'dark:bg-emerald-950' },
};

interface SidebarProps {
  isOpen: boolean;
  collapsed: boolean;
  onClose: () => void;
  onToggleCollapse: () => void;
  onSearchOpen: () => void;
  refreshKey: number;
  appLogoUrl?: string | null;
  sidebarColor?: string | null;
}

export function Sidebar({
  isOpen,
  collapsed: collapsedPref,
  onClose,
  onToggleCollapse,
  onSearchOpen,
  refreshKey,
  appLogoUrl,
  sidebarColor,
}: SidebarProps) {
  // Einklappen ist eine Desktop-Präferenz. Mobil ist der Drawer immer beschriftet —
  // sonst bleibt nur eine 56px-Icon-Spalte, die sich mobil nicht aufklappen lässt.
  const isDesktop = useMediaQuery('(min-width: 1024px)');
  const collapsed = collapsedPref && isDesktop;
  const [projects, setProjects] = useState<Project[]>([]);
  const [sidebarOrder, setSidebarOrder] = useState<string[] | null>(null);
  const [projectsExpanded, setProjectsExpanded] = useState(true);
  const orderLoaded = useRef(false);
  const { activeJobCount, unreadMailCount, pendingDecisions, focusTaskCount } = useBadges();
  const { logout, isOwner } = useAuth();
  const navigate = useNavigate();

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 5 } }),
  );

  useEffect(() => {
    api.get<Project[]>('/api/projects').then(setProjects).catch(() => {});
    if (!orderLoaded.current) {
      orderLoaded.current = true;
      api.get<{ project_sidebar_order?: string[] | null }>('/api/settings')
        .then((s) => { if (s.project_sidebar_order) setSidebarOrder(s.project_sidebar_order); })
        .catch(() => {});
    }
  }, [refreshKey]);

  const sortedProjects = sidebarOrder
    ? [...projects].sort((a, b) => {
        const ia = sidebarOrder.indexOf(a.id);
        const ib = sidebarOrder.indexOf(b.id);
        if (ia === -1 && ib === -1) return 0;
        if (ia === -1) return 1;
        if (ib === -1) return -1;
        return ia - ib;
      })
    : projects;

  const handleDragEnd = useCallback((event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const ids = sortedProjects.map((p) => p.id);
    const oldIndex = ids.indexOf(active.id as string);
    const newIndex = ids.indexOf(over.id as string);
    if (oldIndex === -1 || newIndex === -1) return;
    const newOrder = arrayMove(ids, oldIndex, newIndex);
    setSidebarOrder(newOrder);
    api.patch('/api/settings', { project_sidebar_order: newOrder }).catch(() => {});
  }, [sortedProjects]);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const bgClasses = SIDEBAR_COLORS[sidebarColor || 'default'] || SIDEBAR_COLORS.default;

  const linkClasses = ({ isActive }: { isActive: boolean }) =>
    `flex min-h-11 items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors lg:min-h-0 ${
      isActive
        ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300'
        : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
    }`;

  const collapsedLinkClasses = ({ isActive }: { isActive: boolean }) =>
    `flex items-center justify-center rounded-lg p-2 transition-colors ${
      isActive
        ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300'
        : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
    }`;

  const w = collapsed ? 'w-[56px]' : 'w-64';

  return (
    <>
      {isOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/30 backdrop-blur-sm lg:hidden"
          onClick={onClose}
        />
      )}

      <aside
        className={`fixed top-0 left-0 z-40 flex h-dvh ${w} flex-col border-r border-gray-200 pt-[env(safe-area-inset-top,0px)] lg:h-full ${bgClasses.light} ${bgClasses.dark} transition-[transform,visibility] duration-200 dark:border-gray-800 lg:static lg:visible lg:translate-x-0 lg:pt-0 ${
          isOpen ? 'visible translate-x-0' : 'invisible -translate-x-full'
        }`}
      >
        {/* Logo + Collapse Toggle */}
        <div className="flex items-center gap-2 border-b border-gray-200 px-3 py-3 dark:border-gray-800">
          {collapsed ? (
            <button
              onClick={onToggleCollapse}
              className="hidden h-8 w-8 shrink-0 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200 lg:flex"
              title="Sidebar ausklappen"
            >
              <MenuIcon className="h-5 w-5" />
            </button>
          ) : (
            <>
              {appLogoUrl ? (
                <img src={appLogoUrl} alt="" className="h-8 w-8 shrink-0 rounded-lg object-cover" />
              ) : (
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-indigo-600 text-sm font-bold text-white">
                  T
                </div>
              )}
              <span className="flex-1 text-lg font-semibold text-gray-900 dark:text-white">
                TaskPilot
              </span>
              <button
                onClick={onToggleCollapse}
                className="hidden shrink-0 rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300 lg:flex"
                title="Sidebar einklappen"
              >
                <CollapseIcon className="h-4 w-4" />
              </button>
            </>
          )}
        </div>

        {/* Suche (nur Owner) */}
        {isOwner && (
          !collapsed ? (
            <div className="px-3 pt-3">
              <button
                onClick={onSearchOpen}
                className="flex w-full items-center gap-2 rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-500 transition-colors hover:border-gray-300 hover:text-gray-700 dark:border-gray-700 dark:text-gray-400 dark:hover:border-gray-600 dark:hover:text-gray-300"
              >
                <SearchIcon className="h-4 w-4 shrink-0" />
                <span className="flex-1 text-left">Suchen...</span>
                <kbd className="rounded border border-gray-300 px-1.5 py-0.5 text-xs text-gray-400 dark:border-gray-600">
                  /
                </kbd>
              </button>
            </div>
          ) : (
            <div className="flex justify-center pt-3">
              <button
                onClick={onSearchOpen}
                className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
                title="Suchen (/)"
              >
                <SearchIcon className="h-5 w-5" />
              </button>
            </div>
          )
        )}

        {/* Navigation */}
        <nav className="flex-1 space-y-1 overflow-y-auto px-2 py-3">
          {collapsed ? (
            <>
              {isOwner && (
                <>
                  <NavLink to="/cockpit" className={collapsedLinkClasses} onClick={onClose} title="Cockpit">
                    <span className="relative">
                      <CockpitIcon className="h-5 w-5" />
                      {pendingDecisions > 0 && (
                        <span className="absolute -top-1 -right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-rose-500 text-[9px] font-bold text-white">
                          {pendingDecisions}
                        </span>
                      )}
                    </span>
                  </NavLink>
                  <NavLink to="/pipeline" className={collapsedLinkClasses} onClick={onClose} title="Agenda">
                    <span className="relative">
                      <AgendaIcon className="h-5 w-5" />
                      {focusTaskCount > 0 && (
                        <span className="absolute -top-1 -right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-amber-500 text-[9px] font-bold text-white">
                          {focusTaskCount > 9 ? '9+' : focusTaskCount}
                        </span>
                      )}
                    </span>
                  </NavLink>
                  <NavLink to="/agenten/chat" className={collapsedLinkClasses} onClick={onClose} title="Chat">
                    <ChatIcon className="h-5 w-5" />
                  </NavLink>
                  <NavLink to="/agenten" className={collapsedLinkClasses} onClick={onClose} title="Agenten">
                    <span className="relative">
                      <AgentIcon className="h-5 w-5" />
                      {activeJobCount > 0 && (
                        <span className="absolute -top-1 -right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-indigo-500 text-[9px] font-bold text-white">
                          {activeJobCount}
                        </span>
                      )}
                    </span>
                  </NavLink>

                  <div className="my-2 border-t border-gray-200 dark:border-gray-800" />
                </>
              )}

              <button
                onClick={() => setProjectsExpanded((v) => !v)}
                className="flex items-center justify-center rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                title={projectsExpanded ? 'Projekte einklappen' : 'Projekte ausklappen'}
              >
                {projectsExpanded ? <LayersOpenIcon className="h-5 w-5" /> : <LayersClosedIcon className="h-5 w-5" />}
              </button>

              {projectsExpanded && sortedProjects.map((project) => (
                <NavLink
                  key={project.id}
                  to={`/projects/${project.id}`}
                  className={collapsedLinkClasses}
                  onClick={onClose}
                  title={project.name}
                >
                  <ProjectIcon
                    iconUrl={project.icon_url}
                    iconEmoji={project.icon_emoji}
                    color={project.color}
                    size={20}
                  />
                </NavLink>
              ))}

              {isOwner && (
                <NavLink to="/mindmaps" className={collapsedLinkClasses} onClick={onClose} title="Mind-Maps">
                  <BrainCircuit className="h-5 w-5" />
                </NavLink>
              )}

              {isOwner && (
                <>
                  <div className="my-2 border-t border-gray-200 dark:border-gray-800" />

                  <NavLink to="/inbox" className={collapsedLinkClasses} onClick={onClose} title="Posteingang">
                    <span className="relative">
                      <MailIcon className="h-5 w-5" />
                      {unreadMailCount > 0 && (
                        <span className="absolute -top-1 -right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-blue-500 text-[9px] font-bold text-white">
                          {unreadMailCount > 9 ? '9+' : unreadMailCount}
                        </span>
                      )}
                    </span>
                  </NavLink>
                  <NavLink to="/signale" className={collapsedLinkClasses} onClick={onClose} title="Signale">
                    <SignaleIcon className="h-5 w-5" />
                  </NavLink>
                  <NavLink to="/finanzen" className={collapsedLinkClasses} onClick={onClose} title="Finanzen">
                    <FinanceIcon className="h-5 w-5" />
                  </NavLink>
                  <NavLink to="/kapazitaet" className={collapsedLinkClasses} onClick={onClose} title="Kapazität">
                    <CalendarClock className="h-5 w-5" />
                  </NavLink>
                  <NavLink to="/debitoren" className={collapsedLinkClasses} onClick={onClose} title="Debitoren">
                    <DebtorsIcon className="h-5 w-5" />
                  </NavLink>
                  <NavLink to="/kreditoren" className={collapsedLinkClasses} onClick={onClose} title="Kreditoren">
                    <CreditorsIcon className="h-5 w-5" />
                  </NavLink>
                </>
              )}
            </>
          ) : (
            <>
              {isOwner && (
                <>
                  <NavLink to="/cockpit" className={linkClasses} onClick={onClose}>
                    <CockpitIcon className="h-5 w-5" />
                    <span className="flex-1">Cockpit</span>
                    {pendingDecisions > 0 && (
                      <span className="flex items-center gap-1 rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700 dark:bg-rose-900/40 dark:text-rose-300">
                        {pendingDecisions}
                      </span>
                    )}
                  </NavLink>

                  <NavLink to="/pipeline" className={linkClasses} onClick={onClose}>
                    <AgendaIcon className="h-5 w-5" />
                    <span className="flex-1">Agenda</span>
                    {focusTaskCount > 0 && (
                      <span className="flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                        {focusTaskCount}
                      </span>
                    )}
                  </NavLink>

                  <NavLink to="/agenten/chat" className={linkClasses} onClick={onClose}>
                    <ChatIcon className="h-5 w-5" />
                    <span className="flex-1">Chat</span>
                  </NavLink>

                  <NavLink to="/agenten" className={linkClasses} onClick={onClose}>
                    <AgentIcon className="h-5 w-5" />
                    <span className="flex-1">Agenten</span>
                    {activeJobCount > 0 && (
                      <span className="flex items-center gap-1 rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-500" />
                        {activeJobCount}
                      </span>
                    )}
                  </NavLink>
                </>
              )}

              <div className="mt-4 mb-1 flex items-center gap-0">
                <button
                  onClick={() => setProjectsExpanded((v) => !v)}
                  className="shrink-0 rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                  title={projectsExpanded ? 'Projekte einklappen' : 'Projekte ausklappen'}
                >
                  {projectsExpanded ? <LayersOpenIcon className="h-5 w-5" /> : <LayersClosedIcon className="h-5 w-5" />}
                </button>
                <NavLink to="/projects" className={linkClasses} onClick={onClose} end>
                  <span className="flex-1">Projekte</span>
                </NavLink>
              </div>

              {projectsExpanded && (
                <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
                  <SortableContext items={sortedProjects.map((p) => p.id)} strategy={verticalListSortingStrategy}>
                    {sortedProjects.map((project) => (
                      <SortableProjectItem
                        key={project.id}
                        project={project}
                        linkClasses={linkClasses}
                        onClose={onClose}
                      />
                    ))}
                  </SortableContext>
                </DndContext>
              )}

              {isOwner && (
                <NavLink to="/mindmaps" className={linkClasses} onClick={onClose}>
                  <BrainCircuit className="h-5 w-5" />
                  <span className="flex-1">Mind-Maps</span>
                </NavLink>
              )}

              {isOwner && (
                <>
                  <div className="mt-4 mb-2 border-t border-gray-200 pt-2 dark:border-gray-800" />

                  <NavLink to="/inbox" className={linkClasses} onClick={onClose}>
                    <MailIcon className="h-5 w-5" />
                    <span className="flex-1">Posteingang</span>
                    {unreadMailCount > 0 && (
                      <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                        {unreadMailCount}
                      </span>
                    )}
                  </NavLink>

                  <NavLink to="/signale" className={linkClasses} onClick={onClose}>
                    <SignaleIcon className="h-5 w-5" />
                    <span className="flex-1">Signale</span>
                  </NavLink>

                  <NavLink to="/finanzen" className={linkClasses} onClick={onClose}>
                    <FinanceIcon className="h-5 w-5" />
                    <span className="flex-1">Finanzen</span>
                  </NavLink>

                  <NavLink to="/kapazitaet" className={linkClasses} onClick={onClose}>
                    <CalendarClock className="h-5 w-5" />
                    <span className="flex-1">Kapazität</span>
                  </NavLink>

                  <NavLink to="/debitoren" className={linkClasses} onClick={onClose}>
                    <DebtorsIcon className="h-5 w-5" />
                    <span className="flex-1">Debitoren</span>
                  </NavLink>

                  <NavLink to="/kreditoren" className={linkClasses} onClick={onClose}>
                    <CreditorsIcon className="h-5 w-5" />
                    <span className="flex-1">Kreditoren</span>
                  </NavLink>
                </>
              )}
            </>
          )}
        </nav>

        {/* Footer */}
        <div className="border-t border-gray-200 px-2 py-2 pb-[max(0.5rem,env(safe-area-inset-bottom,0px))] dark:border-gray-800 lg:pb-2">
          {collapsed ? (
            <div className="flex flex-col items-center gap-2">
              <NavLink to="/settings" className={collapsedLinkClasses} onClick={onClose} title="Einstellungen">
                <SettingsIcon className="h-5 w-5" />
              </NavLink>
              <ThemeCycleButton />
              <LogoutButton onLogout={handleLogout} collapsed />
            </div>
          ) : (
            <>
              <NavLink to="/settings" className={linkClasses} onClick={onClose}>
                <SettingsIcon className="h-5 w-5" />
                Einstellungen
              </NavLink>
              <div className="mt-2 flex items-center justify-between px-1">
                <ThemeToggle />
                <LogoutButton onLogout={handleLogout} />
              </div>
            </>
          )}
        </div>
      </aside>
    </>
  );
}

/* ── ThemeCycleButton ── */

function ThemeCycleButton() {
  const { mode, setMode } = useTheme();
  const next = mode === 'light' ? 'dark' : mode === 'dark' ? 'system' : 'light';
  const labels: Record<string, string> = { light: 'Dunkel', dark: 'System', system: 'Hell' };
  return (
    <button
      onClick={() => setMode(next)}
      className="rounded-lg p-2 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
      title={`Wechsel zu: ${labels[mode]}`}
    >
      {mode === 'light' && <SunSmallIcon className="h-5 w-5" />}
      {mode === 'dark' && <MoonSmallIcon className="h-5 w-5" />}
      {mode === 'system' && <MonitorSmallIcon className="h-5 w-5" />}
    </button>
  );
}

/* ── LogoutButton ── */

function LogoutButton({ onLogout, collapsed = false }: { onLogout: () => void; collapsed?: boolean }) {
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(false), 3000);
    return () => clearTimeout(t);
  }, [confirm]);

  if (collapsed) {
    return (
      <button
        onClick={() => { if (confirm) { onLogout(); } else { setConfirm(true); } }}
        className={`rounded-lg p-2 transition-colors ${
          confirm
            ? 'bg-red-100 text-red-600 dark:bg-red-950 dark:text-red-400'
            : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200'
        }`}
        title={confirm ? 'Nochmals klicken zum Abmelden' : 'Abmelden'}
      >
        <LogoutIcon className="h-5 w-5" />
      </button>
    );
  }

  return (
    <button
      onClick={() => { if (confirm) { onLogout(); } else { setConfirm(true); } }}
      className={`min-h-11 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors lg:min-h-0 ${
        confirm
          ? 'bg-red-100 text-red-600 dark:bg-red-950 dark:text-red-400'
          : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200'
      }`}
      title={confirm ? 'Nochmals klicken zum Abmelden' : 'Abmelden'}
    >
      {confirm ? 'Abmelden?' : <LogoutIcon className="h-5 w-5" />}
    </button>
  );
}

/* ── SortableProjectItem ── */

function SortableProjectItem({
  project,
  linkClasses,
  onClose,
}: {
  project: Project;
  linkClasses: (props: { isActive: boolean }) => string;
  onClose: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: project.id });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  return (
    <div ref={setNodeRef} style={style} {...attributes} {...listeners}>
      <NavLink
        to={`/projects/${project.id}`}
        className={linkClasses}
        onClick={onClose}
      >
        <ProjectIcon
          iconUrl={project.icon_url}
          iconEmoji={project.icon_emoji}
          color={project.color}
          size={16}
        />
        {project.name}
      </NavLink>
    </div>
  );
}

/* ── Icons ── */

function SearchIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
    </svg>
  );
}

function CockpitIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 0 0 6 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0 1 18 16.5h-2.25m-7.5 0h7.5m-7.5 0-1 3m8.5-3 1 3m0 0 .5 1.5m-.5-1.5h-9.5m0 0-.5 1.5m.75-9 3-3 2.148 2.148A12.061 12.061 0 0 1 16.5 7.605" />
    </svg>
  );
}

function AgendaIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h2.25A2.25 2.25 0 0 1 10.5 6v2.25a2.25 2.25 0 0 1-2.25 2.25H6a2.25 2.25 0 0 1-2.25-2.25V6ZM3.75 15.75A2.25 2.25 0 0 1 6 13.5h2.25a2.25 2.25 0 0 1 2.25 2.25V18a2.25 2.25 0 0 1-2.25 2.25H6A2.25 2.25 0 0 1 3.75 18v-2.25ZM13.5 6a2.25 2.25 0 0 1 2.25-2.25H18A2.25 2.25 0 0 1 20.25 6v2.25A2.25 2.25 0 0 1 18 10.5h-2.25a2.25 2.25 0 0 1-2.25-2.25V6ZM13.5 15.75a2.25 2.25 0 0 1 2.25-2.25H18a2.25 2.25 0 0 1 2.25 2.25V18A2.25 2.25 0 0 1 18 20.25h-2.25A2.25 2.25 0 0 1 13.5 18v-2.25Z" />
    </svg>
  );
}

function AgentIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" />
    </svg>
  );
}

function MailIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 0 1-2.25 2.25h-15a2.25 2.25 0 0 1-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25m19.5 0v.243a2.25 2.25 0 0 1-1.07 1.916l-7.5 4.615a2.25 2.25 0 0 1-2.36 0L3.32 8.91a2.25 2.25 0 0 1-1.07-1.916V6.75" />
    </svg>
  );
}


function LogoutIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0 0 13.5 3h-6a2.25 2.25 0 0 0-2.25 2.25v13.5A2.25 2.25 0 0 0 7.5 21h6a2.25 2.25 0 0 0 2.25-2.25V15m3 0 3-3m0 0-3-3m3 3H9" />
    </svg>
  );
}

function SettingsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.431.992a7.723 7.723 0 0 1 0 .255c-.007.378.138.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
    </svg>
  );
}

function MenuIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
    </svg>
  );
}

function CollapseIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5M12 17.25h8.25" />
    </svg>
  );
}

function SunSmallIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z" />
    </svg>
  );
}

function MoonSmallIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z" />
    </svg>
  );
}

function MonitorSmallIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 17.25v1.007a3 3 0 0 1-.879 2.122L7.5 21h9l-.621-.621A3 3 0 0 1 15 18.257V17.25m6-12V15a2.25 2.25 0 0 1-2.25 2.25H5.25A2.25 2.25 0 0 1 3 15V5.25A2.25 2.25 0 0 1 5.25 3h13.5A2.25 2.25 0 0 1 21 5.25Z" />
    </svg>
  );
}

function FinanceIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18 9 11.25l4.306 4.306a11.95 11.95 0 0 1 5.814-5.518l2.74-1.22m0 0-5.94-2.281m5.94 2.28-2.28 5.941" />
    </svg>
  );
}

function DebtorsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m-3-2.818.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
    </svg>
  );
}

function CreditorsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 0 0 2.25-2.25V6.75A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25v10.5A2.25 2.25 0 0 0 4.5 19.5Z" />
    </svg>
  );
}

function ChatIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 8.511c.884.284 1.5 1.128 1.5 2.097v4.286c0 1.136-.847 2.1-1.98 2.193-.34.027-.68.052-1.02.072v3.091l-3-3c-1.354 0-2.694-.055-4.02-.163a2.115 2.115 0 0 1-.825-.242m9.345-8.334a2.126 2.126 0 0 0-.476-.095 48.64 48.64 0 0 0-8.048 0c-1.131.094-1.976 1.057-1.976 2.192v4.286c0 .837.46 1.58 1.155 1.951m9.345-8.334V6.637c0-1.621-1.152-3.026-2.76-3.235A48.455 48.455 0 0 0 11.25 3c-2.115 0-4.198.137-6.24.402-1.608.209-2.76 1.614-2.76 3.235v6.226c0 1.621 1.152 3.026 2.76 3.235.577.075 1.157.14 1.74.194V21l4.155-4.155" />
    </svg>
  );
}

function LayersOpenIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 2.25 3.75 7.5 12 12.75l8.25-5.25L12 2.25Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M6.429 9.75 12 13.25l5.571-3.5" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M6.429 14.25 12 17.75l5.571-3.5" />
    </svg>
  );
}

function LayersClosedIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 2.25 3.75 7.5 12 12.75l8.25-5.25L12 2.25Z" />
    </svg>
  );
}

function SignaleIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.348 14.652a3.75 3.75 0 0 1 0-5.304m5.304 0a3.75 3.75 0 0 1 0 5.304m-7.425 2.121a6.75 6.75 0 0 1 0-9.546m9.546 0a6.75 6.75 0 0 1 0 9.546M5.106 18.894c-3.808-3.807-3.808-9.98 0-13.788m13.788 0c3.808 3.807 3.808 9.98 0 13.788M12 12h.008v.008H12V12Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" />
    </svg>
  );
}
