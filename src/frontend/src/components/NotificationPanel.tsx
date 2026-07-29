import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  X,
  CheckCheck,
  Bell,
  UserPlus,
  MessageCircle,
  Clock,
  AlertTriangle,
  Bot,
  Mail,
  MessagesSquare,
  Newspaper,
  FileText,
  MailQuestion,
} from 'lucide-react';
import type { NotificationItem } from '../hooks/useNotifications';

const TYPE_CONFIG: Record<string, { icon: typeof Bell; color: string }> = {
  agent_awaiting_approval: { icon: Bot, color: 'text-amber-500' },
  task_suggested: { icon: Mail, color: 'text-blue-500' },
  task_assigned: { icon: UserPlus, color: 'text-indigo-500' },
  chat_triage_task: { icon: MessagesSquare, color: 'text-teal-500' },
  comment_mention: { icon: MessageCircle, color: 'text-violet-500' },
  task_due_soon: { icon: Clock, color: 'text-orange-500' },
  system_health_warning: { icon: AlertTriangle, color: 'text-red-500' },
  briefing_ready: { icon: Newspaper, color: 'text-sky-500' },
  meeting_summary_ready: { icon: FileText, color: 'text-emerald-500' },
  follow_up_due: { icon: MailQuestion, color: 'text-rose-500' },
};

interface NotificationPanelProps {
  isOpen: boolean;
  items: NotificationItem[];
  loading: boolean;
  onClose: () => void;
  onFetchItems: () => void;
  onMarkAsRead: (id: string) => Promise<void>;
  onMarkAllAsRead: () => Promise<void>;
  onDismiss: (id: string) => Promise<void>;
  mobile?: boolean;
}

