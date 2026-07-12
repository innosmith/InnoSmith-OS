import { useState, useEffect, useCallback, useRef } from 'react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import { QRCodeSVG } from 'qrcode.react';
import { api } from '../api/client';
import type { TaskDetailMode } from '../types';
import { DEFAULT_PROVIDER_ORDER as PROVIDER_ORDER, PROVIDER_LABELS } from '../lib/modelOrdering';

interface UserProfile {
  id: string;
  email: string;
  display_name: string;
  role: string;
  avatar_url: string | null;
  mfa_enabled?: boolean;
}

interface UserSettingsData {
  agenda_background_url?: string | null;
  task_detail_mode?: TaskDetailMode | null;
  sidebar_collapsed?: boolean | null;
  app_logo_url?: string | null;
  sidebar_color?: string | null;
  show_column_count?: boolean | null;
  cockpit_background_url?: string | null;
  cockpit_calendar_exclude_categories?: string | null;
  cockpit_calendar_hide_private?: boolean | null;
  creditors_overview_exclude_vendors?: string | null;
  default_hourly_rate?: number | null;
  forecast_pipeline_weight?: number | null;
  forecast_fill_horizon_months?: number | null;
  forecast_vat_rate?: number | null;
  annual_revenue_goal?: number | null;
  min_liquidity?: number | null;
  vat_method?: string | null;
  vat_saldo_rate?: number | null;
  tax_canton?: string | null;
  civil_status?: string | null;
  briefing_daily_enabled?: boolean | null;
  briefing_daily_time?: string | null;
  briefing_weekly_enabled?: boolean | null;
  briefing_weekly_day?: number | null;
  briefing_weekly_time?: string | null;
  briefing_monthly_enabled?: boolean | null;
  briefing_monthly_time?: string | null;
  followup_enabled?: boolean | null;
  followup_wait_days?: number | null;
}

interface ManagedUser {
  id: string;
  email: string;
  display_name: string;
  role: string;
  avatar_url: string | null;
  is_active: boolean;
}

interface TriageSettingsData {
  triage_prompt: string | null;
  triage_interval_seconds: number | null;
  triage_enabled: boolean | null;
  inbox_hidden_folders: string[] | null;
  integrations_active_env: boolean;
  app_env: string;
}

interface TriageTestResult {
  id: string;
  message_id: string;
  subject: string | null;
  from_address: string | null;
  triage_class: string | null;
}

type SettingsTab = 'profile' | 'display' | 'cockpit' | 'finance' | 'finance_analysis' | 'llm' | 'integrations' | 'triage' | 'search_index' | 'team' | 'intelligence';

interface IndexSourceTotals {
  source_type: string;
  documents: number;
  chunks: number;
  last_indexed_at: string | null;
}

interface IndexStatusData {
  running: boolean;
  phase: string | null;
  detail: string | null;
  run_started_at: string | null;
  run_finished_at: string | null;
  heartbeat_at: string | null;
  folders_total: number;
  folders_done: number;
  docs_total: number;
  docs_done: number;
  last_emails: number;
  last_documents: number;
  last_chunks: number;
  last_error: string | null;
  last_error_at: string | null;
  interval_seconds: number;
  next_run_at: string | null;
  stale: boolean;
  totals: IndexSourceTotals[];
  last_indexed_at: string | null;
}

