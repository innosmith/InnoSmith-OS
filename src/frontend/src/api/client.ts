const TOKEN_KEY = 'taskpilot_token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

let _refreshing: Promise<boolean> | null = null;

export async function tryRefreshToken(): Promise<boolean> {
  if (_refreshing) return _refreshing;
  _refreshing = (async () => {
    try {
      const res = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'include',
      });
      if (!res.ok) return false;
      const data = await res.json();
      if (data.access_token) {
        setToken(data.access_token);
        return true;
      }
      return false;
    } catch {
      return false;
    } finally {
      _refreshing = null;
    }
  })();
  return _refreshing;
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  if (response.status === 401) {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      const retryHeaders = { ...headers, Authorization: `Bearer ${getToken()}` };
      const retry = await fetch(path, { ...options, headers: retryHeaders });
      if (retry.ok) {
        if (retry.status === 204) return undefined as T;
        return retry.json();
      }
    }
    clearToken();
    window.location.href = '/login';
    throw new ApiError(401, 'Nicht autorisiert');
  }

  if (!response.ok) {
    const body = await response.text();
    throw new ApiError(response.status, body || response.statusText);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json();
}

export const api = {
  get: <T>(path: string, options?: RequestInit) => request<T>(path, options),

  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    }),

  put: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'PUT',
      body: body ? JSON.stringify(body) : undefined,
    }),

  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'PATCH',
      body: body ? JSON.stringify(body) : undefined,
    }),

  delete: <T>(path: string) =>
    request<T>(path, { method: 'DELETE' }),

  /** Lädt eine Ressource authentifiziert als Blob (z. B. Sandbox-Artefakte).
   * Nötig, weil Auth per Bearer-Header läuft — ein <img>/<iframe src> würde 401 liefern. */
  blob: async (path: string): Promise<Blob> => {
    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    let response = await fetch(path, { headers });
    if (response.status === 401) {
      const refreshed = await tryRefreshToken();
      if (refreshed) {
        response = await fetch(path, { headers: { Authorization: `Bearer ${getToken()}` } });
      }
    }
    if (!response.ok) {
      const body = await response.text().catch(() => '');
      throw new ApiError(response.status, body || response.statusText);
    }
    return response.blob();
  },

  upload: async <T>(path: string, formData: FormData): Promise<T> => {
    const token = getToken();
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const response = await fetch(path, { method: 'POST', headers, body: formData });
    if (response.status === 401) { clearToken(); window.location.href = '/login'; throw new ApiError(401, 'Nicht autorisiert'); }
    if (!response.ok) { const body = await response.text(); throw new ApiError(response.status, body || response.statusText); }
    if (response.status === 204) return undefined as T;
    return response.json();
  },
};
