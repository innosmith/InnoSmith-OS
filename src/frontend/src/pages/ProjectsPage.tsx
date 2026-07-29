import { useState, useEffect, useCallback } from 'react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { ProjectIcon } from '../components/ProjectIcon';
import { ColorPicker, PRESET_COLORS } from '../components/ColorPicker';
import { BackgroundPicker } from '../components/BackgroundPicker';

interface ProjectMetrics {
  id: string;
  name: string;
  color: string;
  icon_url?: string | null;
  icon_emoji?: string | null;
  status: 'active' | 'archived';
  total_tasks: number;
  open_tasks: number;
  completed_tasks: number;
  overdue_tasks: number;
  progress_pct: number;
}

interface ProjectCreatePayload {
  name: string;
  color: string;
  description?: string;
}

export function ProjectsPage() {
  const navigate = useNavigate();
  const { isOwner } = useAuth();
  const { refreshSidebar } = useOutletContext<{ refreshSidebar: () => void }>();
  const [projects, setProjects] = useState<ProjectMetrics[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [formName, setFormName] = useState('');
  const [formColor, setFormColor] = useState(PRESET_COLORS[0]);
  const [formDescription, setFormDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [bgUrl, setBgUrl] = useState<string | null>(null);
  const [bgPickerOpen, setBgPickerOpen] = useState(false);
  const [editingColorId, setEditingColorId] = useState<string | null>(null);

  useEffect(() => {
    if (!editingColorId) return;
    const handleClick = () => setEditingColorId(null);
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [editingColorId]);

  useEffect(() => {
    api.get<{ projects_background_url?: string | null }>('/api/settings')
      .then((s) => setBgUrl(s.projects_background_url ?? null))
      .catch(() => {});
  }, []);

  const handleBgSelect = async (url: string | null, _type: 'gradient' | 'unsplash' | 'custom' | null) => {
    await api.patch('/api/settings', { projects_background_url: url });
    setBgUrl(url);
  };

  const fetchProjects = useCallback(async () => {
    try {
      const data = await api.get<ProjectMetrics[]>('/api/projects/overview/metrics');
      setProjects(data);
    } catch { /* handled by api client */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchProjects(); }, [fetchProjects]);

  useEffect(() => {
    if (loading || isOwner) return;
    const active = projects.filter((p) => p.status === 'active');
    if (active.length === 1) {
      navigate(`/projects/${active[0].id}`, { replace: true });
    }
  }, [loading, isOwner, projects, navigate]);

  const resetForm = () => {
    setFormName('');
    setFormColor(PRESET_COLORS[0]);
    setFormDescription('');
    setShowForm(false);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formName.trim()) return;
    setSubmitting(true);
    try {
      const payload: ProjectCreatePayload = {
        name: formName.trim(),
        color: formColor,
      };
      if (formDescription.trim()) {
        payload.description = formDescription.trim();
      }
      await api.post('/api/projects', payload);
      resetForm();
      fetchProjects();
      refreshSidebar();
    } catch { /* handled by api client */ }
    finally { setSubmitting(false); }
  };

  const handleArchive = async (e: React.MouseEvent, projectId: string) => {
    e.stopPropagation();
    try {
      await api.patch(`/api/projects/${projectId}`, { status: 'archived' });
      fetchProjects();
    } catch { /* handled by api client */ }
  };

  const handleReactivate = async (e: React.MouseEvent, projectId: string) => {
    e.stopPropagation();
    try {
      await api.patch(`/api/projects/${projectId}`, { status: 'active' });
      fetchProjects();
    } catch { /* handled by api client */ }
  };

  const handleDelete = async (e: React.MouseEvent, projectId: string) => {
    e.stopPropagation();
    if (!confirm('Projekt und alle Tasks unwiderruflich löschen?')) return;
    try {
      await api.delete(`/api/projects/${projectId}`);
      fetchProjects();
    } catch { /* handled by api client */ }
  };

  const handleColorChange = async (projectId: string, color: string) => {
    try {
      await api.patch(`/api/projects/${projectId}`, { color });
      setProjects((prev) => prev.map((p) => p.id === projectId ? { ...p, color } : p));
      refreshSidebar();
    } catch { /* handled by api client */ }
  };

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  const activeProjects = projects.filter((p) => p.status === 'active');
  const archivedProjects = projects.filter((p) => p.status === 'archived');
  const displayedProjects = showArchived ? archivedProjects : activeProjects;

  const hasBg = !!bgUrl;
  const isGradient = bgUrl?.startsWith('gradient:') ?? false;
  const bgStyle = isGradient
    ? { background: bgUrl!.slice('gradient:'.length) }
    : hasBg
      ? { backgroundImage: `url(${bgUrl})`, backgroundSize: 'cover' as const, backgroundPosition: 'center' }
      : undefined;

  const textPrimary = hasBg ? 'text-white' : 'text-gray-900 dark:text-white';
  const textSecondary = hasBg ? 'text-white/70' : 'text-gray-500 dark:text-gray-400';

  return (
    <div className="relative flex h-full flex-col" style={bgStyle}>
      {hasBg && !isGradient && <div className="pointer-events-none absolute inset-0 bg-black/10 dark:bg-black/30" />}
      {isGradient && <div className="pointer-events-none absolute inset-0 bg-black/5 dark:bg-black/20" />}

      <div className={`relative z-10 border-b px-4 py-4 sm:px-6 ${hasBg ? 'border-white/10 bg-black/20 backdrop-blur-sm' : 'border-gray-200 dark:border-gray-800'}`}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start justify-between gap-2">
            <div>
              <h1 className={`hidden text-xl font-bold lg:block ${textPrimary}`}>Projekte</h1>
              {isOwner && (
                <p className={`text-sm lg:mt-0.5 ${textSecondary}`}>
                  {activeProjects.length} aktiv{archivedProjects.length > 0 && ` · ${archivedProjects.length} archiviert`}
                </p>
              )}
            </div>
            {isOwner && (
              <button
                onClick={() => setBgPickerOpen(true)}
                className={`shrink-0 rounded-lg p-2 transition-colors lg:hidden ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`}
                title="Hintergrund ändern"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0 0 22.5 18.75V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
                </svg>
              </button>
            )}
          </div>
          {isOwner && (
            <div className="flex flex-wrap items-center gap-2 sm:gap-3">
              <button
                onClick={() => setBgPickerOpen(true)}
                className={`hidden rounded-lg p-2 transition-colors lg:inline-flex ${hasBg ? 'text-white/70 hover:bg-white/10 hover:text-white' : 'text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300'}`}
                title="Hintergrund ändern"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="m2.25 15.75 5.159-5.159a2.25 2.25 0 0 1 3.182 0l5.159 5.159m-1.5-1.5 1.409-1.409a2.25 2.25 0 0 1 3.182 0l2.909 2.909M3.75 21h16.5A2.25 2.25 0 0 0 22.5 18.75V5.25A2.25 2.25 0 0 0 20.25 3H3.75A2.25 2.25 0 0 0 1.5 5.25v13.5A2.25 2.25 0 0 0 3.75 21Z" />
                </svg>
              </button>
              <div className={`flex rounded-lg border ${hasBg ? 'border-white/20' : 'border-gray-200 dark:border-gray-700'}`}>
                <button
                  onClick={() => setShowArchived(false)}
                  className={`min-h-11 px-3 py-1.5 text-xs font-medium transition-colors lg:min-h-0 ${!showArchived ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'}`}
                >
                  Aktiv ({activeProjects.length})
                </button>
                <button
                  onClick={() => setShowArchived(true)}
                  className={`min-h-11 px-3 py-1.5 text-xs font-medium transition-colors lg:min-h-0 ${showArchived ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'}`}
                >
                  Archiv ({archivedProjects.length})
                </button>
              </div>
              <button
                onClick={() => setShowForm((v) => !v)}
                className="rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 sm:px-4"
              >
                {showForm ? 'Abbrechen' : (
                  <>
                    <span className="sm:hidden">+ Projekt</span>
                    <span className="hidden sm:inline">Neues Projekt</span>
                  </>
                )}
              </button>
            </div>
          )}
        </div>
      </div>

      {isOwner && showForm && (
        <div className={`relative z-10 border-b px-4 py-4 sm:px-6 ${hasBg ? 'border-white/10 bg-black/20 backdrop-blur-sm' : 'border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900/50'}`}>
          <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-4">
            <div className="min-w-[200px] flex-1">
              <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
                Name
              </label>
              <input
                type="text"
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                placeholder="Projektname"
                autoFocus
                className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white dark:placeholder-gray-500"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
                Farbe
              </label>
              <ColorPicker value={formColor} onChange={setFormColor} />
            </div>
            <div className="min-w-[200px] flex-1">
              <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
                Beschreibung
              </label>
              <input
                type="text"
                value={formDescription}
                onChange={(e) => setFormDescription(e.target.value)}
                placeholder="Optional"
                className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white dark:placeholder-gray-500"
              />
            </div>
            <button
              type="submit"
              disabled={submitting || !formName.trim()}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? 'Erstellen…' : 'Erstellen'}
            </button>
          </form>
        </div>
      )}

      <div className="relative z-10 flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
        {displayedProjects.length === 0 ? (
          <div className="flex h-48 items-center justify-center text-gray-400 dark:text-gray-600">
            <p>{showArchived ? 'Kein archiviertes Projekt vorhanden' : 'Noch keine Projekte vorhanden'}</p>
          </div>
        ) : (
          <>
            {/* Mobile: Card-Liste */}
            <div className="flex flex-col gap-3 md:hidden">
              {displayedProjects.map((project) => (
                <div
                  key={project.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => navigate(`/projects/${project.id}`)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      navigate(`/projects/${project.id}`);
                    }
                  }}
                  className="w-full cursor-pointer rounded-xl border border-gray-200 bg-white p-3.5 text-left shadow-sm transition-colors active:bg-gray-50 dark:border-gray-800 dark:bg-gray-900 dark:active:bg-gray-800/60"
                >
                  <div className="flex items-start gap-3">
                    <div className="relative shrink-0" onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setEditingColorId(editingColorId === project.id ? null : project.id);
                        }}
                        onMouseDown={(e) => e.stopPropagation()}
                        className="flex h-10 w-10 items-center justify-center rounded-md transition-colors hover:bg-gray-100 lg:h-auto lg:w-auto lg:p-0.5 dark:hover:bg-gray-800"
                        title="Farbe ändern"
                      >
                        <ProjectIcon
                          iconUrl={project.icon_url}
                          iconEmoji={project.icon_emoji}
                          color={project.color}
                          size={24}
                        />
                      </button>
                      {editingColorId === project.id && (
                        <div
                          className="absolute left-0 top-8 z-50 rounded-xl border border-gray-200 bg-white p-3 shadow-xl dark:border-gray-700 dark:bg-gray-900"
                          onMouseDown={(e) => e.stopPropagation()}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <ColorPicker
                            value={project.color}
                            onChange={(color) => {
                              handleColorChange(project.id, color);
                              setEditingColorId(null);
                            }}
                          />
                        </div>
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-semibold text-gray-900 dark:text-white">
                          {project.name}
                        </span>
                        <span
                          className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                            project.status === 'active'
                              ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                              : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
                          }`}
                        >
                          {project.status === 'active' ? 'Aktiv' : 'Archiv'}
                        </span>
                      </div>
                      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs tabular-nums text-gray-600 dark:text-gray-300">
                        <span>Offen {project.open_tasks}</span>
                        <span>Erledigt {project.completed_tasks}</span>
                        <span className={project.overdue_tasks > 0 ? 'font-medium text-red-600 dark:text-red-400' : ''}>
                          Überfällig {project.overdue_tasks}
                        </span>
                      </div>
                      <div className="mt-2.5 flex items-center gap-2.5">
                        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                          <div
                            className="h-full rounded-full bg-indigo-500 transition-all"
                            style={{ width: `${Math.min(project.progress_pct, 100)}%` }}
                          />
                        </div>
                        <span className="w-9 text-right text-[11px] tabular-nums text-gray-500 dark:text-gray-400">
                          {Math.round(project.progress_pct)}%
                        </span>
                      </div>
                      {isOwner && (
                        <div className="mt-2.5 flex justify-end gap-1">
                          {project.status === 'active' ? (
                            <button
                              type="button"
                              onClick={(e) => handleArchive(e, project.id)}
                              className="min-h-11 rounded-lg px-2.5 py-1 text-xs font-medium text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 lg:min-h-0 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                            >
                              Archivieren
                            </button>
                          ) : (
                            <>
                              <button
                                type="button"
                                onClick={(e) => handleReactivate(e, project.id)}
                                className="rounded-lg px-2.5 py-1 text-xs font-medium text-indigo-600 transition-colors hover:bg-indigo-50 dark:text-indigo-400 dark:hover:bg-indigo-950"
                              >
                                Reaktivieren
                              </button>
                              <button
                                type="button"
                                onClick={(e) => handleDelete(e, project.id)}
                                className="rounded-lg px-2.5 py-1 text-xs font-medium text-red-500 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                              >
                                Löschen
                              </button>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>

            {/* Desktop: Tabelle */}
            <div className="hidden overflow-hidden rounded-xl border border-gray-200 md:block dark:border-gray-800">
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="border-b border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900/60">
                    <th className="px-4 py-3 font-medium text-gray-500 dark:text-gray-400">Projekt</th>
                    <th className="px-4 py-3 font-medium text-gray-500 dark:text-gray-400">Status</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-500 dark:text-gray-400">Offen</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-500 dark:text-gray-400">Erledigt</th>
                    <th className="px-4 py-3 text-right font-medium text-gray-500 dark:text-gray-400">Überfällig</th>
                    <th className="w-48 px-4 py-3 font-medium text-gray-500 dark:text-gray-400">Fortschritt</th>
                    {isOwner && <th className="px-4 py-3 text-right font-medium text-gray-500 dark:text-gray-400">Aktionen</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
                  {displayedProjects.map((project) => (
                    <tr
                      key={project.id}
                      onClick={() => navigate(`/projects/${project.id}`)}
                      className="cursor-pointer bg-white transition-colors hover:bg-gray-50 dark:bg-gray-900 dark:hover:bg-gray-800/60"
                    >
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2.5">
                          <div className="relative">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setEditingColorId(editingColorId === project.id ? null : project.id);
                              }}
                              onMouseDown={(e) => e.stopPropagation()}
                              className="flex h-10 w-10 items-center justify-center rounded-md transition-colors hover:bg-gray-100 lg:h-auto lg:w-auto lg:p-0.5 dark:hover:bg-gray-800"
                              title="Farbe ändern"
                            >
                              <ProjectIcon
                                iconUrl={project.icon_url}
                                iconEmoji={project.icon_emoji}
                                color={project.color}
                                size={20}
                              />
                            </button>
                            {editingColorId === project.id && (
                              <div
                                className="absolute left-0 top-8 z-50 rounded-xl border border-gray-200 bg-white p-3 shadow-xl dark:border-gray-700 dark:bg-gray-900"
                                onMouseDown={(e) => e.stopPropagation()}
                                onClick={(e) => e.stopPropagation()}
                              >
                                <ColorPicker
                                  value={project.color}
                                  onChange={(color) => {
                                    handleColorChange(project.id, color);
                                    setEditingColorId(null);
                                  }}
                                />
                              </div>
                            )}
                          </div>
                          <span className="font-medium text-gray-900 dark:text-white">
                            {project.name}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${
                            project.status === 'active'
                              ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
                              : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
                          }`}
                        >
                          {project.status === 'active' ? 'Aktiv' : 'Archiviert'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300">
                        {project.open_tasks}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300">
                        {project.completed_tasks}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <span
                          className={`tabular-nums ${
                            project.overdue_tasks > 0
                              ? 'font-medium text-red-600 dark:text-red-400'
                              : 'text-gray-700 dark:text-gray-300'
                          }`}
                        >
                          {project.overdue_tasks}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2.5">
                          <div className="h-2 flex-1 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                            <div
                              className="h-full rounded-full bg-indigo-500 transition-all"
                              style={{ width: `${Math.min(project.progress_pct, 100)}%` }}
                            />
                          </div>
                          <span className="w-10 text-right text-xs tabular-nums text-gray-500 dark:text-gray-400">
                            {Math.round(project.progress_pct)}%
                          </span>
                        </div>
                      </td>
                      {isOwner && (
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            {project.status === 'active' ? (
                              <button
                                onClick={(e) => handleArchive(e, project.id)}
                                className="min-h-11 rounded-lg px-2.5 py-1 text-xs font-medium text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 lg:min-h-0 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                              >
                                Archivieren
                              </button>
                            ) : (
                              <>
                                <button
                                  onClick={(e) => handleReactivate(e, project.id)}
                                  className="rounded-lg px-2.5 py-1 text-xs font-medium text-indigo-600 transition-colors hover:bg-indigo-50 dark:text-indigo-400 dark:hover:bg-indigo-950"
                                >
                                  Reaktivieren
                                </button>
                                <button
                                  onClick={(e) => handleDelete(e, project.id)}
                                  className="rounded-lg px-2.5 py-1 text-xs font-medium text-red-500 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                                >
                                  Löschen
                                </button>
                              </>
                            )}
                          </div>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      <BackgroundPicker
        isOpen={bgPickerOpen}
        currentUrl={bgUrl}
        onSelect={handleBgSelect}
        onClose={() => setBgPickerOpen(false)}
      />
    </div>
  );
}
