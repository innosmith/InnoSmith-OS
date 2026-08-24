import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

export function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [mfaCode, setMfaCode] = useState('');
  const [requiresMfa, setRequiresMfa] = useState(false);
  const [_mfaToken, setMfaToken] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { login, isOwner } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const result = await login({
        email,
        password,
        ...(requiresMfa ? { mfa_code: mfaCode } : {}),
      });

      if (result.requires_mfa) {
        setRequiresMfa(true);
        setMfaToken(result.mfa_token || '');
        setLoading(false);
        return;
      }

      navigate(isOwner ? '/cockpit' : '/projects');
    } catch {
      if (requiresMfa) {
        setError('Ungültiger MFA-Code. Bitte erneut versuchen.');
      } else {
        setError('Anmeldung fehlgeschlagen. Bitte prüfe deine Zugangsdaten.');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-gray-50 to-gray-100 px-4 dark:from-gray-950 dark:to-gray-900">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <img
            src="/favicon.png"
            alt="InnoSmith OS"
            className="mx-auto mb-4 h-14 w-14 rounded-2xl drop-shadow-lg"
          />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            InnoSmith OS
          </h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {requiresMfa ? 'Authentifizierungscode eingeben' : 'Melde dich an, um fortzufahren'}
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-2xl border border-gray-200 bg-white p-8 shadow-xl shadow-gray-200/50 dark:border-gray-800 dark:bg-gray-900 dark:shadow-none"
        >
          {error && (
            <div className="mb-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-600 dark:bg-red-950/50 dark:text-red-400">
              {error}
            </div>
          )}

          {!requiresMfa ? (
            <div className="space-y-4">
              <div>
                <label
                  htmlFor="email"
                  className="mb-1.5 block text-sm font-medium text-gray-700 dark:text-gray-300"
                >
                  E-Mail
                </label>
                <input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  autoFocus
                  placeholder="name@beispiel.de"
                  className="w-full rounded-lg border border-gray-300 bg-transparent px-4 py-2.5 text-sm text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-gray-700 dark:text-white dark:placeholder:text-gray-600 dark:focus:border-indigo-400"
                />
              </div>

              <div>
                <label
                  htmlFor="password"
                  className="mb-1.5 block text-sm font-medium text-gray-700 dark:text-gray-300"
                >
                  Passwort
                </label>
                <input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  placeholder="••••••••"
                  className="w-full rounded-lg border border-gray-300 bg-transparent px-4 py-2.5 text-sm text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-gray-700 dark:text-white dark:placeholder:text-gray-600 dark:focus:border-indigo-400"
                />
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="rounded-lg bg-indigo-50 px-4 py-3 text-sm text-indigo-700 dark:bg-indigo-950/50 dark:text-indigo-300">
                Öffne deine Authenticator-App und gib den 6-stelligen Code ein.
              </div>
              <div>
                <label
                  htmlFor="mfa-code"
                  className="mb-1.5 block text-sm font-medium text-gray-700 dark:text-gray-300"
                >
                  Authentifizierungscode
                </label>
                <input
                  id="mfa-code"
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]{6}"
                  maxLength={6}
                  value={mfaCode}
                  onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ''))}
                  required
                  autoFocus
                  placeholder="000000"
                  className="w-full rounded-lg border border-gray-300 bg-transparent px-4 py-2.5 text-center text-lg font-mono tracking-[0.5em] text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-gray-700 dark:text-white dark:placeholder:text-gray-600 dark:focus:border-indigo-400"
                />
              </div>
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="mt-6 flex w-full items-center justify-center rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 disabled:opacity-50 dark:focus:ring-offset-gray-900"
          >
            {loading ? (
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-white border-t-transparent" />
            ) : requiresMfa ? (
              'Bestätigen'
            ) : (
              'Anmelden'
            )}
          </button>

          {requiresMfa && (
            <button
              type="button"
              onClick={() => {
                setRequiresMfa(false);
                setMfaCode('');
                setMfaToken('');
                setError('');
              }}
              className="mt-3 w-full text-center text-sm text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300"
            >
              Zurück zur Anmeldung
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
