import { Component, Suspense, lazy, type ReactNode } from 'react';
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
} from 'react-router-dom';
import { useAuth } from './contexts/AuthContext';
import { isChunkLoadError, recoverFromChunkError } from './lib/chunkRecovery';
import { AppLayout } from './layouts/AppLayout';
import { LoginPage } from './pages/LoginPage';
import { CockpitPage } from './pages/CockpitPage';
import { PipelinePage } from './pages/PipelinePage';
import { ProjectBoardPage } from './pages/ProjectBoardPage';
import { ProjectsPage } from './pages/ProjectsPage';
import { AgentQueuePage } from './pages/AgentQueuePage';
import { SettingsPage } from './pages/SettingsPage';
import { InboxPage } from './pages/InboxPage';

import { SignalePage } from './pages/SignalePage';
import { FinancePage } from './pages/FinancePage';
import DebtorsPage from './pages/DebtorsPage';
import CreditorsPage from './pages/CreditorsPage';

const LazyCapacityPage = lazy(() =>
  import('./pages/CapacityPage').then(m => ({ default: m.CapacityPage })),
);

const LazyChatPage = lazy(() =>
  import('./pages/ChatPage').then(m => ({ default: m.ChatPage })),
);

const LazyAnalysisPage = lazy(() =>
  import('./pages/AnalysisPage').then(m => ({ default: m.AnalysisPage })),
);

const LazyDatenschutzPage = lazy(() => import('./pages/DatenschutzPage'));

const LazyMindMapsPage = lazy(() => import('./pages/MindMapsPage'));
const LazyMindMapEditorPage = lazy(() => import('./pages/MindMapEditorPage'));
const LazySharedMindMapPage = lazy(() => import('./pages/SharedMindMapPage'));

interface ErrorBoundaryState { error: Error | null; recovering: boolean }

class ErrorBoundary extends Component<
  { children: ReactNode; scope?: 'app' | 'route' },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null, recovering: false };

  static getDerivedStateFromError(error: Error) {
    return { error, recovering: isChunkLoadError(error) };
  }

  componentDidCatch(error: Error) {
    // Veralteter Chunk nach einem Deploy: still neu laden statt Fehler zeigen.
    if (recoverFromChunkError(error)) return;
    this.setState({ recovering: false });
  }

  render() {
    const { error, recovering } = this.state;
    if (!error) return this.props.children;
    if (recovering) return <SuspenseFallback />;

    const isRoute = this.props.scope === 'route';
    return (
      <div
        className={`flex flex-col items-center justify-center px-4 ${
          isRoute ? 'h-full py-12' : 'min-h-screen bg-gray-50 dark:bg-gray-950'
        }`}
      >
        <div className="w-full max-w-md rounded-xl border border-red-200 bg-white p-6 shadow-lg dark:border-red-800 dark:bg-gray-900">
          <h1 className="mb-2 text-lg font-semibold text-red-700 dark:text-red-400">Fehler beim Laden</h1>
          <p className="mb-4 text-sm text-gray-600 dark:text-gray-400">
            {isRoute
              ? 'Diese Seite konnte nicht geladen werden. Die Navigation funktioniert weiterhin.'
              : 'Die Anwendung konnte nicht geladen werden. Bitte versuche es erneut.'}
          </p>
          <pre className="mb-4 max-h-32 overflow-auto rounded-lg bg-gray-100 p-3 text-xs text-gray-700 dark:bg-gray-800 dark:text-gray-300">
            {error.message}
          </pre>
          <button
            onClick={() => { this.setState({ error: null, recovering: false }); window.location.reload(); }}
            className="min-h-11 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            Seite neu laden
          </button>
        </div>
      </div>
    );
  }
}

function SuspenseFallback() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-indigo-500 border-t-transparent" />
    </div>
  );
}

/** Lazy-Route: eigener Error-Boundary, damit ein fehlender Chunk die App nicht ersetzt. */
function LazyRoute({ children }: { children: ReactNode }) {
  return (
    <ErrorBoundary scope="route">
      <Suspense fallback={<SuspenseFallback />}>{children}</Suspense>
    </ErrorBoundary>
  );
}

