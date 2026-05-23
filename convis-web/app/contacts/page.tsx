'use client';

/**
 * Contacts management — the operator-facing UI for the conversation-memory
 * feature. Lists the durable per-tenant contacts, shows how many calls each
 * has on record, and exposes the right-to-be-forgotten controls:
 *
 *   • "Remembered" toggle  → flips contacts.do_not_remember. Turning it OFF
 *     (do_not_remember=true) cascade-deletes every stored summary for that
 *     contact — a confirm dialog gates this because it is irreversible.
 *   • Delete               → removes the contact row + all its summaries.
 *
 * Backend: /api/contacts (GET list, PATCH {id}, DELETE {id}) — see
 * convis-api/app/routes/contacts/contacts.py.
 */

import { useEffect, useMemo, useState } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { DotLottieReact } from '@lottiefiles/dotlottie-react';
import { NAV_ITEMS, NavigationItem } from '../components/Navigation';
import { TopBar } from '../components/TopBar';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://api.convis.ai';

type StoredUser = {
  fullName?: string;
  name?: string;
  firstName?: string;
  username?: string;
  email?: string;
  [key: string]: unknown;
};

interface Contact {
  id: string;
  user_id: string;
  phone_number: string;
  name?: string | null;
  do_not_remember: boolean;
  created_at: string;
  updated_at: string;
  call_count?: number | null;
  last_call_at?: string | null;
}

