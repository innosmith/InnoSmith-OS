import { useState, useEffect, useCallback } from 'react';
import { api } from '../api/client';

interface DriveItem {
  id: string;
  name: string;
  size: number;
  is_folder: boolean;
  web_url: string;
  last_modified: string;
  mime_type: string;
  path: string;
}

export interface ContextSource {
  type: 'onedrive_file' | 'onedrive_folder';
  item_id?: string;
  path?: string;
  name: string;
  recursive?: boolean;
  fileCount?: number;
}

interface OneDrivePickerProps {
  isOpen: boolean;
  onClose: () => void;
  onSelect: (sources: ContextSource[]) => void;
}

export function OneDrivePicker({ isOpen, onClose, onSelect }: OneDrivePickerProps) {
  const [currentPath, setCurrentPath] = useState('/');
  const [items, setItems] = useState<DriveItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<Map<string, ContextSource>>(new Map());
  const [selectFolderRecursive, setSelectFolderRecursive] = useState(false);

  const loadFolder = useCallback(async (path: string) => {
    setLoading(true);
    setError(null);
    setSearchQuery('');
    try {
      const data = await api.get<{ items: DriveItem[]; path: string }>(
        `/api/onedrive/list?path=${encodeURIComponent(path)}&top=100`
      );
      setItems(data.items);
      setCurrentPath(path);
    } catch (e) {
      setError((e as Error).message || 'Laden fehlgeschlagen');
    } finally {
      setLoading(false);
    }
  }, []);

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    setError(null);
    try {
      const data = await api.get<{ items: DriveItem[]; query: string }>(
        `/api/onedrive/search?q=${encodeURIComponent(searchQuery)}&top=30`
      );
      setItems(data.items);
    } catch (e) {
      setError((e as Error).message || 'Suche fehlgeschlagen');
    } finally {
      setSearching(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      loadFolder('/');
      setSelected(new Map());
    }
  }, [isOpen, loadFolder]);

  if (!isOpen) return null;

  const breadcrumbs = currentPath === '/'
    ? [{ name: 'OneDrive', path: '/' }]
    : [
        { name: 'OneDrive', path: '/' },
        ...currentPath.split('/').filter(Boolean).map((seg, i, arr) => ({
          name: seg,
          path: '/' + arr.slice(0, i + 1).join('/'),
        })),
      ];

  const toggleItem = (item: DriveItem) => {
    const key = item.id;
    const next = new Map(selected);
    if (next.has(key)) {
      next.delete(key);
    } else {
      if (item.is_folder) {
        const folderFileCount = items.filter(i => !i.is_folder).length;
        next.set(key, {
          type: 'onedrive_folder',
          path: `${currentPath === '/' ? '' : currentPath}/${item.name}`,
          name: item.name,
          recursive: selectFolderRecursive,
          fileCount: folderFileCount,
        });
      } else {
        next.set(key, {
          type: 'onedrive_file',
          item_id: item.id,
          name: item.name,
        });
      }
    }
    setSelected(next);
  };

  const handleConfirm = () => {
    onSelect(Array.from(selected.values()));
    onClose();
  };

  const formatSize = (bytes: number) => {
    if (bytes === 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const formatDate = (iso: string) => {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString('de-CH', { day: '2-digit', month: '2-digit', year: 'numeric' });
    } catch { return ''; }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 px-0 pt-[env(safe-area-inset-top,0px)] pb-[env(safe-area-inset-bottom,0px)] sm:items-center sm:px-4"
      onClick={onClose}
    >
      <div
        className="flex h-[min(70vh,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)-1rem))] w-full max-w-2xl flex-col rounded-t-xl border border-gray-200 bg-white shadow-xl sm:rounded-xl dark:border-gray-700 dark:bg-gray-800"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3 dark:border-gray-700">
          <h3 className="flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-white">
            <CloudIcon className="h-5 w-5 text-blue-500" />
            OneDrive-Dateien
          </h3>
          <button
            onClick={onClose}
            className="rounded p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
            aria-label="Schliessen"
          >
            <XIcon className="h-5 w-5" />
          </button>
        </div>

        {/* Suche */}
        <div className="border-b border-gray-200 px-4 py-2 dark:border-gray-700">
          <div className="flex gap-2">
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              placeholder="Dateien suchen..."
              className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-600 dark:bg-gray-700 dark:text-white"
            />
            <button
              onClick={handleSearch}
              disabled={searching}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              {searching ? '...' : 'Suchen'}
            </button>
          </div>
        </div>

        {/* Breadcrumb */}
        <div className="flex items-center gap-1 border-b border-gray-200 px-4 py-2 text-sm dark:border-gray-700">
          {breadcrumbs.map((bc, i) => (
            <span key={bc.path} className="flex items-center gap-1">
              {i > 0 && <span className="text-gray-400">/</span>}
              <button
                onClick={() => loadFolder(bc.path)}
                className="text-indigo-600 hover:underline dark:text-indigo-400"
              >
                {bc.name}
              </button>
            </span>
          ))}
        </div>

        {/* Dateiliste */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center justify-center py-12 text-gray-400">Laden...</div>
          ) : error ? (
            <div className="px-4 py-8 text-center text-sm text-red-500">{error}</div>
          ) : items.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-gray-400">Ordner ist leer</div>
          ) : (
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {items.map(item => (
                <div
                  key={item.id}
                  className={`flex items-center gap-3 px-4 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 ${
                    selected.has(item.id) ? 'bg-indigo-50 dark:bg-indigo-900/20' : ''
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(item.id)}
                    onChange={() => toggleItem(item)}
                    className="h-4 w-4 rounded text-indigo-600"
                  />
                  {item.is_folder ? (
                    <FolderIcon className="h-5 w-5 flex-shrink-0 text-amber-500" />
                  ) : (
                    <FileIcon className="h-5 w-5 flex-shrink-0 text-gray-400" />
                  )}
                  <div
                    className={`flex-1 min-w-0 ${item.is_folder ? 'cursor-pointer' : ''}`}
                    onClick={() => {
                      if (item.is_folder) {
                        loadFolder(`${currentPath === '/' ? '' : currentPath}/${item.name}`);
                      }
                    }}
                  >
                    <div className="truncate text-sm text-gray-900 dark:text-white">{item.name}</div>
                    <div className="text-xs text-gray-400">
                      {formatDate(item.last_modified)}
                      {!item.is_folder && ` · ${formatSize(item.size)}`}
                    </div>
                  </div>
                  {item.is_folder && (
                    <button
                      onClick={() => loadFolder(`${currentPath === '/' ? '' : currentPath}/${item.name}`)}
                      className="rounded p-1 text-gray-400 hover:text-indigo-500"
                      title="Ordner öffnen"
                    >
                      <ChevronRightIcon className="h-4 w-4" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Ordner-Optionen */}
        {Array.from(selected.values()).some(s => s.type === 'onedrive_folder') && (
          <div className="border-t border-gray-200 px-4 py-2 dark:border-gray-700">
            <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
              <input
                type="checkbox"
                checked={selectFolderRecursive}
                onChange={e => {
                  setSelectFolderRecursive(e.target.checked);
                  const next = new Map(selected);
                  next.forEach((v) => {
                    if (v.type === 'onedrive_folder') v.recursive = e.target.checked;
                  });
                  setSelected(next);
                }}
                className="h-4 w-4 rounded text-indigo-600"
              />
              Ordner rekursiv einschliessen (inkl. Unterordner)
            </label>
          </div>
        )}

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-gray-200 px-4 py-3 dark:border-gray-700">
          <div className="text-xs text-gray-400">
            {selected.size > 0
              ? `${selected.size} Element${selected.size > 1 ? 'e' : ''} ausgewählt`
              : 'Dateien oder Ordner auswählen'}
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              Abbrechen
            </button>
            <button
              onClick={handleConfirm}
              disabled={selected.size === 0}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              Auswählen
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function CloudIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15a4.5 4.5 0 0 0 4.5 4.5H18a3.75 3.75 0 0 0 1.332-7.257 3 3 0 0 0-3.758-3.848 5.25 5.25 0 0 0-10.233 2.33A4.502 4.502 0 0 0 2.25 15Z" /></svg>;
}

function FolderIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M2.25 12.75V12A2.25 2.25 0 0 1 4.5 9.75h15A2.25 2.25 0 0 1 21.75 12v.75m-8.69-6.44-2.12-2.12a1.5 1.5 0 0 0-1.061-.44H4.5A2.25 2.25 0 0 0 2.25 6v12a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9a2.25 2.25 0 0 0-2.25-2.25h-5.379a1.5 1.5 0 0 1-1.06-.44Z" /></svg>;
}

function FileIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" /></svg>;
}

function XIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>;
}

function ChevronRightIcon({ className }: { className?: string }) {
  return <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>;
}
