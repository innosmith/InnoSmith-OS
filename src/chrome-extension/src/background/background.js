/**
 * TaskPilot LinkedIn Sync – Background Service Worker
 * Alle API-Calls laufen hier (umgeht CORS-Einschränkungen).
 */

async function getConfig() {
  const result = await chrome.storage.sync.get([
    'backendUrl', 'apiToken', 'cfClientId', 'cfClientSecret',
  ]);
  return {
    backendUrl: (result.backendUrl || 'https://tp.innosmith.ai').replace(/\/+$/, ''),
    apiToken: result.apiToken || '',
    cfClientId: result.cfClientId || '',
    cfClientSecret: result.cfClientSecret || '',
  };
}

async function apiRequest(method, path, body) {
  const config = await getConfig();
  if (!config.apiToken) {
    return { error: 'API-Key nicht konfiguriert. Bitte in den Einstellungen hinterlegen.' };
  }

  const url = `${config.backendUrl}${path}`;
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${config.apiToken}`,
  };

  if (config.cfClientId && config.cfClientSecret) {
    headers['CF-Access-Client-Id'] = config.cfClientId;
    headers['CF-Access-Client-Secret'] = config.cfClientSecret;
  }

  // redirect:'manual' verhindert, dass fetch dem Cloudflare-Access-Redirect
  // auf die (fremde) Login-Domain folgt — andernfalls wirft fetch generisch
  // "Failed to fetch" und die eigentliche Ursache bleibt verborgen.
  const options = { method, headers, redirect: 'manual' };
  if (body) {
    options.body = JSON.stringify(body);
  }

  function cfAccessError() {
    if (config.cfClientId && config.cfClientSecret) {
      return 'Cloudflare Access hat den Zugriff abgelehnt. Der Service-Token ist vermutlich abgelaufen oder ungültig — bitte in Cloudflare Zero Trust erneuern und in den Einstellungen aktualisieren.';
    }
    return 'Cloudflare Access blockiert den Zugriff. Bitte CF-Access Client-ID und Secret in den Einstellungen hinterlegen.';
  }

  try {
    const response = await fetch(url, options);

    // Cloudflare Access blockiert mit einem Redirect auf die Login-Domain.
    // Mit redirect:'manual' kommt das als opaqueredirect (status 0) zurück.
    if (
      response.type === 'opaqueredirect' ||
      [301, 302, 303, 307, 308].includes(response.status) ||
      (response.url && response.url.includes('cloudflareaccess'))
    ) {
      return { error: cfAccessError() };
    }

    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      if (response.status === 403) {
        return { error: cfAccessError() };
      }
      // Cloudflare ersetzt bei 502/503 oft die Origin-JSON durch HTML —
      // den Antworttext mitliefern, damit die Diagnose nicht irreführend ist.
      let bodyHint = '';
      try {
        const raw = (await response.text()).replace(/\s+/g, ' ').trim();
        if (raw) {
          bodyHint = raw.length > 180 ? `${raw.slice(0, 180)}…` : raw;
        }
      } catch (_) {
        // ignore
      }
      const suffix = bodyHint ? ` Antwort: ${bodyHint}` : ' Prüfe Backend-URL und Cloudflare-Zugang.';
      return { error: `Unerwartete Antwort (${response.status}).${suffix}` };
    }

    const data = await response.json();
    if (!response.ok) {
      return { error: data.detail || `HTTP ${response.status}` };
    }
    return { data };
  } catch (err) {
    if (err.message === 'Failed to fetch') {
      return { error: 'Verbindung fehlgeschlagen. Mögliche Ursachen: Backend offline, falsche Backend-URL, oder Cloudflare-Access-Service-Token abgelaufen/fehlt.' };
    }
    return { error: `Verbindungsfehler: ${err.message}` };
  }
}

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  if (request.action === 'linkedinLookup') {
    apiRequest('POST', '/api/pipedrive/linkedin-lookup', request.payload)
      .then(sendResponse);
    return true;
  }

  if (request.action === 'linkedinSync') {
    apiRequest('POST', '/api/pipedrive/linkedin-sync', request.payload)
      .then(sendResponse);
    return true;
  }

  if (request.action === 'extractProfileLLM') {
    apiRequest('POST', '/api/linkedin/extract-profile', { html: request.payload.html })
      .then(sendResponse);
    return true;
  }

  if (request.action === 'testConnection') {
    apiRequest('GET', '/api/pipedrive/test-connection')
      .then(sendResponse);
    return true;
  }
});