export function NotificationPanel({
  isOpen,
  items,
  loading,
  onClose,
  onFetchItems,
  onMarkAsRead,
  onMarkAllAsRead,
  onDismiss,
  mobile = false,
}: NotificationPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (isOpen) {
      onFetchItems();
    }
  }, [isOpen, onFetchItems]);

  useEffect(() => {
    if (!isOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const handleItemClick = async (item: NotificationItem) => {
    if (!item.is_read) {
      await onMarkAsRead(item.id);
    }
    if (item.link) {
      navigate(item.link);
    }
    onClose();
  };

  const formatTime = (iso: string) => {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'Jetzt';
    if (diffMin < 60) return `vor ${diffMin} Min`;
    const diffH = Math.floor(diffMin / 60);
    if (diffH < 24) return `vor ${diffH}h`;
    const diffD = Math.floor(diffH / 24);
    if (diffD < 7) return `vor ${diffD}d`;
    return d.toLocaleDateString('de-CH', { day: 'numeric', month: 'short' });
  };

  const unreadCount = items.filter((n) => !n.is_read).length;

  if (mobile) {
    return (
      <>
        <div className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm" onClick={onClose} />
        <div
          ref={panelRef}
          className="fixed inset-x-0 z-50 overflow-hidden rounded-b-2xl border-b border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-900"
          style={{ top: 'calc(3rem + env(safe-area-inset-top, 0px))' }}
        >
          <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3 dark:border-gray-800">
            <h3 className="text-sm font-semibold text-gray-900 dark:text-white">
              Benachrichtigungen
              {unreadCount > 0 && (
                <span className="ml-2 rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700 dark:bg-red-900/30 dark:text-red-400">
                  {unreadCount}
                </span>
              )}
            </h3>
            <div className="flex items-center gap-1">
              {unreadCount > 0 && (
                <button
                  onClick={onMarkAllAsRead}
                  className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
                  title="Alle als gelesen markieren"
                >
                  <CheckCheck className="h-3.5 w-3.5" />
                  Alle gelesen
                </button>
              )}
              <button
                onClick={onClose}
                className="rounded-md p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
          </div>
          <div className="max-h-[60dvh] overflow-y-auto">
            {loading && items.length === 0 && (
              <div className="flex items-center justify-center py-8">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-300 border-t-indigo-600" />
              </div>
            )}
            {!loading && items.length === 0 && (
              <div className="flex flex-col items-center justify-center py-10 text-gray-400 dark:text-gray-500">
                <Bell className="mb-2 h-8 w-8" />
                <p className="text-sm">Keine Benachrichtigungen</p>
              </div>
            )}
            {items.map((item) => {
              const cfg = TYPE_CONFIG[item.type] || { icon: Bell, color: 'text-gray-500' };
              const Icon = cfg.icon;
              return (
                <div
                  key={item.id}
                  onClick={() => handleItemClick(item)}
                  className={`group flex cursor-pointer gap-3 border-b border-gray-50 px-4 py-3 transition-colors active:bg-gray-100 dark:border-gray-800/50 dark:active:bg-gray-800/50 ${
                    !item.is_read ? 'bg-indigo-50/40 dark:bg-indigo-950/20' : ''
                  }`}
                >
                  <div className={`mt-0.5 shrink-0 ${cfg.color}`}>
                    <Icon className="h-4.5 w-4.5" />
                  </div>
                  <div className="min-w-0 flex-1 wrap-anywhere">
                    <p className={`text-sm leading-snug ${!item.is_read ? 'font-semibold text-gray-900 dark:text-white' : 'text-gray-700 dark:text-gray-300'}`}>
                      {!item.is_read && <span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-indigo-500" />}
                      {item.title}
                    </p>
                    {item.body && (
                      <p className="mt-0.5 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">{item.body}</p>
                    )}
                    <p className="mt-1 text-[10px] text-gray-400 dark:text-gray-500">{formatTime(item.created_at)}</p>
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); onDismiss(item.id); }}
                    className="shrink-0 self-start rounded p-1 text-gray-300 transition-all dark:text-gray-600"
                    title="Entfernen"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </>
    );
  }

  return (
    <div
      ref={panelRef}
      className="absolute right-0 top-full z-50 mt-2 w-[380px] max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl border border-gray-200 bg-white shadow-xl dark:border-gray-700 dark:bg-gray-900"
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3 dark:border-gray-800">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-white">
          Benachrichtigungen
          {unreadCount > 0 && (
            <span className="ml-2 rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700 dark:bg-red-900/30 dark:text-red-400">
              {unreadCount}
            </span>
          )}
        </h3>
        <div className="flex items-center gap-1">
          {unreadCount > 0 && (
            <button
              onClick={onMarkAllAsRead}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              title="Alle als gelesen markieren"
            >
              <CheckCheck className="h-3.5 w-3.5" />
              Alle gelesen
            </button>
          )}
          <button
            onClick={onClose}
            className="rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Items */}
      <div className="max-h-[min(480px,60vh)] overflow-y-auto">
        {loading && items.length === 0 && (
          <div className="flex items-center justify-center py-8">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-indigo-300 border-t-indigo-600" />
          </div>
        )}

        {!loading && items.length === 0 && (
          <div className="flex flex-col items-center justify-center py-10 text-gray-400 dark:text-gray-500">
            <Bell className="mb-2 h-8 w-8" />
            <p className="text-sm">Keine Benachrichtigungen</p>
          </div>
        )}

        {items.map((item) => {
          const cfg = TYPE_CONFIG[item.type] || { icon: Bell, color: 'text-gray-500' };
          const Icon = cfg.icon;

          return (
            <div
              key={item.id}
              onClick={() => handleItemClick(item)}
              className={`group flex cursor-pointer gap-3 border-b border-gray-50 px-4 py-3 transition-colors hover:bg-gray-50 dark:border-gray-800/50 dark:hover:bg-gray-800/50 ${
                !item.is_read ? 'bg-indigo-50/40 dark:bg-indigo-950/20' : ''
              }`}
            >
              <div className={`mt-0.5 shrink-0 ${cfg.color}`}>
                <Icon className="h-4.5 w-4.5" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-start justify-between gap-2">
                  <p
                    className={`text-sm leading-snug ${
                      !item.is_read
                        ? 'font-semibold text-gray-900 dark:text-white'
                        : 'text-gray-700 dark:text-gray-300'
                    }`}
                  >
                    {!item.is_read && (
                      <span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-indigo-500" />
                    )}
                    {item.title}
                  </p>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDismiss(item.id);
                    }}
                    className="shrink-0 rounded p-0.5 text-gray-300 opacity-0 transition-all hover:bg-gray-200 hover:text-gray-500 group-hover:opacity-100 dark:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-400"
                    title="Entfernen"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                {item.body && (
                  <p className="mt-0.5 line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
                    {item.body}
                  </p>
                )}
                <p className="mt-1 text-[10px] text-gray-400 dark:text-gray-500">
                  {formatTime(item.created_at)}
                </p>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
