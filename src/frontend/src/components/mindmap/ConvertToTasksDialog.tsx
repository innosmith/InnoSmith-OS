import { useState, useEffect, useCallback } from 'react';
import { X, ListChecks } from 'lucide-react';
import { api } from '../../api/client';
import { useMindmapStore } from '../../stores/mindmapStore';

interface Project { id: string; name: string; }
interface Column { id: string; name: string; position: number; }

interface Props {
  mindmapId: string;
  open: boolean;
  onClose: () => void;
  onSaveFirst?: () => Promise<void>;
}

export function ConvertToTasksDialog({ mindmapId, open, onClose, onSaveFirst }: Props) {
  const { nodes, edges, selectedNodeIds } = useMindmapStore();
  const [projects, setProjects] = useState<Project[]>([]);
  const [columns, setColumns] = useState<Column[]>([]);
  const [selectedProject, setSelectedProject] = useState('');
  const [selectedColumn, setSelectedColumn] = useState('');
  const [leafOnly, setLeafOnly] = useState(true);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ count: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      api.get<Project[]>('/api/projects').then(setProjects).catch(() => {});
      setResult(null);
      setError(null);
    }
  }, [open]);

  useEffect(() => {
    if (selectedProject) {
      api.get<{ columns: Column[] }>(`/api/projects/${selectedProject}/board`)
        .then(data => {
          const cols = data.columns.filter(c => !('is_archive' in c && (c as any).is_archive));
          setColumns(cols);
          if (cols.length > 0) setSelectedColumn(cols[0].id);
        })
        .catch(() => {});
    }
  }, [selectedProject]);

  const eligibleNodes = selectedNodeIds.length > 0
    ? nodes.filter(n => selectedNodeIds.includes(n.id))
    : nodes;

  const leafNodeIds = new Set(
    eligibleNodes
      .filter(n => !edges.some(e => e.source === n.id))
      .map(n => n.id)
  );

  const nodesToConvert = leafOnly
    ? eligibleNodes.filter(n => leafNodeIds.has(n.id))
    : eligibleNodes.filter(n => n.id !== 'root');

  const handleConvert = useCallback(async () => {
    if (!selectedProject || !selectedColumn || nodesToConvert.length === 0) return;
    setLoading(true);
    setError(null);
    try {
      if (onSaveFirst) await onSaveFirst();
      const res = await api.post<{ count: number; created_task_ids: string[] }>(
        `/api/mindmaps/${mindmapId}/convert-to-tasks`,
        {
          node_ids: nodesToConvert.map(n => n.id),
          project_id: selectedProject,
          board_column_id: selectedColumn,
        }
      );
      if (res.count === 0) {
        setError('Es konnten keine Tasks erstellt werden. Speichere die Mind-Map und versuche es erneut.');
      } else {
        setResult({ count: res.count });
      }
    } catch (err: any) {
      setError(err?.message || 'Fehler beim Erstellen der Tasks. Bitte versuche es erneut.');
    } finally {
      setLoading(false);
    }
  }, [mindmapId, selectedProject, selectedColumn, nodesToConvert, onSaveFirst]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm modal-safe" onClick={onClose}>
      <div
        onClick={e => e.stopPropagation()}
        className="w-full max-w-lg rounded-2xl bg-white dark:bg-gray-900 shadow-2xl border border-gray-200 dark:border-gray-800"
        data-testid="convert-tasks-dialog"
      >
        <div className="flex items-center justify-between p-4 border-b border-gray-100 dark:border-gray-800">
          <div className="flex items-center gap-2">
            <ListChecks size={20} className="text-emerald-600" />
            <h2 className="text-lg font-semibold dark:text-white">Als Tasks übernehmen</h2>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800">
            <X size={18} />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {result ? (
            <div className="text-center py-6">
              <div className="text-4xl mb-2">✓</div>
              <p className="text-lg font-medium dark:text-white">{result.count} Tasks erstellt</p>
              <button
                onClick={onClose}
                className="mt-4 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700"
              >
                Schliessen
              </button>
            </div>
          ) : (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Projekt</label>
                <select
                  value={selectedProject}
                  onChange={e => setSelectedProject(e.target.value)}
                  className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-sm dark:text-white"
                  data-testid="convert-project-select"
                >
                  <option value="">Projekt wählen...</option>
                  {projects.map(p => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>

              {columns.length > 0 && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Kolonne</label>
                  <select
                    value={selectedColumn}
                    onChange={e => setSelectedColumn(e.target.value)}
                    className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-sm dark:text-white"
                    data-testid="convert-column-select"
                  >
                    {columns.map(c => (
                      <option key={c.id} value={c.id}>{c.name}</option>
                    ))}
                  </select>
                </div>
              )}

              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input
                  type="checkbox"
                  checked={leafOnly}
                  onChange={e => setLeafOnly(e.target.checked)}
                  className="rounded border-gray-300 dark:border-gray-600"
                />
                Nur Blatt-Knoten (unterste Ebene)
              </label>

              <div className="rounded-lg bg-gray-50 dark:bg-gray-800/50 p-3">
                <p className="text-sm font-medium text-gray-600 dark:text-gray-400 mb-2">
                  {nodesToConvert.length} Knoten werden als Tasks erstellt:
                </p>
                <ul className="text-sm text-gray-700 dark:text-gray-300 space-y-1 max-h-40 overflow-y-auto">
                  {nodesToConvert.slice(0, 20).map(n => (
                    <li key={n.id} className="flex items-center gap-1.5">
                      <span className="w-2 h-2 rounded-full" style={{ backgroundColor: (n.data as any)?.color || '#3B82F6' }} />
                      {(n.data as any)?.label || 'Ohne Titel'}
                    </li>
                  ))}
                  {nodesToConvert.length > 20 && (
                    <li className="text-gray-400">... und {nodesToConvert.length - 20} weitere</li>
                  )}
                </ul>
              </div>

              <button
                onClick={handleConvert}
                disabled={!selectedProject || !selectedColumn || nodesToConvert.length === 0 || loading}
                className="w-full py-2.5 rounded-lg bg-emerald-600 text-white font-medium hover:bg-emerald-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                data-testid="convert-submit-button"
              >
                {loading ? 'Erstelle Tasks...' : `${nodesToConvert.length} Tasks erstellen`}
              </button>

              {error && (
                <div className="rounded-lg border border-red-200 bg-red-50 dark:border-red-800 dark:bg-red-900/20 p-3 text-sm text-red-700 dark:text-red-400">
                  {error}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
