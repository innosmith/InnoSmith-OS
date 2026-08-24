/**
 * TaskPilot LinkedIn Sync – Options Page
 */

const backendUrlInput = document.getElementById('backend-url');
const apiTokenInput = document.getElementById('api-token');
const cfClientIdInput = document.getElementById('cf-client-id');
const cfClientSecretInput = document.getElementById('cf-client-secret');
const statusEl = document.getElementById('status');

function showStatus(message, type) {
  statusEl.textContent = message;
  statusEl.className = `status ${type}`;
  statusEl.classList.remove('hidden');
  setTimeout(() => statusEl.classList.add('hidden'), 8000);
}

document.addEventListener('DOMContentLoaded', async () => {
  const stored = await chrome.storage.sync.get([
    'backendUrl', 'apiToken', 'cfClientId', 'cfClientSecret',
  ]);
  backendUrlInput.value = stored.backendUrl || 'https://tp.innosmith.ai';
  apiTokenInput.value = stored.apiToken || '';
  cfClientIdInput.value = stored.cfClientId || '';
  cfClientSecretInput.value = stored.cfClientSecret || '';
});

document.getElementById('btn-save').addEventListener('click', async () => {
  const backendUrl = backendUrlInput.value.trim().replace(/\/+$/, '');
  const apiToken = apiTokenInput.value.trim();
  const cfClientId = cfClientIdInput.value.trim();
  const cfClientSecret = cfClientSecretInput.value.trim();

  if (!backendUrl) {
    showStatus('Backend-URL darf nicht leer sein.', 'error');
    return;
  }
  if (!apiToken) {
    showStatus('API-Key darf nicht leer sein.', 'error');
    return;
  }
  if (!apiToken.startsWith('tpk_')) {
    showStatus('API-Key muss mit «tpk_» beginnen. Generiere ihn in InnoSmith OS unter Einstellungen → Integrationen.', 'error');
    return;
  }

  if ((cfClientId && !cfClientSecret) || (!cfClientId && cfClientSecret)) {
    showStatus('Cloudflare Access: Bitte beide Felder ausfüllen oder beide leer lassen.', 'error');
    return;
  }

  await chrome.storage.sync.set({ backendUrl, apiToken, cfClientId, cfClientSecret });
  showStatus('Einstellungen gespeichert.', 'success');
});

document.getElementById('btn-test').addEventListener('click', async () => {
  statusEl.textContent = 'Teste Verbindung…';
  statusEl.className = 'status';
  statusEl.classList.remove('hidden');

  const result = await new Promise((resolve) => {
    chrome.runtime.sendMessage({ action: 'testConnection' }, resolve);
  });

  if (result.error) {
    showStatus(`Fehler: ${result.error}`, 'error');
  } else if (result.data && result.data.ok) {
    showStatus(`Verbunden als ${result.data.name} (${result.data.company})`, 'success');
  } else {
    showStatus('Verbindung fehlgeschlagen.', 'error');
  }
});
