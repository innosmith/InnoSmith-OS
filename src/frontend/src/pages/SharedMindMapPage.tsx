import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  ReactFlow,
  ReactFlowProvider,
  MiniMap,
  Controls,
  Background,
  BackgroundVariant,
} from '@xyflow/react';
import { MindMapNode } from '../components/mindmap/MindMapNode';
import { MindMapEdge } from '../components/mindmap/MindMapEdge';
import { useMindmapStore } from '../stores/mindmapStore';
import { BrainCircuit, Lock, Eye } from 'lucide-react';

const nodeTypes = { mindmapNode: MindMapNode };
const edgeTypes = { mindmapEdge: MindMapEdge };

interface SharedMapData {
  id: string;
  title: string;
  permission: 'view' | 'edit';
  flow_data: { nodes: any[]; edges: any[]; viewport: { x: number; y: number; zoom: number } };
  background_color: string | null;
}

function SharedMindMapInner() {
  const { token } = useParams<{ token: string }>();
  const {
    nodes, edges, isDirty,
    setNodes, setEdges, onNodesChange, onEdgesChange, onConnect, markClean,
  } = useMindmapStore();

  const [phase, setPhase] = useState<'password' | 'loading' | 'ready' | 'error'>('password');
  const [password, setPassword] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  const [title, setTitle] = useState('');
  const [permission, setPermission] = useState<'view' | 'edit'>('view');
  const [backgroundColor, setBackgroundColor] = useState<string | null>(null);
  const passwordRef = useRef('');
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSavedRef = useRef('');

  const verify = useCallback(async () => {
    if (!password || !token) return;
    setPhase('loading');
    setErrorMsg('');
    passwordRef.current = password;
    try {
      const res = await fetch(`/api/public/mindmaps/${token}/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setErrorMsg(body.detail || 'Zugriff verweigert');
        setPhase('password');
        return;
      }
      const data: SharedMapData = await res.json();
      setTitle(data.title);
      setPermission(data.permission);
      setBackgroundColor(data.background_color);
      setNodes(data.flow_data.nodes || []);
      setEdges(data.flow_data.edges || []);
      markClean();
      lastSavedRef.current = JSON.stringify(data.flow_data);
      setPhase('ready');
    } catch {
      setErrorMsg('Verbindung fehlgeschlagen');
      setPhase('password');
    }
  }, [password, token, setNodes, setEdges, markClean]);

  const saveNow = useCallback(async () => {
    if (!token || permission !== 'edit') return;
    const flowData = { nodes, edges, viewport: { x: 0, y: 0, zoom: 1 } };
    const serialized = JSON.stringify(flowData);
    if (serialized === lastSavedRef.current) return;
    try {
      await fetch(`/api/public/mindmaps/${token}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          'X-Share-Password': passwordRef.current,
        },
        body: JSON.stringify({ flow_data: flowData }),
      });
      lastSavedRef.current = serialized;
      markClean();
    } catch {
      // Fehler wird ignoriert
    }
  }, [token, permission, nodes, edges, markClean]);

  useEffect(() => {
    if (!isDirty || permission !== 'edit') return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(saveNow, 1500);
    return () => { if (saveTimerRef.current) clearTimeout(saveTimerRef.current); };
  }, [isDirty, nodes, edges, saveNow, permission]);

  if (phase === 'password' || phase === 'loading') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950 px-4" data-testid="shared-password-gate">
        <div className="w-full max-w-sm">
          <div className="text-center mb-8">
            <div className="mx-auto w-14 h-14 rounded-2xl bg-indigo-100 dark:bg-indigo-950 flex items-center justify-center mb-4">
              <BrainCircuit size={28} className="text-indigo-600" />
            </div>
            <h1 className="text-xl font-bold text-gray-900 dark:text-white">InnoSmith OS Mind-Map</h1>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Passwort eingeben, um fortzufahren</p>
          </div>

          <div className="rounded-2xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 shadow-lg p-6">
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  <Lock size={14} className="inline mr-1" />
                  Passwort
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') verify(); }}
                  className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2.5 text-sm dark:text-white focus:outline-none focus:ring-2 focus:ring-indigo-400"
                  placeholder="Passwort..."
                  autoFocus
                  data-testid="shared-password-input"
                />
              </div>

              {errorMsg && (
                <p className="text-sm text-red-500">{errorMsg}</p>
              )}

              <button
                onClick={verify}
                disabled={!password || phase === 'loading'}
                className="w-full py-2.5 rounded-lg bg-indigo-600 text-white font-medium hover:bg-indigo-700 disabled:opacity-50 transition-colors"
                data-testid="shared-verify-button"
              >
                {phase === 'loading' ? (
                  <span className="flex items-center justify-center gap-2">
                    <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                    Prüfe...
                  </span>
                ) : 'Öffnen'}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (phase === 'error') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950">
        <p className="text-red-500">{errorMsg}</p>
      </div>
    );
  }

  const isReadonly = permission === 'view';

  return (
    <div className="flex flex-col h-screen" data-testid="shared-mindmap">
      {/* Minimal header */}
      <div className="flex items-center gap-3 px-4 py-2 bg-white/80 dark:bg-gray-900/80 backdrop-blur-sm border-b border-gray-200 dark:border-gray-800">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 text-xs font-bold text-white">
          T
        </div>
        <h1 className="text-lg font-semibold text-gray-900 dark:text-white">{title}</h1>
        <div className="flex items-center gap-1 text-xs text-gray-400">
          {isReadonly ? <Eye size={12} /> : null}
          {isReadonly ? 'Nur Ansicht' : 'Bearbeitbar'}
        </div>
        {isDirty && !isReadonly && (
          <span className="text-xs text-amber-500 font-medium ml-2">Ungespeichert</span>
        )}
      </div>

      {/* Canvas */}
      <div className="flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={isReadonly ? undefined : onNodesChange}
          onEdgesChange={isReadonly ? undefined : onEdgesChange}
          onConnect={isReadonly ? undefined : onConnect}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          nodesDraggable={!isReadonly}
          nodesConnectable={!isReadonly}
          elementsSelectable={!isReadonly}
          fitView
          style={backgroundColor ? { backgroundColor } : undefined}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#94A3B8" className="opacity-30" />
          <Controls showInteractive={false} className="!rounded-xl !border-gray-200 !shadow-lg dark:!border-gray-700 dark:!bg-gray-900" />
          <MiniMap
            nodeColor={(n) => (n.data as any)?.color || '#3B82F6'}
            className="!rounded-xl !border-gray-200 !shadow-lg dark:!border-gray-700 dark:!bg-gray-900"
            maskColor="rgba(0,0,0,0.08)"
          />
        </ReactFlow>
      </div>
    </div>
  );
}

export function SharedMindMapPage() {
  return (
    <ReactFlowProvider>
      <SharedMindMapInner />
    </ReactFlowProvider>
  );
}

export default SharedMindMapPage;
