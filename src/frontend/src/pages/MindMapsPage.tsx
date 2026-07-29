import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Plus, Search, MoreHorizontal, Copy, Trash2, BrainCircuit,
  FolderPlus, Folder, FolderOpen, ChevronRight, ChevronDown,
  Eye, Pencil, X, Upload,
} from 'lucide-react';
import { api } from '../api/client';
import type { MindmapListItem, MindmapFolder } from '../types';

const VISIBILITY_DOTS: Record<string, string> = {
  private: 'bg-gray-400',
  project: 'bg-blue-500',
  shared: 'bg-emerald-500',
};

const VISIBILITY_LABELS: Record<string, string> = {
  private: 'Privat',
  project: 'Projekt',
  shared: 'Geteilt',
};

const CARD_GRADIENTS = [
  'from-indigo-500 to-purple-600',
  'from-emerald-500 to-teal-600',
  'from-amber-500 to-orange-600',
  'from-rose-500 to-pink-600',
  'from-cyan-500 to-blue-600',
  'from-violet-500 to-fuchsia-600',
];

function getGradient(index: number): string {
  return CARD_GRADIENTS[index % CARD_GRADIENTS.length];
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('de-CH', { day: '2-digit', month: 'short', year: 'numeric' });
}

export function MindMapsPage() {
  const navigate = useNavigate();
  const [maps, setMaps] = useState<MindmapListItem[]>([]);
  const [folders, setFolders] = useState<MindmapFolder[]>([]);
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [menuOpen, setMenuOpen] = useState<string | null>(null);
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(new Set());

  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [renamingFolder, setRenamingFolder] = useState<string | null>(null);
  const [renameFolderName, setRenameFolderName] = useState('');
  const [importing, setImporting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [m, f] = await Promise.all([
        api.get<MindmapListItem[]>(`/api/mindmaps${activeFolderId ? `?folder_id=${activeFolderId}` : ''}`),
        api.get<MindmapFolder[]>('/api/mindmaps/folders'),
      ]);
      setMaps(m);
      setFolders(f);
    } catch {
      // Fehler wird durch API-Client gehandelt
    } finally {
      setLoading(false);
    }
  }, [activeFolderId]);

  useEffect(() => { loadData(); }, [loadData]);

  const handleCreate = async () => {
    try {
      const res = await api.post<{ id: string }>('/api/mindmaps', {
        title: 'Neue Mind-Map',
        folder_id: activeFolderId,
      });
      navigate(`/mindmaps/${res.id}`);
    } catch {
      // Fehler wird durch API-Client gehandelt
    }
  };

  const handleDuplicate = async (id: string) => {
    try {
      const res = await api.post<{ id: string }>(`/api/mindmaps/${id}/duplicate`);
      navigate(`/mindmaps/${res.id}`);
    } catch {
      // Fehler wird durch API-Client gehandelt
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.delete(`/api/mindmaps/${id}`);
      setMaps(prev => prev.filter(m => m.id !== id));
    } catch {
      // Fehler wird durch API-Client gehandelt
    }
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImporting(true);
    try {
      const formData = new FormData();
      formData.append('file', file);
      if (activeFolderId) formData.append('folder_id', activeFolderId);
      const res = await api.upload<{ id: string }>('/api/mindmaps/import', formData);
      navigate(`/mindmaps/${res.id}`);
    } catch {
      // Fehler wird durch API-Client gehandelt
    } finally {
      setImporting(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleCreateFolder = async () => {
    if (!newFolderName.trim()) return;
    try {
      await api.post('/api/mindmaps/folders', { name: newFolderName.trim(), parent_id: activeFolderId });
      setNewFolderName('');
      setCreatingFolder(false);
      loadData();
    } catch {
      // Fehler wird durch API-Client gehandelt
    }
  };

  const handleRenameFolder = async (id: string) => {
    if (!renameFolderName.trim()) return;
    try {
      await api.patch(`/api/mindmaps/folders/${id}`, { name: renameFolderName.trim() });
      setRenamingFolder(null);
      loadData();
    } catch {
      // Fehler wird durch API-Client gehandelt
    }
  };

  const handleDeleteFolder = async (id: string) => {
    try {
      await api.delete(`/api/mindmaps/folders/${id}`);
      if (activeFolderId === id) setActiveFolderId(null);
      loadData();
    } catch {
      // Fehler wird durch API-Client gehandelt
    }
  };

  const toggleFolderExpand = (id: string) => {
    setExpandedFolders(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const filteredMaps = search
    ? maps.filter(m => m.title.toLowerCase().includes(search.toLowerCase()))
    : maps;

  const renderFolderTree = (parentId: string | null, depth: number) => {
    const children = folders.filter(f => f.parent_id === parentId);
    if (children.length === 0) return null;
    return (
      <div className={depth > 0 ? 'ml-4' : ''}>
        {children.map(folder => {
          const isActive = activeFolderId === folder.id;
          const isExpanded = expandedFolders.has(folder.id);
          const hasSubfolders = folders.some(f => f.parent_id === folder.id);
          return (
            <div key={folder.id}>
              <div
                className={`group flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm cursor-pointer transition-colors ${
                  isActive
                    ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300'
                    : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
                }`}
              >
                {hasSubfolders ? (
                  <button onClick={() => toggleFolderExpand(folder.id)} className="p-0.5">
                    {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  </button>
                ) : (
                  <span className="w-4" />
                )}
                <button
                  onClick={() => setActiveFolderId(isActive ? null : folder.id)}
                  className="flex items-center gap-1.5 flex-1 min-w-0 text-left"
                >
                  {isExpanded ? <FolderOpen size={14} /> : <Folder size={14} />}
                  {renamingFolder === folder.id ? (
                    <input
                      value={renameFolderName}
                      onChange={e => setRenameFolderName(e.target.value)}
                      onBlur={() => handleRenameFolder(folder.id)}
                      onKeyDown={e => { if (e.key === 'Enter') handleRenameFolder(folder.id); if (e.key === 'Escape') setRenamingFolder(null); }}
                      className="flex-1 bg-transparent outline-none text-sm border-b border-indigo-400"
                      autoFocus
                      onClick={e => e.stopPropagation()}
                    />
                  ) : (
                    <span className="truncate">{folder.name}</span>
                  )}
                </button>
                <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={(e) => { e.stopPropagation(); setRenamingFolder(folder.id); setRenameFolderName(folder.name); }}
                    className="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-700"
                    title="Umbenennen"
                  >
                    <Pencil size={10} />
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleDeleteFolder(folder.id); }}
                    className="p-0.5 rounded hover:bg-red-100 dark:hover:bg-red-900/20 text-red-500"
                    title="Löschen"
                  >
                    <Trash2 size={10} />
                  </button>
                </div>
              </div>
              {isExpanded && renderFolderTree(folder.id, depth + 1)}
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="flex h-full" data-testid="mindmaps-page">
      {/* Folder sidebar */}
      <div className="w-60 shrink-0 border-r border-gray-200 dark:border-gray-800 bg-gray-50/50 dark:bg-gray-950/50 overflow-y-auto hidden md:block">
        <div className="p-3">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">Ordner</h2>
            <button
              onClick={() => { setCreatingFolder(true); setNewFolderName(''); }}
              className="p-1 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-500 dark:text-gray-400"
              title="Neuer Ordner"
              data-testid="create-folder-button"
            >
              <FolderPlus size={14} />
            </button>
          </div>

          <button
            onClick={() => setActiveFolderId(null)}
            className={`w-full flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm transition-colors mb-1 ${
              activeFolderId === null
                ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300 font-medium'
                : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
            }`}
          >
            <BrainCircuit size={14} />
            Alle Mind-Maps
          </button>

          {creatingFolder && (
            <div className="flex items-center gap-1 mb-1">
              <input
                value={newFolderName}
                onChange={e => setNewFolderName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') handleCreateFolder(); if (e.key === 'Escape') setCreatingFolder(false); }}
                placeholder="Ordner-Name..."
                className="flex-1 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1 text-sm dark:text-white"
                autoFocus
              />
              <button onClick={() => setCreatingFolder(false)} className="p-1 text-gray-400"><X size={12} /></button>
            </div>
          )}

          {renderFolderTree(null, 0)}
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6">
          {/* Header */}
          <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4 mb-6">
            <div className="flex-1">
              {/* Titel liefert mobil der MobileHeader. */}
              <h1 className="hidden text-2xl font-bold text-gray-900 dark:text-white lg:flex lg:items-center lg:gap-2">
                <BrainCircuit size={28} className="text-indigo-600" />
                Mind-Maps
              </h1>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                Ideen visuell strukturieren und als Tasks übernehmen
              </p>
            </div>
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept=".mm"
                onChange={handleImport}
                className="hidden"
                data-testid="import-mindmap-input"
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={importing}
                className="flex items-center gap-2 rounded-xl border border-gray-300 dark:border-gray-600 px-4 py-2.5 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors disabled:opacity-50"
                data-testid="import-mindmap-button"
              >
                <Upload size={16} />
                {importing ? 'Importiere…' : 'Importieren'}
              </button>
              <button
                onClick={handleCreate}
                className="flex items-center gap-2 rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 transition-colors"
                data-testid="create-mindmap-button"
              >
                <Plus size={16} />
                Neue Mind-Map
              </button>
            </div>
          </div>

          {/* Search */}
          <div className="relative mb-6">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Mind-Maps suchen..."
              className="w-full sm:w-80 pl-9 pr-3 py-2 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-sm dark:text-white focus:outline-none focus:ring-2 focus:ring-indigo-400"
              data-testid="mindmap-search-input"
            />
          </div>

          {/* Loading */}
          {loading && (
            <div className="flex justify-center py-20">
              <div className="h-6 w-6 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
            </div>
          )}

          {/* Empty state */}
          {!loading && filteredMaps.length === 0 && (
            <div className="text-center py-20" data-testid="mindmaps-empty-state">
              <div className="mx-auto w-16 h-16 rounded-2xl bg-indigo-50 dark:bg-indigo-950 flex items-center justify-center mb-4">
                <BrainCircuit size={32} className="text-indigo-400" />
              </div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-1">
                {search ? 'Keine Ergebnisse' : 'Noch keine Mind-Maps'}
              </h2>
              <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">
                {search
                  ? `Keine Mind-Maps für "${search}" gefunden.`
                  : 'Erstelle deine erste Mind-Map, um Ideen visuell zu strukturieren.'}
              </p>
              {!search && (
                <button
                  onClick={handleCreate}
                  className="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-indigo-700 transition-colors"
                >
                  <Plus size={16} />
                  Erste Mind-Map erstellen
                </button>
              )}
            </div>
          )}

          {/* Grid */}
          {!loading && filteredMaps.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4" data-testid="mindmaps-grid">
              {filteredMaps.map((map, i) => (
                <div
                  key={map.id}
                  className="group relative rounded-2xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 overflow-hidden hover:shadow-lg hover:border-gray-300 dark:hover:border-gray-700 transition-all cursor-pointer"
                  onClick={() => navigate(`/mindmaps/${map.id}`)}
                  data-testid={`mindmap-card-${map.id}`}
                >
                  {/* Thumbnail */}
                  <div
                    className={`h-32 bg-gradient-to-br ${getGradient(i)} relative`}
                    style={map.background_color ? { background: map.background_color } : undefined}
                  >
                    {map.thumbnail_url && (
                      <img src={map.thumbnail_url} alt="" className="absolute inset-0 w-full h-full object-cover" />
                    )}
                    <div className="absolute inset-0 flex items-center justify-center opacity-20">
                      <BrainCircuit size={48} className="text-white" />
                    </div>

                    {/* Context menu button */}
                    <div className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        onClick={e => { e.stopPropagation(); setMenuOpen(menuOpen === map.id ? null : map.id); }}
                        className="flex h-10 w-10 items-center justify-center rounded-lg bg-black/20 text-white backdrop-blur-sm hover:bg-black/40 lg:h-auto lg:w-auto lg:p-1.5"
                        data-testid={`mindmap-menu-${map.id}`}
                      >
                        <MoreHorizontal size={14} />
                      </button>
                      {menuOpen === map.id && (
                        <div className="absolute right-0 top-full mt-1 w-40 rounded-xl bg-white dark:bg-gray-900 shadow-xl border border-gray-200 dark:border-gray-700 py-1 z-10">
                          <button
                            onClick={e => { e.stopPropagation(); handleDuplicate(map.id); setMenuOpen(null); }}
                            className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
                          >
                            <Copy size={12} /> Duplizieren
                          </button>
                          <button
                            onClick={e => { e.stopPropagation(); handleDelete(map.id); setMenuOpen(null); }}
                            className="w-full flex items-center gap-2 px-3 py-1.5 text-sm text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
                          >
                            <Trash2 size={12} /> Löschen
                          </button>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Info */}
                  <div className="p-3">
                    <div className="flex items-center gap-2 mb-1">
                      <span className={`w-2 h-2 rounded-full shrink-0 ${VISIBILITY_DOTS[map.visibility]}`} title={VISIBILITY_LABELS[map.visibility]} />
                      <h3 className="text-sm font-semibold text-gray-900 dark:text-white truncate">{map.title}</h3>
                    </div>
                    <div className="flex items-center justify-between text-xs text-gray-400">
                      {map.project_name && (
                        <span className="truncate">{map.project_name}</span>
                      )}
                      <span className={map.project_name ? '' : 'ml-auto'}>{formatDate(map.created_at)}</span>
                    </div>
                    {map.share_count > 0 && (
                      <div className="mt-1.5 flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                        <Eye size={10} />
                        {map.share_count} {map.share_count === 1 ? 'Share' : 'Shares'}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default MindMapsPage;