export function SettingsPage() {
  const [searchParams] = useSearchParams();
  const initialTab = (searchParams.get('tab') as SettingsTab) || 'profile';
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [settings, setSettings] = useState<UserSettingsData>({});
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [loading, setLoading] = useState(true);
  const { refreshAppSettings } = useOutletContext<{ refreshAppSettings: () => void }>();

  const [displayName, setDisplayName] = useState('');
  const [profileEmail, setProfileEmail] = useState('');
  const [currentPw, setCurrentPw] = useState('');
  const [newPw, setNewPw] = useState('');
  const [confirmPw, setConfirmPw] = useState('');
  const [pwMsg, setPwMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [profileMsg, setProfileMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const avatarInput = useRef<HTMLInputElement>(null);
  const logoInput = useRef<HTMLInputElement>(null);

  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteName, setInviteName] = useState('');
  const [inviteRole, setInviteRole] = useState('member');
  const [confirmDeleteUserId, setConfirmDeleteUserId] = useState<string | null>(null);
  const [createdUserPassword, setCreatedUserPassword] = useState<{ email: string; password: string } | null>(null);

  const [triagePrompt, setTriagePrompt] = useState('');
  const [triageInterval, setTriageInterval] = useState(2);
  const [triageEnabled, setTriageEnabled] = useState(true);
  const [triageMsg, setTriageMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [triageTestResults, setTriageTestResults] = useState<TriageTestResult[] | null>(null);
  const [triageTesting, setTriageTesting] = useState(false);
  const [briefingBusy, setBriefingBusy] = useState<string | null>(null);
  const [briefingMsg, setBriefingMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [hiddenFolders, setHiddenFolders] = useState<string[]>(['ArchivSorted', 'Conversation History', 'Outbox']);
  const [hiddenFolderInput, setHiddenFolderInput] = useState('');
  const [excludedPaths, setExcludedPaths] = useState<string[]>([]);
  const [excludedInput, setExcludedInput] = useState('');
  const [searchIndexMsg, setSearchIndexMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [indexStatus, setIndexStatus] = useState<IndexStatusData | null>(null);
  const [integrationsActiveEnv, setIntegrationsActiveEnv] = useState(true);
  const [appEnv, setAppEnv] = useState('prod');

  const [pdToken, setPdToken] = useState('');
  const [pdDomain, setPdDomain] = useState('innosmith');
  const [pdMsg, setPdMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [pdTesting, setPdTesting] = useState(false);

  const [togglToken, setTogglToken] = useState('');
  const [togglWsId, setTogglWsId] = useState('');
  const [togglMsg, setTogglMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [togglTesting, setTogglTesting] = useState(false);

  const [bexioToken, setBexioToken] = useState('');
  const [bexioMsg, setBexioMsg] = useState<{ type: 'ok' | 'err' | 'warn'; text: string } | null>(null);
  const [bexioTesting, setBexioTesting] = useState(false);

  const [extApiKey, setExtApiKey] = useState('');
  const [extKeyCreatedAt, setExtKeyCreatedAt] = useState<string | null>(null);
  const [extHasKey, setExtHasKey] = useState(false);
  const [extLoading, setExtLoading] = useState(false);
  const [extMsg, setExtMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [extCopied, setExtCopied] = useState(false);

  const [mfaEnabled, setMfaEnabled] = useState(false);
  const [mfaProvUri, setMfaProvUri] = useState<string | null>(null);
  const [mfaSecret, setMfaSecret] = useState<string | null>(null);
  const [mfaCode, setMfaCode] = useState('');
  const [mfaMsg, setMfaMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const [mfaLoading, setMfaLoading] = useState(false);

  const fetchData = useCallback(async () => {
    try {
      const p = await api.get<UserProfile>('/api/auth/me');
      setProfile(p);
      setDisplayName(p.display_name);
      setProfileEmail(p.email);
      setMfaEnabled(!!p.mfa_enabled);
      if (p.role === 'owner') {
        const [s, u, ts, si] = await Promise.all([
          api.get<UserSettingsData>('/api/settings'),
          api.get<ManagedUser[]>('/api/auth/users'),
          api.get<TriageSettingsData>('/api/settings/triage'),
          api.get<{ excluded_paths: string[] | null }>('/api/settings/search-index'),
        ]);
        setSettings(s);
        setUsers(u);
        if (ts.triage_prompt) setTriagePrompt(ts.triage_prompt);
        if (ts.triage_interval_seconds) setTriageInterval(Math.round(ts.triage_interval_seconds / 60));
        if (ts.triage_enabled !== null && ts.triage_enabled !== undefined) setTriageEnabled(ts.triage_enabled);
        if (ts.inbox_hidden_folders) setHiddenFolders(ts.inbox_hidden_folders);
        setIntegrationsActiveEnv(ts.integrations_active_env ?? true);
        setAppEnv(ts.app_env ?? 'prod');
        if (si.excluded_paths) setExcludedPaths(si.excluded_paths);
      }
    } catch { /* */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Suchindex-Status live pollen, solange der Tab offen ist (Owner). Der Daemon
  // schreibt den Status, das Backend liest nur -- daher genuegt einfaches Polling.
  useEffect(() => {
    if (tab !== 'search_index' || profile?.role !== 'owner') return;
    let active = true;
    const load = async () => {
      try {
        const st = await api.get<IndexStatusData>('/api/settings/search-index/status');
        if (active) setIndexStatus(st);
      } catch { /* still */ }
    };
    load();
    const id = setInterval(load, 8000);
    return () => { active = false; clearInterval(id); };
  }, [tab, profile?.role]);

  useEffect(() => {
    if (tab !== 'integrations') return;
    api.get<{ pipedrive_api_token: string | null; pipedrive_domain: string | null; toggl_api_token: string | null; toggl_workspace_id: number | null; bexio_api_token: string | null; integrations_active_env?: boolean; triage_enabled?: boolean; app_env?: string }>('/api/settings/integrations')
      .then((data) => {
        setPdToken(data.pipedrive_api_token || '');
        setPdDomain(data.pipedrive_domain || 'innosmith');
        setTogglToken(data.toggl_api_token || '');
        setTogglWsId(data.toggl_workspace_id ? String(data.toggl_workspace_id) : '');
        setBexioToken(data.bexio_api_token || '');
        if (data.integrations_active_env !== undefined) setIntegrationsActiveEnv(data.integrations_active_env);
        if (data.triage_enabled !== undefined) setTriageEnabled(data.triage_enabled);
        if (data.app_env) setAppEnv(data.app_env);
      })
      .catch(() => {});
    api.get<{ has_key: boolean; created_at: string | null }>('/api/settings/extension-api-key')
      .then((data) => {
        setExtHasKey(data.has_key);
        setExtKeyCreatedAt(data.created_at);
      })
      .catch(() => {});
  }, [tab]);

  const saveProfile = async () => {
    if (!displayName.trim() || !profileEmail.trim()) return;
    try {
      const body: Record<string, string> = { display_name: displayName.trim() };
      if (profileEmail.trim() !== profile?.email) body.email = profileEmail.trim();
      const updated = await api.patch<UserProfile>('/api/auth/me', body);
      setProfile(updated);
      setProfileEmail(updated.email);
      setProfileMsg({ type: 'ok', text: 'Profil gespeichert' });
      setTimeout(() => setProfileMsg(null), 3000);
    } catch {
      setProfileMsg({ type: 'err', text: 'Fehler beim Speichern' });
    }
  };

  const uploadAvatar = async (file: File) => {
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await fetch('/api/uploads/avatars', {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('taskpilot_token')}` },
        body: form,
      });
      if (!res.ok) {
        const err = await res.text().catch(() => '');
        alert(`Avatar-Upload fehlgeschlagen: ${res.status} ${err || res.statusText}`);
        return;
      }
      const { url } = await res.json();
      const updated = await api.patch<UserProfile>('/api/auth/me', { avatar_url: url });
      setProfile(updated);
    } catch (e) {
      alert(`Avatar-Upload fehlgeschlagen: ${e instanceof Error ? e.message : 'Unbekannter Fehler'}`);
    }
  };

  const changePassword = async () => {
    setPwMsg(null);
    if (newPw !== confirmPw) { setPwMsg({ type: 'err', text: 'Passwörter stimmen nicht überein' }); return; }
    if (newPw.length < 8) { setPwMsg({ type: 'err', text: 'Mindestens 8 Zeichen' }); return; }
    try {
      await api.post('/api/auth/change-password', { current_password: currentPw, new_password: newPw });
      setPwMsg({ type: 'ok', text: 'Passwort geändert' });
      setCurrentPw(''); setNewPw(''); setConfirmPw('');
      setTimeout(() => setPwMsg(null), 3000);
    } catch {
      setPwMsg({ type: 'err', text: 'Aktuelles Passwort ist falsch' });
    }
  };

  const startMfaSetup = async () => {
    setMfaLoading(true);
    setMfaMsg(null);
    try {
      const res = await api.post<{ secret: string; provisioning_uri: string }>('/api/auth/mfa/setup', {});
      setMfaProvUri(res.provisioning_uri);
      setMfaSecret(res.secret);
    } catch {
      setMfaMsg({ type: 'err', text: 'MFA-Setup fehlgeschlagen' });
    } finally {
      setMfaLoading(false);
    }
  };

  const verifyMfa = async () => {
    if (mfaCode.length !== 6) { setMfaMsg({ type: 'err', text: 'Bitte 6-stelligen Code eingeben' }); return; }
    setMfaLoading(true);
    setMfaMsg(null);
    try {
      await api.post('/api/auth/mfa/verify', { code: mfaCode });
      setMfaEnabled(true);
      setMfaProvUri(null);
      setMfaSecret(null);
      setMfaCode('');
      setMfaMsg({ type: 'ok', text: 'MFA erfolgreich aktiviert' });
      setTimeout(() => setMfaMsg(null), 4000);
    } catch {
      setMfaMsg({ type: 'err', text: 'Ungültiger Code — bitte erneut versuchen' });
    } finally {
      setMfaLoading(false);
    }
  };

  const disableMfa = async () => {
    if (mfaCode.length !== 6) { setMfaMsg({ type: 'err', text: 'Bitte aktuellen TOTP-Code eingeben' }); return; }
    setMfaLoading(true);
    setMfaMsg(null);
    try {
      await api.post('/api/auth/mfa/disable', { code: mfaCode });
      setMfaEnabled(false);
      setMfaCode('');
      setMfaMsg({ type: 'ok', text: 'MFA deaktiviert' });
      setTimeout(() => setMfaMsg(null), 4000);
    } catch {
      setMfaMsg({ type: 'err', text: 'Ungültiger Code' });
    } finally {
      setMfaLoading(false);
    }
  };

  const updateSetting = async (key: string, value: string | boolean | number | null) => {
    const updated = await api.patch<UserSettingsData>('/api/settings', { [key]: value });
    setSettings(updated);
    if (key === 'app_logo_url' || key === 'sidebar_color') {
      refreshAppSettings();
    }
  };

  const createUser = async () => {
    if (!inviteEmail.trim() || !inviteName.trim()) return;
    try {
      const res = await api.post<{ email: string; temp_password: string }>('/api/auth/users', {
        email: inviteEmail.trim(),
        display_name: inviteName.trim(),
        role: inviteRole,
      });
      setCreatedUserPassword({ email: res.email, password: res.temp_password });
      setInviteEmail(''); setInviteName(''); setInviteRole('member');
      const u = await api.get<ManagedUser[]>('/api/auth/users');
      setUsers(u);
    } catch { /* */ }
  };

  const toggleUserActive = async (userId: string, currentlyActive: boolean) => {
    await api.patch(`/api/auth/users/${userId}`, { is_active: !currentlyActive });
    const u = await api.get<ManagedUser[]>('/api/auth/users');
    setUsers(u);
  };

  const deleteUser = async (userId: string) => {
    try {
      await api.delete(`/api/auth/users/${userId}`);
      setConfirmDeleteUserId(null);
      const u = await api.get<ManagedUser[]>('/api/auth/users');
      setUsers(u);
    } catch { /* */ }
  };

  const saveTriageSettings = async () => {
    try {
      await api.put<TriageSettingsData>('/api/settings/triage', {
        triage_prompt: triagePrompt || null,
        triage_interval_seconds: triageInterval * 60,
        triage_enabled: triageEnabled,
        inbox_hidden_folders: hiddenFolders.length > 0 ? hiddenFolders : null,
      });
      setTriageMsg({ type: 'ok', text: 'Triage-Einstellungen gespeichert' });
      setTimeout(() => setTriageMsg(null), 3000);
    } catch {
      setTriageMsg({ type: 'err', text: 'Fehler beim Speichern' });
    }
  };

  const saveSearchIndexSettings = async () => {
    try {
      const res = await api.put<{ excluded_paths: string[] | null; purged: number }>(
        '/api/settings/search-index',
        { excluded_paths: excludedPaths },
      );
      if (res.excluded_paths) setExcludedPaths(res.excluded_paths);
      const purged = res.purged || 0;
      setSearchIndexMsg({
        type: 'ok',
        text: purged > 0
          ? `Gespeichert -- ${purged} bereits indexierte Einträge bereinigt`
          : 'Ausschlussliste gespeichert',
      });
      setTimeout(() => setSearchIndexMsg(null), 4000);
    } catch {
      setSearchIndexMsg({ type: 'err', text: 'Fehler beim Speichern' });
    }
  };

  const fmtDateTime = (s: string | null | undefined): string => {
    if (!s) return '--';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '--';
    return d.toLocaleString('de-CH', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  };

  const fmtRelative = (s: string | null | undefined): string => {
    if (!s) return '';
    const then = new Date(s).getTime();
    if (isNaN(then)) return '';
    const diff = Math.round((Date.now() - then) / 1000);
    const abs = Math.abs(diff);
    const past = diff >= 0;
    let val: string;
    if (abs < 60) val = 'gerade eben';
    else if (abs < 3600) val = `${Math.round(abs / 60)} min`;
    else if (abs < 86400) val = `${Math.round(abs / 3600)} h`;
    else val = `${Math.round(abs / 86400)} Tg.`;
    if (val === 'gerade eben') return val;
    return past ? `vor ${val}` : `in ${val}`;
  };

  const sourceLabel = (t: string): string => (
    { email: 'E-Mails', onedrive: 'OneDrive', upload: 'Uploads', transcript: 'Transkripte' }[t] || t
  );

  const testTriage = async () => {
    setTriageTesting(true);
    setTriageTestResults(null);
    try {
      const result = await api.post<{ classified: number }>('/api/triage/run?top=5');
      const items = await api.get<TriageTestResult[]>('/api/triage?limit=5');
      setTriageTestResults(items);
      setTriageMsg({ type: 'ok', text: `${result.classified} E-Mails klassifiziert` });
      setTimeout(() => setTriageMsg(null), 5000);
    } catch {
      setTriageMsg({ type: 'err', text: 'Test-Triage fehlgeschlagen' });
    } finally {
      setTriageTesting(false);
    }
  };

  const generateBriefing = async (type: 'daily_briefing' | 'weekly_briefing' | 'monthly_briefing') => {
    setBriefingBusy(type);
    setBriefingMsg(null);
    const labels: Record<string, string> = {
      daily_briefing: 'Tagesbriefing',
      weekly_briefing: 'Wochenbriefing',
      monthly_briefing: 'Monatsbriefing',
    };
    try {
      await api.post('/api/briefings/generate', { type });
      setBriefingMsg({ type: 'ok', text: `${labels[type]} wird erstellt — erscheint gleich im Cockpit` });
      setTimeout(() => setBriefingMsg(null), 6000);
    } catch {
      setBriefingMsg({ type: 'err', text: `${labels[type]} konnte nicht ausgelöst werden` });
    } finally {
      setBriefingBusy(null);
    }
  };

  const saveIntegrations = async () => {
    setPdMsg(null); setTogglMsg(null); setBexioMsg(null);
    try {
      const payload: Record<string, string | number> = { pipedrive_domain: pdDomain };
      if (pdToken && !pdToken.startsWith('****')) payload.pipedrive_api_token = pdToken;
      if (togglToken && !togglToken.startsWith('****')) payload.toggl_api_token = togglToken;
      if (togglWsId) payload.toggl_workspace_id = parseInt(togglWsId) || 0;
      if (bexioToken && !bexioToken.startsWith('****')) payload.bexio_api_token = bexioToken;
      const data = await api.put<{ pipedrive_api_token: string | null; pipedrive_domain: string | null; toggl_api_token: string | null; toggl_workspace_id: number | null; bexio_api_token: string | null }>('/api/settings/integrations', payload);
      setPdToken(data.pipedrive_api_token || '');
      setPdDomain(data.pipedrive_domain || 'innosmith');
      setTogglToken(data.toggl_api_token || '');
      setTogglWsId(data.toggl_workspace_id ? String(data.toggl_workspace_id) : '');
      setBexioToken(data.bexio_api_token || '');
      setPdMsg({ type: 'ok', text: 'Einstellungen gespeichert' });
      setTimeout(() => setPdMsg(null), 3000);
    } catch (err: unknown) {
      const detail = err instanceof Error ? err.message : String(err);
      setPdMsg({ type: 'err', text: `Fehler beim Speichern: ${detail}` });
    }
  };

  const testPipedrive = async () => {
    setPdTesting(true);
    setPdMsg(null);
    try {
      const result = await api.get<{ ok: boolean; name: string; company: string }>('/api/pipedrive/test-connection');
      if (result.ok) {
        setPdMsg({ type: 'ok', text: `Verbunden als ${result.name} (${result.company})` });
      } else {
        setPdMsg({ type: 'err', text: 'Verbindung fehlgeschlagen' });
      }
    } catch (err: unknown) {
      const detail = err instanceof Error ? err.message : String(err);
      setPdMsg({ type: 'err', text: `Verbindungstest fehlgeschlagen: ${detail}` });
    } finally {
      setPdTesting(false);
    }
  };

  const testToggl = async () => {
    setTogglTesting(true);
    setTogglMsg(null);
    try {
      const result = await api.get<{ ok: boolean; name: string; email: string; default_workspace_id: number }>('/api/toggl/test-connection');
      if (result.ok) {
        setTogglMsg({ type: 'ok', text: `Verbunden als ${result.name} (${result.email})` });
        if (result.default_workspace_id && !togglWsId) {
          setTogglWsId(String(result.default_workspace_id));
        }
      } else {
        setTogglMsg({ type: 'err', text: 'Verbindung fehlgeschlagen' });
      }
    } catch (err: unknown) {
      const detail = err instanceof Error ? err.message : String(err);
      setTogglMsg({ type: 'err', text: `Verbindungstest fehlgeschlagen: ${detail}` });
    } finally {
      setTogglTesting(false);
    }
  };

  const testBexio = async () => {
    setBexioTesting(true);
    setBexioMsg(null);
    try {
      const result = await api.get<{ ok: boolean; name: string; email: string; token_expires_at?: string; token_days_remaining?: number; token_expired?: boolean }>('/api/bexio/test-connection');
      const days = result.token_days_remaining;
      const expDate = result.token_expires_at ? new Date(result.token_expires_at).toLocaleDateString('de-CH') : null;
      if (result.token_expired) {
        setBexioMsg({ type: 'err', text: `Token abgelaufen${expDate ? ` (seit ${expDate})` : ''}. Neuen Token unter developer.bexio.com/pat erstellen.` });
      } else if (result.ok) {
        if (typeof days === 'number' && days <= 14) {
          setBexioMsg({ type: 'warn', text: `Verbunden als ${result.name} — Token läuft in ${Math.round(days)} Tagen ab${expDate ? ` (${expDate})` : ''}. Bald erneuern.` });
        } else if (typeof days === 'number' && expDate) {
          setBexioMsg({ type: 'ok', text: `Verbunden als ${result.name} (${result.email}) — Token gültig bis ${expDate}.` });
        } else {
          setBexioMsg({ type: 'ok', text: `Verbunden als ${result.name} (${result.email})` });
        }
      } else {
        setBexioMsg({ type: 'err', text: 'Verbindung fehlgeschlagen' });
      }
    } catch (err: unknown) {
      const detail = err instanceof Error ? err.message : String(err);
      setBexioMsg({ type: 'err', text: `Verbindungstest fehlgeschlagen: ${detail}` });
    } finally {
      setBexioTesting(false);
    }
  };

  const generateExtApiKey = async () => {
    setExtLoading(true);
    setExtMsg(null);
    setExtApiKey('');
    setExtCopied(false);
    try {
      const data = await api.post<{ api_key: string; created_at: string }>('/api/settings/extension-api-key', {});
      setExtApiKey(data.api_key);
      setExtHasKey(true);
      setExtKeyCreatedAt(data.created_at);
      setExtMsg({ type: 'ok', text: 'API-Key generiert — jetzt kopieren und in der Extension einfügen!' });
    } catch (err: unknown) {
      const detail = err instanceof Error ? err.message : String(err);
      setExtMsg({ type: 'err', text: `Fehler: ${detail}` });
    } finally {
      setExtLoading(false);
    }
  };

  const revokeExtApiKey = async () => {
    setExtLoading(true);
    setExtMsg(null);
    try {
      await api.delete('/api/settings/extension-api-key');
      setExtHasKey(false);
      setExtKeyCreatedAt(null);
      setExtApiKey('');
      setExtMsg({ type: 'ok', text: 'API-Key widerrufen' });
    } catch (err: unknown) {
      const detail = err instanceof Error ? err.message : String(err);
      setExtMsg({ type: 'err', text: `Fehler: ${detail}` });
    } finally {
      setExtLoading(false);
    }
  };

  const copyExtApiKey = () => {
    if (!extApiKey) return;
    navigator.clipboard.writeText(extApiKey);
    setExtCopied(true);
    setTimeout(() => setExtCopied(false), 3000);
  };

  const isOwner = profile?.role === 'owner';

  useEffect(() => {
    if (!isOwner && tab !== 'profile') setTab('profile');
  }, [isOwner, tab]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-3 border-indigo-500 border-t-transparent" />
      </div>
    );
  }

  const tabs: { id: SettingsTab; label: string }[] = isOwner
    ? [
        { id: 'profile', label: 'Profil' },
        { id: 'display', label: 'Erscheinungsbild' },
        { id: 'cockpit', label: 'Cockpit' },
        { id: 'finance', label: 'Finanzen' },
        { id: 'finance_analysis', label: 'Finanzanalysen' },
        { id: 'llm', label: 'LLM-Modelle' },
        { id: 'integrations', label: 'Integrationen' },
        { id: 'triage', label: 'E-Mail-Triage' },
        { id: 'search_index', label: 'Suchindex' },
        { id: 'team', label: 'Team' },
      ]
    : [{ id: 'profile', label: 'Profil' }];

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-white/40 bg-white/50 px-4 py-4 sm:px-6 backdrop-blur-sm dark:border-gray-800 dark:bg-gray-900/50">
        <h1 className="text-xl font-bold text-gray-900 dark:text-white">Einstellungen</h1>
      </div>

      <div className="border-b border-white/40 bg-white/50 px-4 sm:px-6 backdrop-blur-sm dark:border-gray-800 dark:bg-gray-900/50">
        <div className="flex gap-4 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" style={{ WebkitOverflowScrolling: 'touch', maskImage: 'linear-gradient(to right, transparent 0, black 8px, black calc(100% - 24px), transparent 100%)' }}>
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`shrink-0 whitespace-nowrap border-b-2 px-1 py-3 text-sm font-medium transition-colors ${
                tab === t.id
                  ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                  : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto overflow-x-hidden overscroll-x-none p-4 sm:p-6">
        <div className="mx-auto max-w-4xl space-y-8 rounded-2xl border border-white/40 bg-white/60 p-6 shadow-sm backdrop-blur-sm dark:border-gray-800 dark:bg-gray-900/60">

          {/* ── Profil ── */}
          {tab === 'profile' && profile && (
            <>
              <section>
                <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Profil</h2>
                <div className="space-y-4">
                  <div className="flex items-center gap-4">
                    <div
                      className="group relative h-16 w-16 cursor-pointer overflow-hidden rounded-full bg-gray-200 dark:bg-gray-800"
                      onClick={() => avatarInput.current?.click()}
                    >
                      {profile.avatar_url ? (
                        <img src={profile.avatar_url} alt="" className="h-full w-full object-cover" />
                      ) : (
                        <div className="flex h-full w-full items-center justify-center text-xl font-bold text-gray-500 dark:text-gray-400">
                          {profile.display_name.charAt(0).toUpperCase()}
                        </div>
                      )}
                      <div className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
                        <CameraIcon className="h-5 w-5 text-white" />
                      </div>
                    </div>
                    <input ref={avatarInput} type="file" accept="image/*" className="hidden" onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) uploadAvatar(f);
                    }} />
                    <div>
                      <p className="font-medium text-gray-900 dark:text-white">{profile.display_name}</p>
                      <p className="text-sm text-gray-500 dark:text-gray-400">{profile.email}</p>
                    </div>
                  </div>

                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Anzeigename</label>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <input
                        value={displayName}
                        onChange={(e) => setDisplayName(e.target.value)}
                        className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                      />
                      <button
                        onClick={saveProfile}
                        disabled={!displayName.trim() || !profileEmail.trim() || (displayName === profile.display_name && profileEmail === profile.email)}
                        className="shrink-0 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-40"
                      >
                        Speichern
                      </button>
                    </div>
                    {profileMsg && (
                      <p className={`mt-1 text-xs ${profileMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>{profileMsg.text}</p>
                    )}
                  </div>

                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">E-Mail</label>
                    <input
                      value={profileEmail}
                      onChange={e => setProfileEmail(e.target.value)}
                      type="email"
                      className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                    />
                  </div>
                </div>
              </section>

              <section>
                <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Passwort ändern</h2>
                <div className="space-y-3">
                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Aktuelles Passwort</label>
                    <input
                      type="password"
                      value={currentPw}
                      onChange={(e) => setCurrentPw(e.target.value)}
                      className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Neues Passwort</label>
                    <input
                      type="password"
                      value={newPw}
                      onChange={(e) => setNewPw(e.target.value)}
                      className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Passwort bestätigen</label>
                    <input
                      type="password"
                      value={confirmPw}
                      onChange={(e) => setConfirmPw(e.target.value)}
                      className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                    />
                  </div>
                  <div className="flex items-center gap-3">
                    <button
                      onClick={changePassword}
                      disabled={!currentPw || !newPw || !confirmPw}
                      className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-40"
                    >
                      Passwort ändern
                    </button>
                    {pwMsg && (
                      <p className={`text-sm ${pwMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>{pwMsg.text}</p>
                    )}
                  </div>
                </div>
              </section>

              {profile.role === 'owner' && (
                <section>
                  <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Zwei-Faktor-Authentifizierung (MFA)</h2>

                  {mfaEnabled && !mfaProvUri && (
                    <div className="space-y-3">
                      <div className="flex items-center gap-2 rounded-lg bg-green-50 px-3 py-2 dark:bg-green-950/40">
                        <span className="inline-block h-2 w-2 rounded-full bg-green-500" />
                        <span className="text-sm font-medium text-green-800 dark:text-green-300">MFA ist aktiv</span>
                      </div>
                      <p className="text-sm text-gray-600 dark:text-gray-400">
                        Zum Deaktivieren den aktuellen TOTP-Code eingeben:
                      </p>
                      <input
                        type="text"
                        inputMode="numeric"
                        maxLength={6}
                        placeholder="6-stelliger Code"
                        value={mfaCode}
                        onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ''))}
                        className="w-40 rounded-lg border px-3 py-2 text-center font-mono text-lg tracking-widest dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                      />
                      <div className="flex items-center gap-3">
                        <button
                          onClick={disableMfa}
                          disabled={mfaLoading || mfaCode.length !== 6}
                          className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-700 disabled:opacity-40"
                        >
                          MFA deaktivieren
                        </button>
                        {mfaMsg && (
                          <p className={`text-sm ${mfaMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>{mfaMsg.text}</p>
                        )}
                      </div>
                    </div>
                  )}

                  {!mfaEnabled && !mfaProvUri && (
                    <div className="space-y-3">
                      <p className="text-sm text-gray-600 dark:text-gray-400">
                        Schütze dein Konto mit einer Authenticator-App (Google Authenticator, Authy, 1Password etc.).
                      </p>
                      <button
                        onClick={startMfaSetup}
                        disabled={mfaLoading}
                        className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-40"
                      >
                        {mfaLoading ? 'Wird eingerichtet…' : 'MFA aktivieren'}
                      </button>
                      {mfaMsg && (
                        <p className={`text-sm ${mfaMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>{mfaMsg.text}</p>
                      )}
                    </div>
                  )}

                  {mfaProvUri && (
                    <div className="space-y-4">
                      <p className="text-sm text-gray-600 dark:text-gray-400">
                        Scanne den QR-Code mit deiner Authenticator-App:
                      </p>
                      <div className="inline-block rounded-xl border bg-white p-4 shadow-sm dark:border-gray-700">
                        <QRCodeSVG value={mfaProvUri} size={200} level="M" />
                      </div>
                      {mfaSecret && (
                        <div>
                          <p className="text-xs text-gray-500 dark:text-gray-400">Oder manuell eingeben:</p>
                          <code className="mt-1 block select-all rounded bg-gray-100 px-3 py-1.5 font-mono text-sm dark:bg-gray-800">
                            {mfaSecret}
                          </code>
                        </div>
                      )}
                      <div className="space-y-2">
                        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                          Bestätigungscode aus der App:
                        </label>
                        <input
                          type="text"
                          inputMode="numeric"
                          maxLength={6}
                          placeholder="000000"
                          value={mfaCode}
                          onChange={(e) => setMfaCode(e.target.value.replace(/\D/g, ''))}
                          className="w-40 rounded-lg border px-3 py-2 text-center font-mono text-lg tracking-widest dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                          autoComplete="one-time-code"
                        />
                      </div>
                      <div className="flex items-center gap-3">
                        <button
                          onClick={verifyMfa}
                          disabled={mfaLoading || mfaCode.length !== 6}
                          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-40"
                        >
                          {mfaLoading ? 'Wird verifiziert…' : 'Code verifizieren & aktivieren'}
                        </button>
                        <button
                          onClick={() => { setMfaProvUri(null); setMfaSecret(null); setMfaCode(''); }}
                          className="rounded-lg border px-4 py-2 text-sm text-gray-700 transition-colors hover:bg-gray-100 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
                        >
                          Abbrechen
                        </button>
                      </div>
                      {mfaMsg && (
                        <p className={`text-sm ${mfaMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>{mfaMsg.text}</p>
                      )}
                    </div>
                  )}
                </section>
              )}
            </>
          )}

          {/* ── Erscheinungsbild ── */}
          {tab === 'display' && (
            <section>
              <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Erscheinungsbild</h2>
              <div className="space-y-6">

                {/* Firmenlogo - jetzt prominenter */}
                <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-4 dark:border-indigo-900 dark:bg-indigo-950/30">
                  <label className="mb-1 block text-sm font-semibold text-gray-900 dark:text-white">Firmenlogo / App-Icon</label>
                  <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
                    Wird oben links in der Sidebar als App-Logo angezeigt. Ideal: quadratisches Bild (z.B. 128×128 px).
                  </p>
                  <div className="flex items-center gap-3">
                    <div
                      className="group relative flex h-14 w-14 cursor-pointer items-center justify-center overflow-hidden rounded-xl border-2 border-dashed border-indigo-300 bg-white dark:border-indigo-700 dark:bg-gray-800"
                      onClick={() => logoInput.current?.click()}
                    >
                      {settings.app_logo_url ? (
                        <img src={settings.app_logo_url} alt="" className="h-full w-full object-cover" />
                      ) : (
                        <span className="text-2xl font-bold text-gray-300 dark:text-gray-600">T</span>
                      )}
                      <div className="absolute inset-0 flex items-center justify-center bg-black/40 opacity-0 transition-opacity group-hover:opacity-100">
                        <CameraIcon className="h-5 w-5 text-white" />
                      </div>
                    </div>
                    <input ref={logoInput} type="file" accept="image/*" className="hidden" onChange={async (e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      const form = new FormData();
                      form.append('file', file);
                      try {
                        const res = await fetch('/api/uploads/icons', {
                          method: 'POST',
                          headers: { Authorization: `Bearer ${localStorage.getItem('taskpilot_token')}` },
                          body: form,
                        });
                        if (!res.ok) {
                          const err = await res.text().catch(() => '');
                          alert(`Upload fehlgeschlagen: ${res.status} ${err || res.statusText}`);
                          return;
                        }
                        const { url } = await res.json();
                        await updateSetting('app_logo_url', url);
                      } catch (e) {
                        alert(`Upload fehlgeschlagen: ${e instanceof Error ? e.message : 'Unbekannter Fehler'}`);
                      }
                    }} />
                    <div className="flex flex-col gap-1">
                      <button
                        onClick={() => logoInput.current?.click()}
                        className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-indigo-700"
                      >
                        Logo hochladen
                      </button>
                      {settings.app_logo_url && (
                        <button
                          onClick={() => updateSetting('app_logo_url', null)}
                          className="rounded-lg px-3 py-1.5 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                        >
                          Entfernen
                        </button>
                      )}
                    </div>
                  </div>
                </div>

                {/* Task-Anzahl in Spalten */}
                <div className="flex items-center justify-between rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <div>
                    <p className="font-medium text-gray-900 dark:text-white">Task-Anzahl in Spalten</p>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Zeigt die Anzahl Aufgaben pro Spalte in Agenda und Projekt-Boards an
                    </p>
                  </div>
                  <button
                    onClick={() => updateSetting('show_column_count', !(settings.show_column_count ?? false))}
                    className={`relative h-6 w-11 rounded-full transition-colors ${(settings.show_column_count ?? false) ? 'bg-indigo-600' : 'bg-gray-300 dark:bg-gray-600'}`}
                  >
                    <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${(settings.show_column_count ?? false) ? 'left-[22px]' : 'left-0.5'}`} />
                  </button>
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">Task-Detail Ansicht</label>
                  <div className="flex gap-2">
                    {([
                      { value: 'modal', label: 'Modal', icon: <ModalIcon className="h-4 w-4" /> },
                      { value: 'panel', label: 'Seitenpanel', icon: <PanelIcon className="h-4 w-4" /> },
                      { value: 'fullscreen', label: 'Vollbild', icon: <FullscreenIcon className="h-4 w-4" /> },
                    ] as const).map((opt) => (
                      <button
                        key={opt.value}
                        onClick={() => updateSetting('task_detail_mode', opt.value)}
                        className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
                          (settings.task_detail_mode || 'modal') === opt.value
                            ? 'bg-indigo-50 text-indigo-700 ring-1 ring-indigo-200 dark:bg-indigo-950 dark:text-indigo-300 dark:ring-indigo-800'
                            : 'bg-gray-50 text-gray-600 hover:bg-gray-100 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                        }`}
                      >
                        {opt.icon}
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">Sidebar-Farbe</label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Hintergrundfarbe für die Sidebar (passt sich an Light/Dark an).
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {SIDEBAR_PALETTE.map((c) => (
                      <button
                        key={c.key}
                        onClick={() => updateSetting('sidebar_color', c.key === 'default' ? null : c.key)}
                        className={`flex h-10 w-10 items-center justify-center rounded-lg border-2 transition-all ${
                          (settings.sidebar_color || 'default') === c.key
                            ? 'border-indigo-500 ring-2 ring-indigo-200 dark:ring-indigo-800'
                            : 'border-gray-200 dark:border-gray-700'
                        }`}
                        title={c.label}
                      >
                        <span className={`h-6 w-6 rounded-md ${c.swatch}`} />
                      </button>
                    ))}
                  </div>
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">Agenda-Hintergrund</label>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {settings.agenda_background_url ? 'Hintergrund gesetzt' : 'Kein Hintergrund gesetzt'} — kann in der Agenda-Ansicht geändert werden.
                  </p>
                  {settings.agenda_background_url && (
                    <button
                      onClick={() => updateSetting('agenda_background_url', null)}
                      className="mt-2 rounded-lg px-3 py-1.5 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                    >
                      Hintergrund entfernen
                    </button>
                  )}
                </div>
              </div>
            </section>
          )}

          {/* ── Cockpit ── */}
          {tab === 'cockpit' && (
            <section>
              <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Cockpit-Einstellungen</h2>
              <div className="space-y-6">

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">Cockpit-Hintergrund</label>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {settings.cockpit_background_url ? 'Hintergrund gesetzt' : 'Kein Hintergrund gesetzt'} — kann auch in der Cockpit-Ansicht geändert werden.
                  </p>
                  {settings.cockpit_background_url && (
                    <button
                      onClick={() => updateSetting('cockpit_background_url', null)}
                      className="mt-2 rounded-lg px-3 py-1.5 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 dark:text-red-400 dark:hover:bg-red-950"
                    >
                      Hintergrund entfernen
                    </button>
                  )}
                </div>

                <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-4 dark:border-indigo-900 dark:bg-indigo-950/30">
                  <label className="mb-1 block text-sm font-semibold text-gray-900 dark:text-white">Kalender-Kategorien ausblenden</label>
                  <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
                    Termine mit diesen Kategorien werden im Cockpit nicht angezeigt. Mehrere Kategorien mit Komma trennen (z.B. «Transfer, Privat, Lunch»).
                  </p>
                  <input
                    value={settings.cockpit_calendar_exclude_categories ?? 'Transfer, Privat'}
                    onChange={(e) => updateSetting('cockpit_calendar_exclude_categories', e.target.value || null)}
                    placeholder="z.B. Transfer, Privat, Lunch"
                    className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                  />
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {(settings.cockpit_calendar_exclude_categories ?? 'Transfer, Privat').split(',').map((cat, i) => {
                      const trimmed = cat.trim();
                      if (!trimmed) return null;
                      return (
                        <span key={i} className="inline-flex items-center gap-1 rounded-full bg-gray-200 px-2.5 py-0.5 text-xs font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                          {trimmed}
                          <button
                            onClick={() => {
                              const cats = (settings.cockpit_calendar_exclude_categories ?? 'Transfer, Privat')
                                .split(',').map(c => c.trim()).filter(c => c && c !== trimmed);
                              updateSetting('cockpit_calendar_exclude_categories', cats.join(', ') || null);
                            }}
                            className="ml-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                          >×</button>
                        </span>
                      );
                    })}
                  </div>
                </div>

                <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-4 dark:border-indigo-900 dark:bg-indigo-950/30">
                  <label className="mb-1 block text-sm font-semibold text-gray-900 dark:text-white">Kreditoren-Lieferanten ausblenden (Cockpit &amp; Finanzen)</label>
                  <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
                    Lieferanten mit diesen Namen werden in den Übersichten «Fällige Zahlungen» (Cockpit) und «InvoiceInsight» (Finanzen) ausgeblendet — die Kreditoren-Seite zeigt weiterhin alles. Mehrere mit Komma trennen (z.B. «Cursor, Anysphere»).
                  </p>
                  <input
                    value={settings.creditors_overview_exclude_vendors ?? 'Cursor'}
                    onChange={(e) => updateSetting('creditors_overview_exclude_vendors', e.target.value || null)}
                    placeholder="z.B. Cursor, Anysphere"
                    className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                  />
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {(settings.creditors_overview_exclude_vendors ?? 'Cursor').split(',').map((vendor, i) => {
                      const trimmed = vendor.trim();
                      if (!trimmed) return null;
                      return (
                        <span key={i} className="inline-flex items-center gap-1 rounded-full bg-gray-200 px-2.5 py-0.5 text-xs font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                          {trimmed}
                          <button
                            onClick={() => {
                              const vendors = (settings.creditors_overview_exclude_vendors ?? 'Cursor')
                                .split(',').map(v => v.trim()).filter(v => v && v !== trimmed);
                              updateSetting('creditors_overview_exclude_vendors', vendors.join(', ') || null);
                            }}
                            className="ml-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                          >×</button>
                        </span>
                      );
                    })}
                  </div>
                </div>

                <div className="flex items-center justify-between rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <div>
                    <p className="font-medium text-gray-900 dark:text-white">Private Termine ausblenden</p>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Termine mit Vertraulichkeit «Privat» (sensitivity) im Cockpit verbergen
                    </p>
                  </div>
                  <button
                    onClick={() => updateSetting('cockpit_calendar_hide_private', !(settings.cockpit_calendar_hide_private ?? true))}
                    className={`relative h-6 w-11 rounded-full transition-colors ${(settings.cockpit_calendar_hide_private ?? true) ? 'bg-indigo-600' : 'bg-gray-300 dark:bg-gray-600'}`}
                  >
                    <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${(settings.cockpit_calendar_hide_private ?? true) ? 'left-[22px]' : 'left-0.5'}`} />
                  </button>
                </div>

                {/* ── Briefings (Daily / Weekly / Monthly) ── */}
                <div className="rounded-xl border border-sky-200 bg-sky-50/50 p-4 dark:border-sky-900 dark:bg-sky-950/30">
                  <label className="mb-1 block text-sm font-semibold text-gray-900 dark:text-white">Automatische Briefings</label>
                  <p className="mb-4 text-xs text-gray-500 dark:text-gray-400">
                    Der Agent erstellt Briefings automatisch und zeigt sie zuoberst im Cockpit.
                    Ältere Briefings bleiben im Archiv (Agenten → Aufträge → Filter «Briefings»).
                  </p>
                  <div className="space-y-3">
                    {/* Daily */}
                    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800/60">
                      <button
                        onClick={() => updateSetting('briefing_daily_enabled', !(settings.briefing_daily_enabled ?? true))}
                        className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${(settings.briefing_daily_enabled ?? true) ? 'bg-indigo-600' : 'bg-gray-300 dark:bg-gray-600'}`}
                      >
                        <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${(settings.briefing_daily_enabled ?? true) ? 'left-[22px]' : 'left-0.5'}`} />
                      </button>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-900 dark:text-white">Tagesbriefing</p>
                        <p className="text-xs text-gray-500 dark:text-gray-400">Werktags — Termine, fällige Tasks, Triage seit gestern</p>
                      </div>
                      <input
                        type="time"
                        value={settings.briefing_daily_time ?? '06:30'}
                        onChange={(e) => updateSetting('briefing_daily_time', e.target.value || null)}
                        disabled={!(settings.briefing_daily_enabled ?? true)}
                        className="rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none disabled:opacity-40 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                      />
                      <button
                        onClick={() => generateBriefing('daily_briefing')}
                        disabled={briefingBusy !== null}
                        className="shrink-0 rounded-lg border border-sky-300 bg-white px-3 py-1.5 text-sm font-medium text-sky-700 transition-colors hover:bg-sky-50 disabled:opacity-40 dark:border-sky-700 dark:bg-transparent dark:text-sky-300 dark:hover:bg-sky-950/40"
                      >
                        {briefingBusy === 'daily_briefing' ? 'Erstelle…' : 'Jetzt generieren'}
                      </button>
                    </div>
                    {/* Weekly */}
                    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800/60">
                      <button
                        onClick={() => updateSetting('briefing_weekly_enabled', !(settings.briefing_weekly_enabled ?? true))}
                        className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${(settings.briefing_weekly_enabled ?? true) ? 'bg-indigo-600' : 'bg-gray-300 dark:bg-gray-600'}`}
                      >
                        <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${(settings.briefing_weekly_enabled ?? true) ? 'left-[22px]' : 'left-0.5'}`} />
                      </button>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-900 dark:text-white">Wochenbriefing</p>
                        <p className="text-xs text-gray-500 dark:text-gray-400">Projekte, Kapazität, Plan vs. Ist, kommende Woche</p>
                      </div>
                      <select
                        value={settings.briefing_weekly_day ?? 6}
                        onChange={(e) => updateSetting('briefing_weekly_day', Number(e.target.value))}
                        disabled={!(settings.briefing_weekly_enabled ?? true)}
                        className="rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none disabled:opacity-40 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                      >
                        {['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'].map((d, i) => (
                          <option key={i} value={i}>{d}</option>
                        ))}
                      </select>
                      <input
                        type="time"
                        value={settings.briefing_weekly_time ?? '17:00'}
                        onChange={(e) => updateSetting('briefing_weekly_time', e.target.value || null)}
                        disabled={!(settings.briefing_weekly_enabled ?? true)}
                        className="rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none disabled:opacity-40 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                      />
                      <button
                        onClick={() => generateBriefing('weekly_briefing')}
                        disabled={briefingBusy !== null}
                        className="shrink-0 rounded-lg border border-sky-300 bg-white px-3 py-1.5 text-sm font-medium text-sky-700 transition-colors hover:bg-sky-50 disabled:opacity-40 dark:border-sky-700 dark:bg-transparent dark:text-sky-300 dark:hover:bg-sky-950/40"
                      >
                        {briefingBusy === 'weekly_briefing' ? 'Erstelle…' : 'Jetzt generieren'}
                      </button>
                    </div>
                    {/* Monthly */}
                    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800/60">
                      <button
                        onClick={() => updateSetting('briefing_monthly_enabled', !(settings.briefing_monthly_enabled ?? true))}
                        className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${(settings.briefing_monthly_enabled ?? true) ? 'bg-indigo-600' : 'bg-gray-300 dark:bg-gray-600'}`}
                      >
                        <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${(settings.briefing_monthly_enabled ?? true) ? 'left-[22px]' : 'left-0.5'}`} />
                      </button>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-900 dark:text-white">Monatsbriefing</p>
                        <p className="text-xs text-gray-500 dark:text-gray-400">Am letzten Kalendertag — Rückblick, Forecast, nächste zwei Monate</p>
                      </div>
                      <input
                        type="time"
                        value={settings.briefing_monthly_time ?? '17:00'}
                        onChange={(e) => updateSetting('briefing_monthly_time', e.target.value || null)}
                        disabled={!(settings.briefing_monthly_enabled ?? true)}
                        className="rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none disabled:opacity-40 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                      />
                      <button
                        onClick={() => generateBriefing('monthly_briefing')}
                        disabled={briefingBusy !== null}
                        className="shrink-0 rounded-lg border border-sky-300 bg-white px-3 py-1.5 text-sm font-medium text-sky-700 transition-colors hover:bg-sky-50 disabled:opacity-40 dark:border-sky-700 dark:bg-transparent dark:text-sky-300 dark:hover:bg-sky-950/40"
                      >
                        {briefingBusy === 'monthly_briefing' ? 'Erstelle…' : 'Jetzt generieren'}
                      </button>
                    </div>
                  </div>
                  {briefingMsg && (
                    <div className={`mt-3 flex flex-wrap items-center gap-2 text-sm ${briefingMsg.type === 'ok' ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                      <span>{briefingMsg.text}</span>
                      {briefingMsg.type === 'ok' && (
                        <Link to="/agenten?type=briefing" className="font-medium text-sky-700 underline hover:text-sky-800 dark:text-sky-300">
                          Im Archiv ansehen
                        </Link>
                      )}
                    </div>
                  )}
                  <p className="mt-2 text-xs text-gray-400 dark:text-gray-500">
                    «Jetzt generieren» erstellt sofort ein Briefing (unabhängig vom Zeitplan) — ideal zum Testen der Inhalte.
                  </p>
                </div>

                {/* ── Follow-up-Erkennung ── */}
                <div className="rounded-xl border border-rose-200 bg-rose-50/50 p-4 dark:border-rose-900 dark:bg-rose-950/30">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <label className="block text-sm font-semibold text-gray-900 dark:text-white">Follow-up-Erkennung</label>
                      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        Gesendete E-Mails ohne Antwort werden nach der Wartezeit als
                        Nachfass-Vorschlag im Cockpit angezeigt (mit Follow-up-Badge).
                      </p>
                    </div>
                    <button
                      onClick={() => updateSetting('followup_enabled', !(settings.followup_enabled ?? true))}
                      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${(settings.followup_enabled ?? true) ? 'bg-indigo-600' : 'bg-gray-300 dark:bg-gray-600'}`}
                    >
                      <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${(settings.followup_enabled ?? true) ? 'left-[22px]' : 'left-0.5'}`} />
                    </button>
                  </div>
                  <div className="mt-3 flex items-center gap-2">
                    <label className="text-sm text-gray-700 dark:text-gray-300">Wartezeit:</label>
                    <input
                      type="number"
                      min={1}
                      max={30}
                      value={settings.followup_wait_days ?? 5}
                      onChange={(e) => updateSetting('followup_wait_days', Number(e.target.value) || 5)}
                      disabled={!(settings.followup_enabled ?? true)}
                      className="w-20 rounded-lg border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none disabled:opacity-40 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                    />
                    <span className="text-sm text-gray-500 dark:text-gray-400">Arbeitstage</span>
                  </div>
                </div>

              </div>
            </section>
          )}

          {/* ── Finanzen / Cashflow-Prognose ── */}
          {tab === 'finance' && isOwner && (
            <section>
              <h2 className="mb-1 text-lg font-semibold text-gray-900 dark:text-white">Finanzen</h2>
              <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
                Parameter für die Cashflow-Prognose. Stundensätze kommen primär aus Toggl;
                der Default-Satz greift nur für noch nicht zugesagte (vorläufige) Kapazitätsprojekte.
              </p>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <label className="block text-sm font-medium text-gray-900 dark:text-white">
                    Default-Stundensatz (CHF/h, exkl. MwSt)
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Für vorläufige Projekte ohne hinterlegten oder Toggl-Satz.
                  </p>
                  <input
                    type="number" min="0" step="10"
                    defaultValue={settings.default_hourly_rate ?? ''}
                    placeholder="240"
                    onBlur={(e) => updateSetting('default_hourly_rate', e.target.value === '' ? null : parseFloat(e.target.value))}
                    className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                </div>

                <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <label className="block text-sm font-medium text-gray-900 dark:text-white">
                    Pipeline-Wahrscheinlichkeit (%)
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Gewichtung vorläufiger (noch nicht zugesagter) Projekte. Empfohlen 70–80%.
                  </p>
                  <input
                    type="number" min="0" max="100" step="5"
                    defaultValue={settings.forecast_pipeline_weight != null ? Math.round(settings.forecast_pipeline_weight * 100) : ''}
                    placeholder="75"
                    onBlur={(e) => updateSetting('forecast_pipeline_weight', e.target.value === '' ? null : Math.min(1, Math.max(0, parseFloat(e.target.value) / 100)))}
                    className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                </div>

                <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <label className="block text-sm font-medium text-gray-900 dark:text-white">
                    Auffüll-Horizont (Monate)
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Ab wann ferne Monate voll auf das historische Niveau aufgefüllt werden
                    (nahe Monate zählen primär das Gebuchte).
                  </p>
                  <input
                    type="number" min="1" max="12" step="1"
                    defaultValue={settings.forecast_fill_horizon_months ?? ''}
                    placeholder="4"
                    onBlur={(e) => updateSetting('forecast_fill_horizon_months', e.target.value === '' ? null : Math.max(1, parseInt(e.target.value, 10)))}
                    className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                </div>

                <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <label className="block text-sm font-medium text-gray-900 dark:text-white">
                    MwSt-Satz / Fakturierungssatz (%)
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Normalsatz für die Brutto-Hochrechnung der Kapazität (CH typ. 8.1%).
                    Die MWST-Methode (Saldosatz/effektiv) wird unter «Finanzanalysen» gesetzt.
                  </p>
                  <input
                    type="number" min="0" max="100" step="0.1"
                    defaultValue={settings.forecast_vat_rate != null ? +(settings.forecast_vat_rate * 100).toFixed(2) : ''}
                    placeholder="8.1"
                    onBlur={(e) => updateSetting('forecast_vat_rate', e.target.value === '' ? null : Math.max(0, parseFloat(e.target.value) / 100))}
                    className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                </div>

                <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <label className="block text-sm font-medium text-gray-900 dark:text-white">
                    Umsatz-Jahresziel (CHF, brutto)
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Zielumsatz für das laufende Jahr. Zeigt im Cockpit die Lücke zum Ziel
                    und im Cashflow-Chart die Monatsziel-Linie (Ziel / 12). Leer = kein Ziel.
                  </p>
                  <input
                    type="number" min="0" step="10000"
                    defaultValue={settings.annual_revenue_goal ?? ''}
                    placeholder="z.B. 360000"
                    onBlur={(e) => updateSetting('annual_revenue_goal', e.target.value === '' ? null : Math.max(0, parseFloat(e.target.value)))}
                    className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                </div>

                <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                  <label className="block text-sm font-medium text-gray-900 dark:text-white">
                    Mindest-Liquidität (CHF)
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Schwelle für den Banksaldo. Erscheint als Warnlinie im Cashflow-Chart und
                    färbt die Cashflow-KPI rot, wenn der Saldo darunter fällt. Leer = keine Schwelle.
                  </p>
                  <input
                    type="number" min="0" step="5000"
                    defaultValue={settings.min_liquidity ?? ''}
                    placeholder="z.B. 50000"
                    onBlur={(e) => updateSetting('min_liquidity', e.target.value === '' ? null : Math.max(0, parseFloat(e.target.value)))}
                    className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  />
                </div>
              </div>
            </section>
          )}

          {/* ── Finanzanalysen ── */}
          {tab === 'finance_analysis' && isOwner && (
            <section className="space-y-8">
              <div>
                <h2 className="mb-1 text-lg font-semibold text-gray-900 dark:text-white">MWST-Methode</h2>
                <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
                  Bestimmt, wie der Netto-Umsatz aus dem fakturierten Brutto-Umsatz abgeleitet wird
                  (Basis für Marge, EBITDA und Personalquote).
                </p>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                    <label className="block text-sm font-medium text-gray-900 dark:text-white">
                      Abrechnungsmethode
                    </label>
                    <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                      Saldosteuersatz: Normalsatz fakturiert, nur Saldosatz an die ESTV abgeliefert
                      (CH-Standard für viele KMU-Dienstleister).
                    </p>
                    <select
                      value={settings.vat_method ?? 'saldo'}
                      onChange={(e) => updateSetting('vat_method', e.target.value)}
                      className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    >
                      <option value="saldo">Saldosteuersatz</option>
                      <option value="effektiv">Effektive Methode</option>
                      <option value="none">Keine MWST</option>
                    </select>
                  </div>

                  <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                    <label className="block text-sm font-medium text-gray-900 dark:text-white">
                      Saldosteuersatz (%)
                    </label>
                    <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                      Branchenabhängig (Beratung/Dienstleistung typ. 6.2%). Nur bei Methode «Saldosteuersatz» relevant.
                    </p>
                    <input
                      type="number" min="0" max="100" step="0.1"
                      defaultValue={settings.vat_saldo_rate != null ? +(settings.vat_saldo_rate * 100).toFixed(2) : ''}
                      placeholder="6.2"
                      disabled={(settings.vat_method ?? 'saldo') !== 'saldo'}
                      onBlur={(e) => updateSetting('vat_saldo_rate', e.target.value === '' ? null : Math.max(0, parseFloat(e.target.value) / 100))}
                      className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    />
                  </div>
                </div>
              </div>

              <div>
                <h2 className="mb-1 text-lg font-semibold text-gray-900 dark:text-white">Steuer-Kontext</h2>
                <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
                  Für treffende Steuer- und Lohn-/Dividenden-Empfehlungen. Ohne Angabe rechnet die
                  Analyse mit Annahmen und weist dies als Datenlücke aus.
                </p>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                    <label className="block text-sm font-medium text-gray-900 dark:text-white">
                      Sitz-/Wohnkanton
                    </label>
                    <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                      Bestimmt Teilbesteuerung der Dividende und Grenzsteuersatz (z.B. Bern).
                    </p>
                    <input
                      type="text"
                      defaultValue={settings.tax_canton ?? ''}
                      placeholder="z.B. Bern"
                      onBlur={(e) => updateSetting('tax_canton', e.target.value.trim() === '' ? null : e.target.value.trim())}
                      className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    />
                  </div>

                  <div className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
                    <label className="block text-sm font-medium text-gray-900 dark:text-white">
                      Zivilstand
                    </label>
                    <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                      Beeinflusst den privaten Steuertarif.
                    </p>
                    <select
                      value={settings.civil_status ?? ''}
                      onChange={(e) => updateSetting('civil_status', e.target.value === '' ? null : e.target.value)}
                      className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    >
                      <option value="">– nicht angegeben –</option>
                      <option value="ledig">ledig</option>
                      <option value="verheiratet">verheiratet / eingetragene Partnerschaft</option>
                    </select>
                  </div>
                </div>
              </div>

              <FinanceDocumentsManager />
            </section>
          )}

          {/* ── Integrationen ── */}
          {tab === 'integrations' && isOwner && (
            <section>
              <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Integrationen</h2>
              <div className="space-y-6">

                {/* Integrations-Steuerung */}
                <div className={`rounded-xl border p-5 ${
                  !integrationsActiveEnv
                    ? 'border-amber-300 bg-amber-50/50 dark:border-amber-800 dark:bg-amber-950/30'
                    : triageEnabled
                      ? 'border-emerald-200 bg-emerald-50/50 dark:border-emerald-900 dark:bg-emerald-950/30'
                      : 'border-gray-200 bg-gray-50/50 dark:border-gray-700 dark:bg-gray-800/50'
                }`}>
                  <div className="flex items-center justify-between">
                    <div>
                      <h3 className="text-sm font-semibold text-gray-900 dark:text-white">
                        E-Mail- & Chat-Triage
                      </h3>
                      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                        Automatisches Polling und Klassifikation von E-Mails und Teams-Chats.
                        {!integrationsActiveEnv && (
                          <span className="ml-1 font-medium text-amber-600 dark:text-amber-400">
                            Auf Env-Ebene gesperrt (TP_INTEGRATIONS_ACTIVE=false, {appEnv.toUpperCase()}).
                          </span>
                        )}
                      </p>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className={`text-xs font-medium ${
                        !integrationsActiveEnv ? 'text-amber-600 dark:text-amber-400' :
                        triageEnabled ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-400'
                      }`}>
                        {!integrationsActiveEnv ? 'Gesperrt' : triageEnabled ? 'Aktiv' : 'Inaktiv'}
                      </span>
                      <button
                        onClick={async () => {
                          if (!integrationsActiveEnv) return;
                          const next = !triageEnabled;
                          try {
                            await api.patch('/api/settings/integrations/triage-toggle', { triage_enabled: next });
                            setTriageEnabled(next);
                          } catch { /* */ }
                        }}
                        disabled={!integrationsActiveEnv}
                        className={`relative h-6 w-11 rounded-full transition-colors ${
                          !integrationsActiveEnv ? 'cursor-not-allowed bg-gray-300 opacity-50 dark:bg-gray-600' :
                          triageEnabled ? 'bg-emerald-600' : 'bg-gray-300 dark:bg-gray-600'
                        }`}
                      >
                        <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${triageEnabled ? 'left-[22px]' : 'left-0.5'}`} />
                      </button>
                    </div>
                  </div>
                </div>

                <div className="rounded-xl border border-orange-200 bg-orange-50/50 p-5 dark:border-orange-900 dark:bg-orange-950/30">
                  <div className="mb-3 flex items-center gap-3">
                    <PipedriveIcon className="h-8 w-8" />
                    <div>
                      <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Pipedrive CRM</h3>
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        Deals, Leads, Kontakte und Aktivitäten synchronisieren
                      </p>
                    </div>
                  </div>

                  <div className="space-y-3">
                    <div>
                      <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">API-Token</label>
                      <input
                        type="password"
                        value={pdToken}
                        onChange={(e) => setPdToken(e.target.value)}
                        placeholder="Pipedrive API-Token eingeben"
                        className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                      />
                      <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">
                        Zu finden unter: Pipedrive → Einstellungen → Persönliche Einstellungen → API
                      </p>
                    </div>

                    <div>
                      <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Firmen-Domain</label>
                      <div className="flex items-center gap-0">
                        <input
                          value={pdDomain}
                          onChange={(e) => setPdDomain(e.target.value)}
                          className="rounded-l-lg border border-r-0 border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                        />
                        <span className="rounded-r-lg border border-gray-300 bg-gray-50 px-3 py-2 text-sm text-gray-500 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-400">
                          .pipedrive.com
                        </span>
                      </div>
                    </div>
                  </div>

                  <div className="mt-4 flex items-center gap-3">
                    <button
                      onClick={saveIntegrations}
                      className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700"
                    >
                      Speichern
                    </button>
                    <button
                      onClick={testPipedrive}
                      disabled={pdTesting}
                      className="rounded-lg bg-orange-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-orange-700 disabled:opacity-50"
                    >
                      {pdTesting ? 'Teste...' : 'Verbindung testen'}
                    </button>
                    {pdMsg && (
                      <span className={`text-sm ${pdMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>
                        {pdMsg.text}
                      </span>
                    )}
                  </div>
                </div>

              </div>

              {/* ── Toggl Track ── */}
              <div className="mt-6 rounded-xl border border-gray-200 p-5 dark:border-gray-700">
                <div className="mb-4 flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-pink-100 dark:bg-pink-900/30">
                    <svg className="h-5 w-5 text-pink-600 dark:text-pink-400" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Toggl Track</h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400">Zeiterfassung, Kunden und Projekte</p>
                  </div>
                </div>
                <div className="space-y-3">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">API-Token</label>
                    <input type="password" className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white" value={togglToken} onChange={(e) => setTogglToken(e.target.value)} placeholder="Toggl API-Token eingeben" />
                    <p className="mt-1 text-[10px] text-gray-400">Zu finden unter: Toggl Track → Profile Settings → API Token</p>
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">Workspace-ID</label>
                    <input type="text" className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white" value={togglWsId} onChange={(e) => setTogglWsId(e.target.value)} placeholder="Wird nach Test automatisch gesetzt" />
                  </div>
                  <div className="flex items-center gap-3 pt-2">
                    <button onClick={saveIntegrations} className="rounded-lg bg-gray-200 px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600">Speichern</button>
                    <button onClick={testToggl} disabled={togglTesting} className="rounded-lg bg-pink-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-pink-700 disabled:opacity-50">
                      {togglTesting ? 'Teste...' : 'Verbindung testen'}
                    </button>
                    {togglMsg && (
                      <span className={`text-sm ${togglMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>{togglMsg.text}</span>
                    )}
                  </div>
                </div>
              </div>

              {/* ── Bexio ── */}
              <div className="mt-6 rounded-xl border border-gray-200 p-5 dark:border-gray-700">
                <div className="mb-4 flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-blue-100 dark:bg-blue-900/30">
                    <svg className="h-5 w-5 text-blue-600 dark:text-blue-400" fill="currentColor" viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-7 3c1.93 0 3.5 1.57 3.5 3.5S13.93 13 12 13s-3.5-1.57-3.5-3.5S10.07 6 12 6zm7 13H5v-.23c0-.62.28-1.2.76-1.58C7.47 15.82 9.64 15 12 15s4.53.82 6.24 2.19c.48.38.76.97.76 1.58V19z"/></svg>
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Bexio</h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400">Buchhaltung, Kontakte und Aufträge</p>
                  </div>
                </div>
                <div className="space-y-3">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-400">API-Token</label>
                    <input type="password" className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white" value={bexioToken} onChange={(e) => setBexioToken(e.target.value)} placeholder="Bexio API-Token eingeben" />
                    <p className="mt-1 text-[10px] text-gray-400">Personal Access Token (PAT) erstellen unter: developer.bexio.com/pat — gültig für 6 Monate, danach erneuern.</p>
                  </div>
                  <div className="flex items-center gap-3 pt-2">
                    <button onClick={saveIntegrations} className="rounded-lg bg-gray-200 px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600">Speichern</button>
                    <button onClick={testBexio} disabled={bexioTesting} className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:opacity-50">
                      {bexioTesting ? 'Teste...' : 'Verbindung testen'}
                    </button>
                    {bexioMsg && (
                      <span className={`text-sm ${bexioMsg.type === 'ok' ? 'text-green-600' : bexioMsg.type === 'warn' ? 'text-amber-600' : 'text-red-600'}`}>{bexioMsg.text}</span>
                    )}
                  </div>
                </div>
              </div>

              {/* ── Browser Extension (LinkedIn Sync) ── */}
              <div className="mt-6 rounded-xl border border-emerald-200 bg-emerald-50/50 p-5 dark:border-emerald-900 dark:bg-emerald-950/30">
                <div className="mb-3 flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-100 dark:bg-emerald-900/30">
                    <svg className="h-5 w-5 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
                    </svg>
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Browser-Extension (LinkedIn Sync)</h3>
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      API-Key für die Chrome-Extension zur LinkedIn-Pipedrive-Synchronisation
                    </p>
                  </div>
                </div>

                <div className="space-y-3">
                  {extHasKey && !extApiKey && (
                    <div className="flex items-center gap-2 rounded-lg bg-emerald-100 p-3 dark:bg-emerald-900/40">
                      <svg className="h-4 w-4 text-emerald-600 dark:text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                      </svg>
                      <span className="text-sm text-emerald-700 dark:text-emerald-300">
                        API-Key aktiv{extKeyCreatedAt ? ` (erstellt: ${new Date(extKeyCreatedAt).toLocaleDateString('de-CH')})` : ''}
                      </span>
                    </div>
                  )}

                  {extApiKey && (
                    <div className="space-y-2">
                      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
                        Dein API-Key (nur jetzt sichtbar!)
                      </label>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 break-all rounded-lg border border-gray-300 bg-gray-50 p-2.5 font-mono text-xs text-gray-800 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200">
                          {extApiKey}
                        </code>
                        <button
                          onClick={copyExtApiKey}
                          className="shrink-0 rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-emerald-700"
                        >
                          {extCopied ? 'Kopiert!' : 'Kopieren'}
                        </button>
                      </div>
                      <p className="text-xs text-amber-600 dark:text-amber-400">
                        Dieser Key wird nur einmal angezeigt. Kopiere ihn jetzt in die Extension-Einstellungen.
                      </p>
                    </div>
                  )}

                  <div className="flex flex-wrap items-center gap-3 pt-1">
                    <button
                      onClick={generateExtApiKey}
                      disabled={extLoading}
                      className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-emerald-700 disabled:opacity-50"
                    >
                      {extLoading ? 'Generiere...' : extHasKey ? 'Neuen Key generieren' : 'API-Key generieren'}
                    </button>
                    {extHasKey && (
                      <button
                        onClick={revokeExtApiKey}
                        disabled={extLoading}
                        className="rounded-lg bg-red-100 px-4 py-2 text-sm font-medium text-red-700 transition-colors hover:bg-red-200 disabled:opacity-50 dark:bg-red-900/30 dark:text-red-400 dark:hover:bg-red-900/50"
                      >
                        Key widerrufen
                      </button>
                    )}
                  </div>

                  {extMsg && (
                    <p className={`text-sm ${extMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>
                      {extMsg.text}
                    </p>
                  )}

                  <p className="text-[10px] text-gray-400 dark:text-gray-500">
                    Der Key beginnt mit <code className="rounded bg-gray-100 px-1 dark:bg-gray-800">tpk_</code> und läuft nicht ab.
                    Beim Generieren eines neuen Keys wird der vorherige automatisch ungültig.
                  </p>
                </div>
              </div>

            </section>
          )}

          {/* ── E-Mail-Triage ── */}
          {tab === 'triage' && isOwner && (
            <section>
              <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">E-Mail-Triage</h2>
              <div className="space-y-6">

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Triage-Intervall: {triageInterval} Min.
                  </label>
                  <input
                    type="range"
                    min={1}
                    max={10}
                    value={triageInterval}
                    onChange={(e) => setTriageInterval(Number(e.target.value))}
                    className="w-full accent-indigo-600"
                  />
                  <div className="flex justify-between text-xs text-gray-400">
                    <span>1 Min.</span>
                    <span>10 Min.</span>
                  </div>
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Triage-Prompt (System-Prompt für die Klassifikation)
                  </label>
                  <textarea
                    value={triagePrompt}
                    onChange={(e) => setTriagePrompt(e.target.value)}
                    rows={12}
                    placeholder="Standard-Prompt wird verwendet wenn leer..."
                    className="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white font-mono"
                  />
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Ausgeblendete Posteingangs-Ordner
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Diese Ordner werden im Posteingang nicht angezeigt.
                  </p>
                  <div className="flex flex-wrap gap-2 mb-2">
                    {hiddenFolders.map(f => (
                      <span key={f} className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-3 py-1 text-xs font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                        {f}
                        <button
                          onClick={() => setHiddenFolders(prev => prev.filter(x => x !== f))}
                          className="ml-0.5 text-gray-400 hover:text-red-500"
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={hiddenFolderInput}
                      onChange={e => setHiddenFolderInput(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' && hiddenFolderInput.trim()) {
                          const name = hiddenFolderInput.trim();
                          if (!hiddenFolders.includes(name)) setHiddenFolders(prev => [...prev, name]);
                          setHiddenFolderInput('');
                        }
                      }}
                      placeholder="Ordnername eingeben..."
                      className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                    />
                    <button
                      onClick={() => {
                        const name = hiddenFolderInput.trim();
                        if (name && !hiddenFolders.includes(name)) setHiddenFolders(prev => [...prev, name]);
                        setHiddenFolderInput('');
                      }}
                      className="shrink-0 rounded-lg bg-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                    >
                      Hinzufügen
                    </button>
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <button
                    onClick={saveTriageSettings}
                    className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700"
                  >
                    Einstellungen speichern
                  </button>
                  <button
                    onClick={testTriage}
                    disabled={triageTesting}
                    className="rounded-lg bg-amber-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-amber-700 disabled:opacity-50"
                  >
                    {triageTesting ? 'Teste...' : 'Test-Triage (5 E-Mails)'}
                  </button>
                  {triageMsg && (
                    <span className={`text-sm ${triageMsg.type === 'ok' ? 'text-green-600' : 'text-red-600'}`}>
                      {triageMsg.text}
                    </span>
                  )}
                </div>

                {triageTestResults && triageTestResults.length > 0 && (
                  <div className="rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
                    <h3 className="px-4 py-2 text-sm font-semibold text-gray-700 bg-gray-50 dark:bg-gray-900 dark:text-gray-300">
                      Test-Ergebnisse
                    </h3>
                    <div className="divide-y divide-gray-200 dark:divide-gray-700">
                      {triageTestResults.map((r) => (
                        <div key={r.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                            r.triage_class === 'auto_reply' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300' :
                            r.triage_class === 'task' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' :
                            'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
                          }`}>
                            {r.triage_class}
                          </span>
                          <span className="flex-1 truncate text-gray-700 dark:text-gray-300">{r.subject}</span>
                          <span className="shrink-0 text-xs text-gray-400">{r.from_address}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </section>
          )}

          {/* ── Suchindex ── */}
          {tab === 'search_index' && isOwner && (
            <section>
              <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Suchindex</h2>
              <div className="space-y-6">
                <div className="rounded-lg border border-gray-200 bg-white/60 p-4 text-sm text-gray-600 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-300">
                  Die Vektorisierung läuft permanent als eigener Dienst und indexiert dein
                  vollständiges E-Mail-Archiv (alle Ordner ausser Junk und Gelöschte Elemente)
                  sowie deine OneDrive-Dokumente. Über die Ausschlussliste hältst du grosse,
                  themenfremde Ordner aus der Suche heraus.
                </div>

                {/* ── Verarbeitungsstatus (Live) ── */}
                <div className="rounded-lg border border-gray-200 bg-white/60 p-4 dark:border-gray-700 dark:bg-gray-800/40">
                  <div className="mb-3 flex items-center justify-between">
                    <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Verarbeitungsstatus</h3>
                    {(() => {
                      const st = indexStatus;
                      if (!st) return <span className="text-xs text-gray-400">lädt …</span>;
                      if (st.running && st.stale) {
                        return <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"><span className="h-1.5 w-1.5 rounded-full bg-amber-500" />Reagiert nicht</span>;
                      }
                      if (st.running) {
                        return <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-100 px-2.5 py-0.5 text-xs font-medium text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-500" />Läuft</span>;
                      }
                      if (st.last_error) {
                        return <span className="inline-flex items-center gap-1.5 rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-medium text-red-700 dark:bg-red-900/40 dark:text-red-300"><span className="h-1.5 w-1.5 rounded-full bg-red-500" />Problem</span>;
                      }
                      return <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"><span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />Bereit</span>;
                    })()}
                  </div>

                  {!indexStatus ? (
                    <p className="text-sm text-gray-400">Status wird geladen …</p>
                  ) : (
                    <div className="space-y-4 text-sm text-gray-600 dark:text-gray-300">
                      {/* Laufender Fortschritt */}
                      {indexStatus.running && (
                        <div className="space-y-3">
                          {indexStatus.phase === 'purge' && (
                            <p className="text-gray-500 dark:text-gray-400">Bereinige ausgeschlossene Pfade …</p>
                          )}
                          {indexStatus.phase === 'mails' && (
                            <div>
                              <div className="mb-1 flex items-center justify-between">
                                <span>E-Mail-Ordner{indexStatus.detail ? <span className="text-gray-400"> — {indexStatus.detail}</span> : null}</span>
                                <span className="tabular-nums text-gray-500 dark:text-gray-400">{indexStatus.folders_done} / {indexStatus.folders_total}</span>
                              </div>
                              <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                                <div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${indexStatus.folders_total > 0 ? Math.min(100, Math.round((indexStatus.folders_done / indexStatus.folders_total) * 100)) : 0}%` }} />
                              </div>
                            </div>
                          )}
                          {indexStatus.phase === 'documents' && (
                            <div>
                              <div className="mb-1 flex items-center justify-between">
                                <span>OneDrive-Dokumente{indexStatus.detail ? <span className="text-gray-400"> — {indexStatus.detail}</span> : null}</span>
                                <span className="tabular-nums text-gray-500 dark:text-gray-400">{indexStatus.docs_done} / {indexStatus.docs_total}</span>
                              </div>
                              <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                                <div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${indexStatus.docs_total > 0 ? Math.min(100, Math.round((indexStatus.docs_done / indexStatus.docs_total) * 100)) : 0}%` }} />
                              </div>
                            </div>
                          )}
                          {indexStatus.run_started_at && (
                            <p className="text-xs text-gray-400">Lauf gestartet {fmtRelative(indexStatus.run_started_at)}</p>
                          )}
                          {indexStatus.stale && (
                            <p className="text-xs text-amber-600 dark:text-amber-400">
                              Kein Lebenszeichen seit über 15 Minuten — der Indexer-Dienst hängt möglicherweise. Logs prüfen (<code>make logs-indexer</code>).
                            </p>
                          )}
                        </div>
                      )}

                      {/* Letzter Abschluss + nächste Prüfung */}
                      {!indexStatus.running && (
                        <div className="flex flex-wrap gap-x-8 gap-y-1">
                          <div>
                            <span className="text-gray-500 dark:text-gray-400">Zuletzt abgeschlossen: </span>
                            {indexStatus.run_finished_at
                              ? <span>{fmtDateTime(indexStatus.run_finished_at)} <span className="text-gray-400">({fmtRelative(indexStatus.run_finished_at)})</span></span>
                              : <span className="text-gray-400">noch kein Lauf abgeschlossen</span>}
                          </div>
                          {indexStatus.run_finished_at && (
                            <div className="text-gray-500 dark:text-gray-400">
                              Letzter Lauf: {indexStatus.last_emails} E-Mails · {indexStatus.last_documents} Dokumente · {indexStatus.last_chunks} Chunks neu/aktualisiert
                            </div>
                          )}
                          <div>
                            <span className="text-gray-500 dark:text-gray-400">Nächste Prüfung: </span>
                            {indexStatus.next_run_at
                              ? <span>{fmtRelative(indexStatus.next_run_at)} <span className="text-gray-400">({fmtDateTime(indexStatus.next_run_at)})</span></span>
                              : <span className="text-gray-400">--</span>}
                          </div>
                        </div>
                      )}

                      {/* Fehler des letzten Laufs */}
                      {indexStatus.last_error && (
                        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-xs text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">
                          <span className="font-medium">Letzter Fehler {fmtRelative(indexStatus.last_error_at)}:</span> {indexStatus.last_error}
                        </div>
                      )}

                      {/* Totals je Quelle */}
                      <div>
                        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">Bestand im Index</p>
                        {indexStatus.totals.length === 0 ? (
                          <p className="text-xs text-gray-400">Noch nichts indexiert.</p>
                        ) : (
                          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                            {indexStatus.totals.map(t => (
                              <div key={t.source_type} className="rounded-md border border-gray-200 bg-white/50 p-2.5 dark:border-gray-700 dark:bg-gray-800/40">
                                <div className="text-xs text-gray-500 dark:text-gray-400">{sourceLabel(t.source_type)}</div>
                                <div className="text-base font-semibold text-gray-900 dark:text-white tabular-nums">{t.documents.toLocaleString('de-CH')}</div>
                                <div className="text-[11px] text-gray-400">{t.chunks.toLocaleString('de-CH')} Chunks</div>
                                {t.last_indexed_at && (
                                  <div className="mt-0.5 text-[11px] text-gray-400">zuletzt {fmtRelative(t.last_indexed_at)}</div>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>

                      <p className="text-xs text-gray-400">
                        Der Dienst prüft {indexStatus.interval_seconds >= 86400 ? 'täglich' : `alle ${Math.round(indexStatus.interval_seconds / 3600)} h`} automatisch auf neue E-Mails und Dateien. Jeder Lauf ist idempotent (bereits Indexiertes wird übersprungen), und es läuft immer nur ein Lauf gleichzeitig.
                      </p>
                    </div>
                  )}
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-300">
                    Ausgeschlossene OneDrive-Pfade
                  </label>
                  <p className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                    Pfade relativ zum OneDrive-Stamm (z. B. <code>/Shared/KnowledgeFlow/bundesgericht-steuerrecht</code>).
                    Dokumente unterhalb dieser Pfade werden weder indexiert noch angezeigt; bereits
                    indexierte Einträge werden beim Speichern entfernt.
                  </p>
                  <div className="flex flex-wrap gap-2 mb-2">
                    {excludedPaths.map(p => (
                      <span key={p} className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-3 py-1 text-xs font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                        {p}
                        <button
                          onClick={() => setExcludedPaths(prev => prev.filter(x => x !== p))}
                          className="ml-0.5 text-gray-400 hover:text-red-500"
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    {excludedPaths.length === 0 && (
                      <span className="text-xs text-gray-400 dark:text-gray-500">Keine Ausschlüsse -- alles wird indexiert.</span>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={excludedInput}
                      onChange={e => setExcludedInput(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === 'Enter' && excludedInput.trim()) {
                          const name = excludedInput.trim();
                          if (!excludedPaths.includes(name)) setExcludedPaths(prev => [...prev, name]);
                          setExcludedInput('');
                        }
                      }}
                      placeholder="/Shared/… Pfad eingeben"
                      className="flex-1 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none dark:border-gray-700 dark:bg-gray-800 dark:text-white font-mono"
                    />
                    <button
                      onClick={() => {
                        const name = excludedInput.trim();
                        if (name && !excludedPaths.includes(name)) setExcludedPaths(prev => [...prev, name]);
                        setExcludedInput('');
                      }}
                      className="shrink-0 rounded-lg bg-gray-200 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                    >
                      Hinzufügen
                    </button>
                  </div>
                </div>

                <div className="flex items-center gap-3">
                  <button
                    onClick={saveSearchIndexSettings}
                    className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700"
                  >
                    Speichern & bereinigen
                  </button>
                  {searchIndexMsg && (
                    <span className={`text-sm ${searchIndexMsg.type === 'ok' ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>
                      {searchIndexMsg.text}
                    </span>
                  )}
                </div>
              </div>
            </section>
          )}

          {/* ── Team (ehemals Admin) ── */}
          {tab === 'team' && isOwner && (
            <section>
              <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">Team & Benutzerverwaltung</h2>

              <div className="mb-6 rounded-xl border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-900/50">
                <h3 className="mb-3 text-sm font-semibold text-gray-700 dark:text-gray-300">Neuen Benutzer einladen</h3>
                <div className="flex flex-wrap gap-3">
                  <input
                    type="email"
                    value={inviteEmail}
                    onChange={(e) => setInviteEmail(e.target.value)}
                    placeholder="E-Mail"
                    className="min-w-[180px] flex-1 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                  />
                  <input
                    value={inviteName}
                    onChange={(e) => setInviteName(e.target.value)}
                    placeholder="Name"
                    className="min-w-[140px] flex-1 rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                  />
                  <select
                    value={inviteRole}
                    onChange={(e) => setInviteRole(e.target.value)}
                    className="rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-white"
                  >
                    <option value="member">Mitglied</option>
                    <option value="viewer">Betrachter</option>
                  </select>
                  <button
                    onClick={createUser}
                    disabled={!inviteEmail.trim() || !inviteName.trim()}
                    className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-700 disabled:opacity-40"
                  >
                    Einladen
                  </button>
                </div>

                {createdUserPassword && (
                  <div className="mt-3 rounded-lg border border-green-200 bg-green-50 p-3 dark:border-green-800 dark:bg-green-950">
                    <p className="text-sm text-green-800 dark:text-green-300">
                      <strong>{createdUserPassword.email}</strong> wurde erstellt.
                    </p>
                    <p className="mt-1 text-sm font-medium text-green-800 dark:text-green-300">Temporäres Passwort:</p>
                    <code className="mt-1 block select-all rounded bg-white px-2 py-1 font-mono text-sm dark:bg-gray-800 dark:text-green-300">
                      {createdUserPassword.password}
                    </code>
                    <p className="mt-1 text-xs text-green-600 opacity-70 dark:text-green-400">
                      Bitte an die Person weitergeben. Es wird nur einmal angezeigt.
                    </p>
                    <button
                      onClick={() => setCreatedUserPassword(null)}
                      className="mt-2 rounded px-2 py-1 text-xs font-medium text-green-700 hover:bg-green-100 dark:text-green-400 dark:hover:bg-green-900"
                    >
                      Schliessen
                    </button>
                  </div>
                )}
              </div>

              <div className="overflow-x-auto rounded-xl border border-gray-200 dark:border-gray-800">
                <table className="w-full min-w-[640px] text-left text-sm">
                  <thead>
                    <tr className="border-b border-gray-200 bg-gray-50 dark:border-gray-800 dark:bg-gray-900/60">
                      <th className="px-4 py-3 font-medium text-gray-500 dark:text-gray-400">Name</th>
                      <th className="px-4 py-3 font-medium text-gray-500 dark:text-gray-400">E-Mail</th>
                      <th className="px-4 py-3 font-medium text-gray-500 dark:text-gray-400">Rolle</th>
                      <th className="px-4 py-3 text-right font-medium text-gray-500 dark:text-gray-400">Aktionen</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
                    {users.map((u) => (
                      <tr key={u.id} className={`${u.is_active ? 'bg-white dark:bg-gray-900' : 'bg-gray-50 opacity-60 dark:bg-gray-950'}`}>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            {u.avatar_url ? (
                              <img src={u.avatar_url} alt="" className="h-6 w-6 rounded-full object-cover" />
                            ) : (
                              <div className="flex h-6 w-6 items-center justify-center rounded-full bg-gray-200 text-xs font-bold text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                                {u.display_name.charAt(0).toUpperCase()}
                              </div>
                            )}
                            <span className="font-medium text-gray-900 dark:text-white">{u.display_name}</span>
                            {!u.is_active && (
                              <span className="rounded-full bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-500 dark:bg-gray-700 dark:text-gray-400">inaktiv</span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-gray-600 dark:text-gray-400">{u.email}</td>
                        <td className="px-4 py-3">
                          <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                            u.role === 'owner' ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300' :
                            u.role === 'member' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' :
                            'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
                          }`}>
                            {u.role === 'owner' ? 'Inhaber' : u.role === 'member' ? 'Mitglied' : 'Betrachter'}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-right">
                          {u.role !== 'owner' && (
                            <div className="flex items-center justify-end gap-2">
                              <button
                                onClick={() => toggleUserActive(u.id, u.is_active)}
                                className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-colors ${
                                  u.is_active
                                    ? 'text-amber-600 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950'
                                    : 'text-green-600 hover:bg-green-50 dark:text-green-400 dark:hover:bg-green-950'
                                }`}
                              >
                                {u.is_active ? 'Deaktivieren' : 'Reaktivieren'}
                              </button>
                              <button
                                onClick={() => setConfirmDeleteUserId(u.id === confirmDeleteUserId ? null : u.id)}
                                className="rounded-lg px-2.5 py-1 text-xs font-medium text-red-500 transition-colors hover:bg-red-50 dark:hover:bg-red-950"
                              >
                                Entfernen
                              </button>
                            </div>
                          )}
                          {confirmDeleteUserId === u.id && (
                            <div className="mt-2 rounded-lg border border-red-200 bg-red-50 p-2 text-left dark:border-red-800 dark:bg-red-950">
                              <p className="mb-1.5 text-xs text-red-700 dark:text-red-300">User unwiderruflich löschen?</p>
                              <div className="flex gap-2">
                                <button
                                  onClick={() => deleteUser(u.id)}
                                  className="rounded bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-700"
                                >
                                  Löschen
                                </button>
                                <button
                                  onClick={() => setConfirmDeleteUserId(null)}
                                  className="rounded px-2.5 py-1 text-xs font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800"
                                >
                                  Abbrechen
                                </button>
                              </div>
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {tab === 'llm' && (
            <LlmSettingsTab />
          )}

          {/* ── Intelligenz (umgezogen ins Agenten-Cockpit) ── */}
          {tab === 'intelligence' && (
            <section>
              <h2 className="mb-1 text-lg font-semibold text-gray-900 dark:text-white">Intelligenz</h2>
              <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
                Regeln, Skills und das Wissen des Agenten sind ins Agenten-Cockpit umgezogen.
              </p>
              <div className="grid gap-3 sm:grid-cols-3">
                <Link
                  to="/agenten?tab=regeln"
                  className="group rounded-xl border border-gray-200 bg-white p-4 transition-colors hover:border-indigo-300 hover:bg-indigo-50/40 dark:border-gray-700 dark:bg-gray-800/50 dark:hover:border-indigo-500/50 dark:hover:bg-indigo-900/10"
                >
                  <div className="text-sm font-semibold text-gray-900 group-hover:text-indigo-700 dark:text-white dark:group-hover:text-indigo-300">Regeln</div>
                  <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">Leitregeln & deterministische Overrides pflegen und freigeben.</div>
                </Link>
                <Link
                  to="/agenten?tab=skills"
                  className="group rounded-xl border border-gray-200 bg-white p-4 transition-colors hover:border-indigo-300 hover:bg-indigo-50/40 dark:border-gray-700 dark:bg-gray-800/50 dark:hover:border-indigo-500/50 dark:hover:bg-indigo-900/10"
                >
                  <div className="text-sm font-semibold text-gray-900 group-hover:text-indigo-700 dark:text-white dark:group-hover:text-indigo-300">Skills</div>
                  <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">Fähigkeiten des Agenten als Markdown direkt bearbeiten.</div>
                </Link>
                <Link
                  to="/agenten?tab=wissen"
                  className="group rounded-xl border border-gray-200 bg-white p-4 transition-colors hover:border-indigo-300 hover:bg-indigo-50/40 dark:border-gray-700 dark:bg-gray-800/50 dark:hover:border-indigo-500/50 dark:hover:bg-indigo-900/10"
                >
                  <div className="text-sm font-semibold text-gray-900 group-hover:text-indigo-700 dark:text-white dark:group-hover:text-indigo-300">Wissen</div>
                  <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">Lernfortschritt, Triage-Statistik, Absenderprofile & Memory.</div>
                </Link>
              </div>
            </section>
          )}

        </div>
      </div>
    </div>
  );
}

interface FinanceDocMeta {
  id: string;
  label: string;
  filename: string | null;
  mime: string | null;
  file_size: number | null;
  text_chars: number;
  created_at: string | null;
}

function FinanceDocumentsManager() {
  const [docs, setDocs] = useState<FinanceDocMeta[]>([]);
  const [uploading, setUploading] = useState(false);
  const [label, setLabel] = useState('');
  const [msg, setMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.get<{ documents: FinanceDocMeta[] }>('/api/analysis/documents');
      setDocs(res.documents || []);
    } catch {
      setDocs([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleUpload = async (file: File) => {
    setUploading(true);
    setMsg(null);
    try {
      const fd = new FormData();
      fd.append('file', file);
      if (label.trim()) fd.append('label', label.trim());
      await api.upload<FinanceDocMeta>('/api/analysis/documents', fd);
      setLabel('');
      if (fileRef.current) fileRef.current.value = '';
      setMsg({ type: 'ok', text: 'Dokument hochgeladen und Text extrahiert.' });
      await load();
    } catch (e) {
      const text = e instanceof Error ? e.message : 'Upload fehlgeschlagen';
      setMsg({ type: 'err', text });
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await api.delete(`/api/analysis/documents/${id}`);
      await load();
    } catch { /* */ }
  };

  const fmtSize = (n: number | null) => (n == null ? '' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`);

  return (
    <div>
      <h2 className="mb-1 text-lg font-semibold text-gray-900 dark:text-white">Jahresrechnung / Finanzbelege</h2>
      <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
        Lade eine Jahresrechnung (PDF/DOCX) hoch. Es wird nur der extrahierte Text gespeichert
        (kein Original-PDF) und vor dem Versand an ein Cloud-Modell anonymisiert. Hochgeladene
        Belege können bei einer Analyse als zusätzlicher Kontext ausgewählt werden.
      </p>

      <div className="mb-4 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
        <label className="block text-sm font-medium text-gray-900 dark:text-white">Bezeichnung (optional)</label>
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="z.B. Jahresrechnung 2025"
          className="mt-1 mb-3 w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
        />
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,.doc,.txt,.md"
          disabled={uploading}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) handleUpload(f); }}
          className="block w-full text-sm text-gray-600 file:mr-3 file:rounded-md file:border-0 file:bg-indigo-600 file:px-4 file:py-2 file:text-sm file:font-medium file:text-white hover:file:bg-indigo-700 disabled:opacity-50 dark:text-gray-300"
        />
        {uploading && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Lädt hoch und extrahiert Text…</p>}
        {msg && (
          <p className={`mt-2 text-xs ${msg.type === 'ok' ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>{msg.text}</p>
        )}
      </div>

      {docs.length === 0 ? (
        <p className="text-sm text-gray-400 dark:text-gray-500">Noch keine Dokumente hochgeladen.</p>
      ) : (
        <ul className="space-y-2">
          {docs.map((d) => (
            <li key={d.id} className="flex items-center justify-between rounded-lg border border-gray-200 px-4 py-3 dark:border-gray-700">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-gray-900 dark:text-white">{d.label}</p>
                <p className="truncate text-xs text-gray-500 dark:text-gray-400">
                  {d.filename || '—'} · {fmtSize(d.file_size)} · {d.text_chars.toLocaleString('de-CH')} Zeichen
                  {d.created_at ? ` · ${new Date(d.created_at).toLocaleDateString('de-CH')}` : ''}
                </p>
              </div>
              <button
                onClick={() => handleDelete(d.id)}
                className="ml-3 shrink-0 rounded-md border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950"
              >
                Löschen
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function LlmSettingsTab() {
  const [models, setModels] = useState<{ id: string; name: string; provider: string }[]>([]);
  const [llmSettings, setLlmSettings] = useState<{
    llm_providers: Record<string, { enabled: boolean; models: string[] }> | null;
    llm_default_model: string | null;
    llm_default_local_model: string | null;
    llm_default_temperature: number | null;
  }>({ llm_providers: null, llm_default_model: null, llm_default_local_model: null, llm_default_temperature: null });
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  useEffect(() => {
    Promise.all([
      api.get<{ local: any[]; cloud: any[] }>('/api/models/available'),
      api.get<typeof llmSettings>('/api/settings/llm'),
    ]).then(([modelData, settings]) => {
      setModels([...modelData.local, ...modelData.cloud]);
      setLlmSettings(settings);
    }).catch(() => {});
  }, []);

  const providers = PROVIDER_ORDER;
  const providerLabels = PROVIDER_LABELS;

  const getProviderModels = (provider: string) =>
    models.filter((m) => m.provider === provider);

  const isProviderEnabled = (provider: string) =>
    llmSettings.llm_providers?.[provider]?.enabled ?? false;

  const getEnabledModels = (provider: string) =>
    llmSettings.llm_providers?.[provider]?.models ?? [];

  const toggleProvider = (provider: string) => {
    const current = llmSettings.llm_providers || {};
    const providerConfig = current[provider] || { enabled: false, models: [] };
    const newProviders = {
      ...current,
      [provider]: {
        ...providerConfig,
        enabled: !providerConfig.enabled,
        models: !providerConfig.enabled
          ? getProviderModels(provider).map((m) => m.id)
          : providerConfig.models,
      },
    };
    setLlmSettings({ ...llmSettings, llm_providers: newProviders });
  };

  const toggleModel = (provider: string, modelId: string) => {
    const current = llmSettings.llm_providers || {};
    const providerConfig = current[provider] || { enabled: true, models: [] };
    const models = providerConfig.models.includes(modelId)
      ? providerConfig.models.filter((m) => m !== modelId)
      : [...providerConfig.models, modelId];
    setLlmSettings({
      ...llmSettings,
      llm_providers: {
        ...current,
        [provider]: { ...providerConfig, models },
      },
    });
  };

  const handleSave = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await api.put('/api/settings/llm', llmSettings);
      setMsg({ type: 'ok', text: 'LLM-Einstellungen gespeichert' });
    } catch {
      setMsg({ type: 'err', text: 'Fehler beim Speichern' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <section>
      <h2 className="mb-4 text-lg font-semibold text-gray-900 dark:text-white">LLM-Modelle</h2>
      <p className="mb-6 text-sm text-gray-500 dark:text-gray-400">
        Aktiviere die LLM-Provider und wähle die Modelle, die dir zur Verfügung stehen sollen.
      </p>

      <div className="space-y-6">
        {providers.map((provider) => {
          const providerModels = getProviderModels(provider);
          if (providerModels.length === 0) return null;
          const enabled = isProviderEnabled(provider);
          const enabledModels = getEnabledModels(provider);

          return (
            <div
              key={provider}
              className="rounded-xl border border-gray-200 p-4 dark:border-gray-700"
            >
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="font-medium text-gray-900 dark:text-white">
                    {providerLabels[provider] || provider}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {providerModels.length} Modell{providerModels.length !== 1 ? 'e' : ''} verfügbar
                  </p>
                </div>
                <button
                  onClick={() => toggleProvider(provider)}
                  className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
                    enabled ? 'bg-indigo-600' : 'bg-gray-200 dark:bg-gray-700'
                  }`}
                >
                  <span
                    className={`pointer-events-none inline-block h-5 w-5 rounded-full bg-white shadow ring-0 transition-transform ${
                      enabled ? 'translate-x-5' : 'translate-x-0'
                    }`}
                  />
                </button>
              </div>

              {enabled && (
                <div className="mt-3 space-y-2">
                  {providerModels.map((model) => (
                    <label
                      key={model.id}
                      className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-gray-800"
                    >
                      <input
                        type="checkbox"
                        checked={enabledModels.includes(model.id)}
                        onChange={() => toggleModel(provider, model.id)}
                        className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                      />
                      <span className="text-gray-700 dark:text-gray-300">{model.name}</span>
                      <span className="ml-auto text-xs text-gray-400">{model.id}</span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="mt-6 space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
            Standard-Modell
          </label>
          <select
            value={llmSettings.llm_default_model || ''}
            onChange={(e) =>
              setLlmSettings({ ...llmSettings, llm_default_model: e.target.value || null })
            }
            className="mt-1 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">Kein Standard</option>
            {models
              .filter((m) => {
                const p = m.provider;
                return llmSettings.llm_providers?.[p]?.enabled && llmSettings.llm_providers[p].models.includes(m.id);
              })
              .map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
            Standard Lokal-Modell
          </label>
          <p className="mb-1 text-xs text-gray-500 dark:text-gray-400">
            Wird systemweit als Default für Agent, Triage und Code-Execution verwendet.
          </p>
          <select
            value={llmSettings.llm_default_local_model || ''}
            onChange={(e) =>
              setLlmSettings({ ...llmSettings, llm_default_local_model: e.target.value || null })
            }
            className="mt-1 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">Automatisch (erstes verfügbares)</option>
            {models
              .filter((m) => m.provider === 'ollama')
              .map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300">
            Standard-Temperatur: {llmSettings.llm_default_temperature ?? 0.7}
          </label>
          <input
            type="range"
            min="0"
            max="1.5"
            step="0.1"
            value={llmSettings.llm_default_temperature ?? 0.7}
            onChange={(e) =>
              setLlmSettings({ ...llmSettings, llm_default_temperature: parseFloat(e.target.value) })
            }
            className="mt-1 w-full"
          />
          <div className="flex justify-between text-xs text-gray-400">
            <span>Präzise (0)</span>
            <span>Kreativ (1.5)</span>
          </div>
        </div>
      </div>

      <div className="mt-6 flex items-center gap-3">
        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-indigo-700 disabled:opacity-50"
        >
          {saving ? 'Speichern...' : 'Speichern'}
        </button>
        {msg && (
          <span className={`text-sm ${msg.type === 'ok' ? 'text-green-600' : 'text-red-500'}`}>
            {msg.text}
          </span>
        )}
      </div>
    </section>
  );
}

const SIDEBAR_PALETTE = [
  { key: 'default', label: 'Standard', swatch: 'bg-white dark:bg-gray-950' },
  { key: 'slate', label: 'Slate', swatch: 'bg-slate-200 dark:bg-slate-800' },
  { key: 'zinc', label: 'Zinc', swatch: 'bg-zinc-200 dark:bg-zinc-800' },
  { key: 'stone', label: 'Stone', swatch: 'bg-stone-200 dark:bg-stone-800' },
  { key: 'indigo', label: 'Indigo', swatch: 'bg-indigo-200 dark:bg-indigo-800' },
  { key: 'blue', label: 'Blue', swatch: 'bg-blue-200 dark:bg-blue-800' },
  { key: 'sky', label: 'Sky', swatch: 'bg-sky-200 dark:bg-sky-800' },
  { key: 'emerald', label: 'Emerald', swatch: 'bg-emerald-200 dark:bg-emerald-800' },
];

function CameraIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M6.827 6.175A2.31 2.31 0 0 1 5.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 0 0-1.134-.175 2.31 2.31 0 0 1-1.64-1.055l-.822-1.316a2.192 2.192 0 0 0-1.736-1.039 48.774 48.774 0 0 0-5.232 0 2.192 2.192 0 0 0-1.736 1.039l-.821 1.316Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 12.75a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0Z" />
    </svg>
  );
}

function ModalIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h12A2.25 2.25 0 0 1 20.25 6v12A2.25 2.25 0 0 1 18 20.25H6A2.25 2.25 0 0 1 3.75 18V6Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9M7.5 12h9M7.5 15.75h5.25" />
    </svg>
  );
}

function PanelIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h12A2.25 2.25 0 0 1 20.25 6v12A2.25 2.25 0 0 1 18 20.25H6A2.25 2.25 0 0 1 3.75 18V6Z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 3.75v16.5" />
    </svg>
  );
}

function FullscreenIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9m11.25-5.25v4.5m0-4.5h-4.5m4.5 0L15 9m5.25 11.25v-4.5m0 4.5h-4.5m4.5 0L15 15m-11.25 5.25v-4.5m0 4.5h4.5m-4.5 0L9 15" />
    </svg>
  );
}

function PipedriveIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="6" fill="#017737" />
      <path d="M12 4C8.69 4 6 6.69 6 10c0 2.22 1.21 4.16 3 5.2V18a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2v-2.8c1.79-1.04 3-2.98 3-5.2 0-3.31-2.69-6-6-6Zm0 9a3 3 0 1 1 0-6 3 3 0 0 1 0 6Z" fill="white" />
    </svg>
  );
}