function formatDate(value?: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export default function ContactsPage() {
  const router = useRouter();
  const pathname = usePathname();

  // ── Shell state (mirrors the other dashboard pages) ──
  const [user, setUser] = useState<StoredUser | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(true);
  const [isDarkMode, setIsDarkMode] = useState(false);
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const [activeNav] = useState('Contacts');

  // ── Page state ──
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  const navigationItems = useMemo(() => NAV_ITEMS, []);

  const userInitial = useMemo(() => {
    const candidate = user?.fullName || user?.name || user?.username || user?.email;
    if (!candidate || typeof candidate !== 'string') return 'U';
    const trimmed = candidate.trim();
    return trimmed.length > 0 ? trimmed.charAt(0).toUpperCase() : 'U';
  }, [user]);

  const userGreeting = useMemo(() => {
    const options = [user?.firstName, user?.fullName, user?.name, user?.username, user?.email]
      .filter((v) => typeof v === 'string' && v.trim().length > 0) as string[];
    if (options.length === 0) return undefined;
    const preferred = options[0];
    return preferred.includes('@') ? preferred.split('@')[0] : preferred.split(' ')[0];
  }, [user]);

  useEffect(() => {
    const storedToken = localStorage.getItem('token');
    if (!storedToken) {
      router.push('/login');
      return;
    }
    setToken(storedToken);
    const storedUser = localStorage.getItem('user');
    if (storedUser) {
      try {
        setUser(JSON.parse(storedUser));
      } catch {
        /* ignore corrupt user blob */
      }
    }
    if (localStorage.getItem('theme') === 'dark') setIsDarkMode(true);
    fetchContacts(storedToken);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchContacts = async (authToken: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/contacts?limit=200`, {
        headers: { Authorization: `Bearer ${authToken}` },
      });
      if (res.status === 401) {
        localStorage.removeItem('token');
        router.push('/login');
        return;
      }
      if (!res.ok) throw new Error(`Failed to load contacts (HTTP ${res.status})`);
      const data = await res.json();
      setContacts(Array.isArray(data?.contacts) ? data.contacts : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load contacts');
    } finally {
      setLoading(false);
    }
  };

  const toggleRemembered = async (c: Contact) => {
    // do_not_remember=true  ⇒ "not remembered". Turning memory OFF deletes
    // every stored summary — gate it behind an explicit confirm.
    const turnMemoryOff = !c.do_not_remember;
    if (turnMemoryOff) {
      const ok = window.confirm(
        `Stop remembering ${c.name || c.phone_number}?\n\n` +
          `This permanently deletes all stored conversation summaries for this ` +
          `contact and the assistant will no longer recall past calls. ` +
          `This cannot be undone.`,
      );
      if (!ok) return;
    }
    setBusyId(c.id);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch(`${API_URL}/api/contacts/${c.id}`, {
        method: 'PATCH',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ do_not_remember: turnMemoryOff }),
      });
      if (!res.ok) throw new Error(`Update failed (HTTP ${res.status})`);
      const updated: Contact = await res.json();
      setContacts((prev) => prev.map((x) => (x.id === c.id ? { ...x, ...updated } : x)));
      setNotice(
        turnMemoryOff
          ? `Stopped remembering ${c.name || c.phone_number} — stored summaries deleted.`
          : `${c.name || c.phone_number} will be remembered again on future calls.`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setBusyId(null);
    }
  };

  const deleteContact = async (c: Contact) => {
    const ok = window.confirm(
      `Permanently delete ${c.name || c.phone_number}?\n\n` +
        `The contact and every stored conversation summary will be removed. ` +
        `This cannot be undone.`,
    );
    if (!ok) return;
    setBusyId(c.id);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch(`${API_URL}/api/contacts/${c.id}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`Delete failed (HTTP ${res.status})`);
      setContacts((prev) => prev.filter((x) => x.id !== c.id));
      setNotice(`Deleted ${c.name || c.phone_number}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setBusyId(null);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('clientId');
    localStorage.removeItem('isAdmin');
    router.push('/login');
  };

  const toggleTheme = () => {
    const next = !isDarkMode;
    setIsDarkMode(next);
    localStorage.setItem('theme', next ? 'dark' : 'light');
  };

  const handleNavigation = (navItem: NavigationItem) => {
    if (navItem.href) router.push(navItem.href);
  };

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return contacts;
    return contacts.filter(
      (c) => (c.name || '').toLowerCase().includes(q) || c.phone_number.toLowerCase().includes(q),
    );
  }, [contacts, search]);

  return (
    <div className={`flex h-screen ${isDarkMode ? 'dark bg-gray-900' : 'bg-neutral-light'}`}>
      {/* Sidebar */}
      <aside
        onMouseEnter={() => setIsSidebarCollapsed(false)}
        onMouseLeave={() => setIsSidebarCollapsed(true)}
        className={`fixed left-0 top-0 h-screen ${isDarkMode ? 'bg-gray-800' : 'bg-white'} border-r ${isDarkMode ? 'border-gray-700' : 'border-neutral-mid/10'} transition-all duration-300 z-40 ${isSidebarCollapsed ? 'w-20' : 'w-64'} ${isMobileMenuOpen ? 'translate-x-0' : '-translate-x-full'} lg:translate-x-0`}
      >
        <div className="flex flex-col h-full">
          <div className={`flex items-center ${isSidebarCollapsed ? 'justify-center px-4' : 'justify-start gap-3 px-6'} py-4 h-[57px] border-b ${isDarkMode ? 'border-gray-700' : 'border-neutral-mid/10'}`}>
            <div className="w-10 h-10 bg-primary rounded-xl flex items-center justify-center flex-shrink-0">
              <DotLottieReact
                src="/microphone-animation.lottie"
                loop
                autoplay
                style={{ width: '24px', height: '24px' }}
              />
            </div>
            {!isSidebarCollapsed && (
              <span className={`font-bold text-lg ${isDarkMode ? 'text-white' : 'text-neutral-dark'} whitespace-nowrap`}>
                Convis AI
              </span>
            )}
          </div>

          <nav className="flex-1 p-4 space-y-1 overflow-y-auto">
            {navigationItems.map((item) => {
              const hasSubItems = item.subItems && item.subItems.length > 0;
              const isCurrentPageInSubItems =
                hasSubItems && item.subItems?.some((sub) => pathname === sub.href);
              const isDropdownOpen = openDropdown === item.name || isCurrentPageInSubItems;

              return (
                <div key={item.name}>
                  <button
                    onClick={() => {
                      if (hasSubItems) {
                        setOpenDropdown(isDropdownOpen ? null : item.name);
                      } else {
                        handleNavigation(item);
                      }
                    }}
                    className={`w-full flex items-center gap-3 px-4 py-3 rounded-xl transition-all duration-200 ${
                      activeNav === item.name || isCurrentPageInSubItems
                        ? `${isDarkMode ? 'bg-primary/20 text-primary' : 'bg-primary/10 text-primary'} font-semibold`
                        : `${isDarkMode ? 'text-gray-400 hover:bg-gray-700 hover:text-white' : 'text-neutral-mid hover:bg-neutral-light hover:text-neutral-dark'}`
                    } ${isSidebarCollapsed ? 'justify-center' : ''}`}
                  >
                    <svg className="w-5 h-5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      {item.icon}
                    </svg>
                    {!isSidebarCollapsed && (
                      <>
                        <span className="text-sm flex-1 text-left">{item.name}</span>
                        {hasSubItems && (
                          <svg
                            className={`w-4 h-4 transition-transform duration-200 ${isDropdownOpen ? 'rotate-180' : ''}`}
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                          >
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                          </svg>
                        )}
                      </>
                    )}
                  </button>

                  {hasSubItems && isDropdownOpen && !isSidebarCollapsed && (
                    <div className="ml-4 mt-1 space-y-1">
                      {item.subItems?.map((subItem) => (
                        <button
                          key={subItem.name}
                          onClick={() => router.push(subItem.href)}
                          className={`w-full flex items-center gap-3 px-4 py-2 rounded-lg transition-all duration-200 ${
                            pathname === subItem.href
                              ? `${isDarkMode ? 'bg-primary/20 text-primary' : 'bg-primary/10 text-primary'} font-semibold`
                              : `${isDarkMode ? 'text-gray-400 hover:bg-gray-700 hover:text-white' : 'text-neutral-mid hover:bg-neutral-light hover:text-neutral-dark'}`
                          }`}
                        >
                          {subItem.logo ? (
                            <div className="w-5 h-5 flex-shrink-0">{subItem.logo}</div>
                          ) : (
                            <svg className="w-5 h-5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              {subItem.icon}
                            </svg>
                          )}
                          <span className="text-sm">{subItem.name}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </nav>
        </div>
      </aside>

      {/* Main content */}
      <div className={`flex-1 flex flex-col transition-all duration-300 ${isSidebarCollapsed ? 'lg:ml-20' : 'lg:ml-64'}`}>
        <TopBar
          isDarkMode={isDarkMode}
          toggleTheme={toggleTheme}
          onLogout={handleLogout}
          userInitial={userInitial}
          userLabel={userGreeting}
          onToggleMobileMenu={() => setIsMobileMenuOpen((prev) => !prev)}
          showSearch={false}
          token={token || undefined}
        />

        <div className="flex-1 overflow-y-auto">
          <main className="p-6">
            {/* Header */}
            <div className="flex items-start justify-between mb-6 gap-4 flex-wrap">
              <div>
                <h1 className={`text-3xl font-bold ${isDarkMode ? 'text-white' : 'text-neutral-dark'} mb-2`}>
                  Contacts
                </h1>
                <p className={`${isDarkMode ? 'text-gray-400' : 'text-neutral-mid'} max-w-2xl`}>
                  People your assistants have spoken to. When conversation memory is on, each
                  contact accumulates call summaries the assistant recalls on future calls.
                  Turn memory off (right to be forgotten) to delete a contact&apos;s stored history.
                </p>
              </div>
              <button
                onClick={() => token && fetchContacts(token)}
                disabled={loading}
                className={`px-4 py-2 rounded-xl border text-sm font-medium transition-all ${
                  isDarkMode
                    ? 'border-gray-700 text-gray-300 hover:bg-gray-800'
                    : 'border-neutral-mid/20 text-neutral-dark hover:bg-neutral-light'
                } disabled:opacity-50`}
              >
                {loading ? 'Refreshing…' : 'Refresh'}
              </button>
            </div>

            {/* Notices */}
            {error && (
              <div className="mb-4 px-4 py-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm">
                {error}
              </div>
            )}
            {notice && (
              <div className="mb-4 px-4 py-3 rounded-xl bg-green-50 border border-green-200 text-green-700 text-sm">
                {notice}
              </div>
            )}

            {/* Search */}
            <div className="mb-4">
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search by name or phone number"
                className={`w-full sm:w-80 px-4 py-2 rounded-xl border text-sm ${
                  isDarkMode
                    ? 'bg-gray-800 border-gray-700 text-white placeholder-gray-500'
                    : 'bg-white border-neutral-mid/20 text-neutral-dark placeholder-neutral-mid'
                } focus:outline-none focus:ring-2 focus:ring-primary/40`}
              />
            </div>

            {/* Table */}
            <div className={`rounded-2xl border overflow-hidden ${isDarkMode ? 'border-gray-700 bg-gray-800' : 'border-neutral-mid/10 bg-white'}`}>
              {loading ? (
                <div className="p-12 flex justify-center">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary" />
                </div>
              ) : filtered.length === 0 ? (
                <div className={`p-12 text-center ${isDarkMode ? 'text-gray-400' : 'text-neutral-mid'}`}>
                  {contacts.length === 0
                    ? 'No contacts yet. They are created automatically after calls on assistants with conversation memory enabled.'
                    : 'No contacts match your search.'}
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className={`${isDarkMode ? 'bg-gray-900/50 text-gray-400' : 'bg-neutral-light text-neutral-mid'} text-left`}>
                        <th className="px-4 py-3 font-semibold">Name</th>
                        <th className="px-4 py-3 font-semibold">Phone</th>
                        <th className="px-4 py-3 font-semibold">Calls</th>
                        <th className="px-4 py-3 font-semibold">Last call</th>
                        <th className="px-4 py-3 font-semibold">Memory</th>
                        <th className="px-4 py-3 font-semibold text-right">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((c) => {
                        const remembered = !c.do_not_remember;
                        const busy = busyId === c.id;
                        return (
                          <tr
                            key={c.id}
                            className={`border-t ${isDarkMode ? 'border-gray-700' : 'border-neutral-mid/10'}`}
                          >
                            <td className={`px-4 py-3 font-medium ${isDarkMode ? 'text-white' : 'text-neutral-dark'}`}>
                              {c.name || <span className={isDarkMode ? 'text-gray-500' : 'text-neutral-mid'}>—</span>}
                            </td>
                            <td className={`px-4 py-3 ${isDarkMode ? 'text-gray-300' : 'text-neutral-dark'}`}>
                              {c.phone_number}
                            </td>
                            <td className={`px-4 py-3 ${isDarkMode ? 'text-gray-300' : 'text-neutral-dark'}`}>
                              {c.call_count ?? 0}
                            </td>
                            <td className={`px-4 py-3 ${isDarkMode ? 'text-gray-400' : 'text-neutral-mid'}`}>
                              {formatDate(c.last_call_at)}
                            </td>
                            <td className="px-4 py-3">
                              <button
                                onClick={() => toggleRemembered(c)}
                                disabled={busy}
                                title={
                                  remembered
                                    ? 'Memory on — click to forget this contact (deletes stored summaries)'
                                    : 'Memory off — click to start remembering again'
                                }
                                className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-semibold transition-all disabled:opacity-50 ${
                                  remembered
                                    ? 'bg-green-100 text-green-700 hover:bg-green-200'
                                    : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                                }`}
                              >
                                <span
                                  className={`w-2 h-2 rounded-full ${remembered ? 'bg-green-500' : 'bg-gray-400'}`}
                                />
                                {busy ? 'Saving…' : remembered ? 'Remembered' : 'Forgotten'}
                              </button>
                            </td>
                            <td className="px-4 py-3 text-right">
                              <button
                                onClick={() => deleteContact(c)}
                                disabled={busy}
                                className="px-3 py-1.5 rounded-lg text-xs font-semibold text-red-600 hover:bg-red-50 transition-all disabled:opacity-50"
                              >
                                Delete
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {!loading && filtered.length > 0 && (
              <p className={`mt-3 text-xs ${isDarkMode ? 'text-gray-500' : 'text-neutral-mid'}`}>
                Showing {filtered.length} of {contacts.length} contact{contacts.length === 1 ? '' : 's'}.
              </p>
            )}
          </main>
        </div>
      </div>
    </div>
  );
}
