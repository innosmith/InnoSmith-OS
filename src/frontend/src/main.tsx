import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import App from './App';
import { ThemeProvider } from './contexts/ThemeContext';
import { AuthProvider } from './contexts/AuthContext';
import { installChunkErrorRecovery } from './lib/chunkRecovery';

if ((navigator as any).standalone || window.matchMedia('(display-mode: standalone)').matches) {
  document.documentElement.classList.add('standalone');
}

installChunkErrorRecovery();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <AuthProvider>
        <App />
      </AuthProvider>
    </ThemeProvider>
  </StrictMode>,
);