function ProtectedRoute({ children }: { children: ReactNode }) {
  const { isAuthenticated, profileLoading } = useAuth();
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (profileLoading) return <SuspenseFallback />;
  return <>{children}</>;
}

function OwnerRoute({ children }: { children: ReactNode }) {
  const { isAuthenticated, isOwner, user, profileLoading } = useAuth();
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  if (profileLoading) return <SuspenseFallback />;
  if (user && !isOwner) return <Navigate to="/projects" replace />;
  return <>{children}</>;
}

function PublicRoute({ children }: { children: ReactNode }) {
  const { isAuthenticated, isOwner, profileLoading } = useAuth();
  if (isAuthenticated && profileLoading) return <SuspenseFallback />;
  if (isAuthenticated) return <Navigate to={isOwner ? '/cockpit' : '/projects'} replace />;
  return <>{children}</>;
}

function DefaultRedirect() {
  const { isAuthenticated, isOwner, profileLoading } = useAuth();
  if (!isAuthenticated && !profileLoading) return <Navigate to="/login" replace />;
  if (profileLoading) return <SuspenseFallback />;
  return <Navigate to={isOwner ? '/cockpit' : '/projects'} replace />;
}

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          {/* Öffentliche Share-Seite (kein Auth) */}
          <Route
            path="/shared/:token"
            element={
              <LazyRoute>
                <LazySharedMindMapPage />
              </LazyRoute>
            }
          />

          <Route
            path="/login"
            element={
              <PublicRoute>
                <LoginPage />
              </PublicRoute>
            }
          />

          {/* Member-erlaubte Routen (alle authentifizierten User) */}
          <Route
            element={
              <ProtectedRoute>
                <AppLayout />
              </ProtectedRoute>
            }
          >
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/projects/:id" element={<ProjectBoardPage />} />
            <Route path="/settings" element={<SettingsPage />} />

            {/* Owner-only Routen */}
            <Route path="/cockpit" element={<OwnerRoute><CockpitPage /></OwnerRoute>} />
            <Route path="/pipeline" element={<OwnerRoute><PipelinePage /></OwnerRoute>} />
            <Route path="/agenten" element={<OwnerRoute><AgentQueuePage /></OwnerRoute>} />
            <Route path="/agenten/chat" element={<OwnerRoute><LazyRoute><LazyChatPage /></LazyRoute></OwnerRoute>} />
            <Route path="/agenten/chat/:conversationId" element={<OwnerRoute><LazyRoute><LazyChatPage /></LazyRoute></OwnerRoute>} />
            <Route path="/datenschutz" element={<OwnerRoute><LazyRoute><LazyDatenschutzPage /></LazyRoute></OwnerRoute>} />
            <Route path="/mindmaps" element={<OwnerRoute><LazyRoute><LazyMindMapsPage /></LazyRoute></OwnerRoute>} />
            <Route path="/mindmaps/:id" element={<LazyRoute><LazyMindMapEditorPage /></LazyRoute>} />
            <Route path="/inbox" element={<OwnerRoute><InboxPage /></OwnerRoute>} />
            <Route path="/signale" element={<OwnerRoute><SignalePage /></OwnerRoute>} />
            <Route path="/kapazitaet" element={<OwnerRoute><LazyRoute><LazyCapacityPage /></LazyRoute></OwnerRoute>} />
            <Route path="/finanzen" element={<OwnerRoute><FinancePage /></OwnerRoute>} />
            <Route path="/finanzen/analysen" element={<OwnerRoute><LazyRoute><LazyAnalysisPage /></LazyRoute></OwnerRoute>} />
            <Route path="/debitoren" element={<OwnerRoute><DebtorsPage /></OwnerRoute>} />
            <Route path="/kreditoren" element={<OwnerRoute><CreditorsPage /></OwnerRoute>} />
          </Route>

          {/* Redirects */}
          <Route path="/agent-queue" element={<Navigate to="/agenten" replace />} />
          <Route path="/memory" element={<Navigate to="/settings?tab=memory" replace />} />
          <Route path="*" element={<DefaultRedirect />} />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
