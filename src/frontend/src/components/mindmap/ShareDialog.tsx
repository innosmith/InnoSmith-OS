import { useState, useEffect, useCallback } from 'react';
import { X, Share2, Copy, Trash2, Check, Eye, Pencil, ClipboardCopy, AlertCircle } from 'lucide-react';
import { api } from '../../api/client';
import type { MindmapShare } from '../../types';

interface Props {
  mindmapId: string;
  title: string;
  open: boolean;
  onClose: () => void;
}

export function ShareDialog({ mindmapId, title, open, onClose }: Props) {
  const [shares, setShares] = useState<MindmapShare[]>([]);
  const [password, setPassword] = useState('');
  const [permission, setPermission] = useState<'view' | 'edit'>('view');
  const [label, setLabel] = useState('');
  const [copied, setCopied] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [justCreated, setJustCreated] = useState<{ token: string; password: string; permission: string } | null>(null);
  const [allCopied, setAllCopied] = useState(false);
  const [creating, setCreating] = useState(false);

  const loadShares = useCallback(() => {
    api.get<MindmapShare[]>(`/api/mindmaps/${mindmapId}/shares`).then(setShares).catch(() => {});
  }, [mindmapId]);

  useEffect(() => {
    if (open) {
      loadShares();
      setJustCreated(null);
      setError(null);
      setPassword('');
      setLabel('');
      setPermission('view');
    }
  }, [open, loadShares]);

  const handleCreate = async () => {
    if (!password || creating) return;
    setError(null);
    setCreating(true);
    try {
      const res = await api.post<MindmapShare>(`/api/mindmaps/${mindmapId}/shares`, {
        password,
        permission,
        label: label || null,
      });
      setJustCreated({ token: res.token, password, permission });
      setPassword('');
      setLabel('');
      setPermission('view');
      loadShares();
    } catch (err: any) {
      setError(err?.message || 'Fehler beim Erstellen des Share-Links. Bitte versuche es erneut.');
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (shareId: string) => {
    try {
      await api.delete(`/api/mindmaps/shares/${shareId}`);
      loadShares();
    } catch {
      setError('Fehler beim Löschen des Share-Links.');
    }
  };

  const copyLink = (token: string) => {
    navigator.clipboard.writeText(`${window.location.origin}/shared/${token}`);
    setCopied(token);
    setTimeout(() => setCopied(null), 2000);
  };

  const copyAll = () => {
    if (!justCreated) return;
    const permLabel = justCreated.permission === 'view' ? 'Nur ansehen' : 'Bearbeiten';
    const text = [
      `Mind-Map: ${title}`,
      `Link: ${window.location.origin}/shared/${justCreated.token}`,
      `Passwort: ${justCreated.password}`,
      `Berechtigung: ${permLabel}`,
    ].join('\n');
    navigator.clipboard.writeText(text);
    setAllCopied(true);
    setTimeout(() => setAllCopied(false), 2500);
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm modal-safe" onClick={onClose}>
      <div onClick={e => e.stopPropagation()} className="w-full max-w-lg rounded-2xl bg-white dark:bg-gray-900 shadow-2xl border border-gray-200 dark:border-gray-800" data-testid="share-dialog">
        <div className="flex items-center justify-between p-4 border-b border-gray-100 dark:border-gray-800">
          <div className="flex items-center gap-2">
            <Share2 size={20} className="text-indigo-600" />
            <h2 className="text-lg font-semibold dark:text-white">Mind-Map teilen</h2>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-800"><X size={18} /></button>
        </div>

        <div className="p-4 space-y-4">
          {error && (
            <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 dark:border-red-800 dark:bg-red-900/20 p-3 text-sm text-red-700 dark:text-red-400">
              <AlertCircle size={16} className="shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {justCreated && (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-900/20 p-4 space-y-3" data-testid="share-just-created">
              <div className="flex items-center gap-2 text-sm font-semibold text-emerald-700 dark:text-emerald-400">
                <Check size={16} />
                Share-Link erstellt
              </div>
              <div className="space-y-1.5 text-sm text-gray-700 dark:text-gray-300">
                <div>
                  <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Link</span>
                  <p className="font-mono text-xs break-all mt-0.5">{window.location.origin}/shared/{justCreated.token}</p>
                </div>
                <div>
                  <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Passwort</span>
                  <p className="font-mono text-sm mt-0.5">{justCreated.password}</p>
                </div>
                <div>
                  <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Berechtigung</span>
                  <p className="text-xs mt-0.5">{justCreated.permission === 'view' ? 'Nur ansehen' : 'Bearbeiten'}</p>
                </div>
              </div>
              <button
                onClick={copyAll}
                className={`w-full flex items-center justify-center gap-2 py-2 rounded-lg text-sm font-medium transition-colors ${
                  allCopied
                    ? 'bg-emerald-600 text-white'
                    : 'bg-emerald-100 text-emerald-800 hover:bg-emerald-200 dark:bg-emerald-800/40 dark:text-emerald-300 dark:hover:bg-emerald-800/60'
                }`}
                data-testid="share-copy-all"
              >
                {allCopied ? (
                  <><Check size={14} /> Kopiert</>
                ) : (
                  <><ClipboardCopy size={14} /> Alles kopieren (für E-Mail)</>
                )}
              </button>
            </div>
          )}

          <div className="space-y-3 p-3 rounded-lg border border-gray-200 dark:border-gray-700">
            <input
              value={label}
              onChange={e => setLabel(e.target.value)}
              placeholder="Bezeichnung (optional, z.B. 'Für Kunde X')"
              className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-sm dark:text-white"
            />
            <input
              type="text"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="Passwort eingeben"
              className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-sm dark:text-white"
              autoComplete="off"
              data-testid="share-password-input"
            />
            <div className="flex gap-2">
              <button
                onClick={() => setPermission('view')}
                className={`flex-1 py-1.5 rounded-lg text-sm border ${permission === 'view' ? 'border-indigo-500 bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400'}`}
              >
                Nur ansehen
              </button>
              <button
                onClick={() => setPermission('edit')}
                className={`flex-1 py-1.5 rounded-lg text-sm border ${permission === 'edit' ? 'border-indigo-500 bg-indigo-50 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400'}`}
              >
                Bearbeiten
              </button>
            </div>
            <button
              onClick={handleCreate}
              disabled={!password || creating}
              className="w-full py-2 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-700 disabled:opacity-50 transition-colors"
              data-testid="share-create-button"
            >
              {creating ? 'Wird erstellt...' : 'Link erstellen'}
            </button>
          </div>

          {shares.length > 0 && (
            <div className="space-y-2">
              <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">Bestehende Links</h3>
              {shares.map(s => (
                <div key={s.id} className="flex items-center gap-2 p-3 rounded-lg bg-gray-50 dark:bg-gray-800/50">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 text-sm font-medium dark:text-white">
                      {s.permission === 'view' ? <Eye size={12} /> : <Pencil size={12} />}
                      {s.label || 'Share-Link'}
                    </div>
                    <p className="text-xs text-gray-400 truncate mt-0.5">
                      {window.location.origin}/shared/{s.token}
                    </p>
                  </div>
                  <button onClick={() => copyLink(s.token)} className="p-1.5 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700" title="Link kopieren">
                    {copied === s.token ? <Check size={14} className="text-emerald-500" /> : <Copy size={14} />}
                  </button>
                  <button onClick={() => handleDelete(s.id)} className="p-1.5 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 text-red-500" title="Löschen">
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
