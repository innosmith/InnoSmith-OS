import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../api/client';

const GRADIENTS = [
  { id: 'none', label: 'Kein', css: '', thumb: 'bg-gray-100 dark:bg-gray-800' },
  { id: 'indigo-night', label: 'Indigo Night', css: 'linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%)', thumb: 'bg-gradient-to-br from-[#0f0c29] via-[#302b63] to-[#24243e]' },
  { id: 'ocean-blue', label: 'Ocean Blue', css: 'linear-gradient(135deg, #2E3192 0%, #1BFFFF 100%)', thumb: 'bg-gradient-to-br from-[#2E3192] to-[#1BFFFF]' },
  { id: 'sunset', label: 'Sunset', css: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)', thumb: 'bg-gradient-to-br from-[#f093fb] to-[#f5576c]' },
  { id: 'forest', label: 'Forest', css: 'linear-gradient(135deg, #11998e 0%, #38ef7d 100%)', thumb: 'bg-gradient-to-br from-[#11998e] to-[#38ef7d]' },
  { id: 'aurora', label: 'Aurora', css: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)', thumb: 'bg-gradient-to-br from-[#667eea] to-[#764ba2]' },
  { id: 'warm-flame', label: 'Warm Flame', css: 'linear-gradient(135deg, #ff9a9e 0%, #fecfef 99%, #fecfef 100%)', thumb: 'bg-gradient-to-br from-[#ff9a9e] to-[#fecfef]' },
  { id: 'deep-space', label: 'Deep Space', css: 'linear-gradient(135deg, #000000 0%, #434343 100%)', thumb: 'bg-gradient-to-br from-black to-[#434343]' },
  { id: 'fresh-mint', label: 'Fresh Mint', css: 'linear-gradient(135deg, #00b09b 0%, #96c93d 100%)', thumb: 'bg-gradient-to-br from-[#00b09b] to-[#96c93d]' },
  { id: 'cosmic', label: 'Cosmic', css: 'linear-gradient(135deg, #ff00cc 0%, #333399 100%)', thumb: 'bg-gradient-to-br from-[#ff00cc] to-[#333399]' },
  { id: 'arctic', label: 'Arctic', css: 'linear-gradient(135deg, #E0EAFC 0%, #CFDEF3 100%)', thumb: 'bg-gradient-to-br from-[#E0EAFC] to-[#CFDEF3]' },
  { id: 'ember', label: 'Ember', css: 'linear-gradient(135deg, #f12711 0%, #f5af19 100%)', thumb: 'bg-gradient-to-br from-[#f12711] to-[#f5af19]' },
];

interface UnsplashPhoto {
  id: string;
  thumb: string;
  regular: string;
  hq: string;
  author: string;
  author_url: string;
  description: string;
}

interface BackgroundPickerProps {
  isOpen: boolean;
  onClose: () => void;
  currentUrl: string | null;
  onSelect: (url: string | null, type: 'gradient' | 'unsplash' | 'custom' | null) => void;
}

export function BackgroundPicker({ isOpen, onClose, currentUrl, onSelect }: BackgroundPickerProps) {
  const [tab, setTab] = useState<'gradients' | 'unsplash' | 'url'>('gradients');
  const [query, setQuery] = useState('');
  const [photos, setPhotos] = useState<UnsplashPhoto[]>([]);
  const [unsplashAvailable, setUnsplashAvailable] = useState(true);
  const [searching, setSearching] = useState(false);
  const [customUrl, setCustomUrl] = useState('');
  const backdropRef = useRef<HTMLDivElement>(null);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const searchUnsplash = useCallback(async (q: string, pageNum: number, append: boolean) => {
    if (!q.trim()) { setPhotos([]); setHasMore(false); return; }
    if (pageNum === 1) setSearching(true); else setLoadingMore(true);
    try {
      const data = await api.get<{ results: UnsplashPhoto[]; total: number }>(`/api/unsplash/search?q=${encodeURIComponent(q)}&page=${pageNum}&per_page=18`);
      if (append) {
        setPhotos(prev => [...prev, ...data.results]);
      } else {
        setPhotos(data.results);
      }
      setHasMore(pageNum * 18 < data.total);
    } catch {
      setUnsplashAvailable(false);
    } finally {
      setSearching(false);
      setLoadingMore(false);
    }
  }, []);

  useEffect(() => {
    if (tab !== 'unsplash' || !query.trim()) return;
    clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => { setPage(1); searchUnsplash(query, 1, false); }, 400);
    return () => clearTimeout(searchTimer.current);
  }, [query, tab, searchUnsplash]);

  const handleLoadMore = useCallback(() => {
    if (loadingMore || !hasMore || !query.trim()) return;
    const nextPage = page + 1;
    setPage(nextPage);
    searchUnsplash(query, nextPage, true);
  }, [loadingMore, hasMore, query, page, searchUnsplash]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || tab !== 'unsplash') return;
    const onScroll = () => {
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 200) {
        handleLoadMore();
      }
    };
    el.addEventListener('scroll', onScroll);
    return () => el.removeEventListener('scroll', onScroll);
  }, [tab, handleLoadMore]);

  useEffect(() => {
    if (!isOpen) { setQuery(''); setPhotos([]); setCustomUrl(''); setPage(1); setHasMore(false); }
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div
      ref={backdropRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm modal-safe"
      onClick={(e) => { if (e.target === backdropRef.current) onClose(); }}
    >
      <div className="w-full max-w-2xl rounded-2xl border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900">
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Hintergrundbild wählen</h2>
          <button onClick={onClose} className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300">
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex gap-1 border-b border-gray-200 px-6 dark:border-gray-700">
          {(['gradients', 'unsplash', 'url'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-3 text-sm font-medium transition-colors ${
                tab === t
                  ? 'border-b-2 border-indigo-500 text-indigo-600 dark:text-indigo-400'
                  : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
              }`}
            >
              {{ gradients: 'Gradienten', unsplash: 'Unsplash', url: 'Eigene URL' }[t]}
            </button>
          ))}
        </div>

        <div ref={scrollRef} className="max-h-[60dvh] overflow-y-auto p-6">
          {tab === 'gradients' && (
            <div className="grid grid-cols-4 gap-3">
              {GRADIENTS.map((g) => (
                <button
                  key={g.id}
                  onClick={() => {
                    onSelect(g.id === 'none' ? null : `gradient:${g.css}`, g.id === 'none' ? null : 'gradient');
                    onClose();
                  }}
                  className={`group relative aspect-video overflow-hidden rounded-xl border-2 transition-all hover:scale-105 ${
                    currentUrl === `gradient:${g.css}` || (g.id === 'none' && !currentUrl)
                      ? 'border-indigo-500 ring-2 ring-indigo-500/30'
                      : 'border-transparent hover:border-gray-300 dark:hover:border-gray-600'
                  }`}
                >
                  <div className={`absolute inset-0 ${g.thumb}`} style={g.css ? { background: g.css } : undefined} />
                  <span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/60 to-transparent px-2 py-1.5 text-[10px] font-medium text-white">
                    {g.label}
                  </span>
                </button>
              ))}
            </div>
          )}

          {tab === 'unsplash' && (
            <div>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Suche auf Unsplash... (z.B. mountains, office, nature)"
                autoFocus
                className="mb-4 w-full rounded-lg border border-gray-200 bg-transparent px-4 py-2.5 text-sm text-gray-900 outline-none placeholder:text-gray-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-400/20 dark:border-gray-700 dark:text-white"
              />
              {!unsplashAvailable && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-300">
                  Unsplash ist nicht konfiguriert. Setze <code className="rounded bg-amber-100 px-1 dark:bg-amber-900">TP_UNSPLASH_ACCESS_KEY</code> in deinen Umgebungsvariablen.
                </div>
              )}
              {searching && (
                <div className="flex justify-center py-8">
                  <div className="h-6 w-6 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
                </div>
              )}
              <div className="grid grid-cols-3 gap-3">
                {photos.map((photo) => (
                  <button
                    key={photo.id}
                    onClick={() => {
                      onSelect(photo.hq || photo.regular, 'unsplash');
                      onClose();
                    }}
                    className="group relative aspect-video overflow-hidden rounded-xl border-2 border-transparent transition-all hover:scale-105 hover:border-indigo-400"
                  >
                    <img src={photo.thumb} alt={photo.description} className="h-full w-full object-cover" loading="lazy" />
                    <span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/70 to-transparent px-2 py-1.5 text-[10px] text-white opacity-0 transition-opacity group-hover:opacity-100">
                      📷 {photo.author}
                    </span>
                  </button>
                ))}
              </div>
              {loadingMore && (
                <div className="flex justify-center py-4">
                  <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
                </div>
              )}
              {hasMore && !loadingMore && photos.length > 0 && (
                <div className="flex justify-center py-3">
                  <button
                    onClick={handleLoadMore}
                    className="rounded-lg px-4 py-2 text-xs font-medium text-indigo-600 transition-colors hover:bg-indigo-50 dark:text-indigo-400 dark:hover:bg-indigo-950/30"
                  >
                    Weitere Bilder laden…
                  </button>
                </div>
              )}
              {query && !searching && photos.length === 0 && unsplashAvailable && (
                <p className="py-8 text-center text-sm text-gray-400">Keine Ergebnisse für "{query}"</p>
              )}
            </div>
          )}

          {tab === 'url' && (
            <div className="space-y-4">
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Gib eine URL zu einem Bild ein (JPG, PNG, WebP).
              </p>
              <input
                value={customUrl}
                onChange={(e) => setCustomUrl(e.target.value)}
                placeholder="https://example.com/image.jpg"
                className="w-full rounded-lg border border-gray-200 bg-transparent px-4 py-2.5 text-sm text-gray-900 outline-none placeholder:text-gray-400 focus:border-indigo-400 focus:ring-2 focus:ring-indigo-400/20 dark:border-gray-700 dark:text-white"
              />
              {customUrl && (
                <div className="space-y-3">
                  <div className="aspect-video overflow-hidden rounded-xl border border-gray-200 dark:border-gray-700">
                    <img src={customUrl} alt="Vorschau" className="h-full w-full object-cover" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
                  </div>
                  <button
                    onClick={() => {
                      onSelect(customUrl, 'custom');
                      onClose();
                    }}
                    className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700"
                  >
                    Übernehmen
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
