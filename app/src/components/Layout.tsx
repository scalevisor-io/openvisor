import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { billingApi } from "../lib/endpoints";
import { useTheme } from "../lib/theme";
import { BrandMark, BrandName, formatCredits, readCardCollapsed, writeCardCollapsed } from "./ui";

const SIDEBAR_KEY = "app:sidebar";

function Wordmark() {
  const { settings } = useAuth();
  return (
    <span className="wordmark" style={{ gap: "0.4rem" }}>
      <BrandMark
        size={20}
        primary={settings?.brand_color_primary ?? "#22d3ee"}
        secondary={settings?.brand_color_secondary ?? "#7c3aed"}
      />
      <BrandName name={settings?.brand_name ?? "Openvisor"} />
    </span>
  );
}

function HeaderCredits() {
  const { me } = useAuth();
  const location = useLocation();
  const [balance, setBalance] = useState<number | null>(me?.org.credit_balance ?? null);

  // The balance changes server-side (top-ups, review fees, build charges), so
  // refresh it on every navigation and when the tab regains focus.
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      billingApi
        .balance()
        .then((b) => {
          if (!cancelled) setBalance(b.credit_balance);
        })
        .catch(() => {});
    load();
    window.addEventListener("focus", load);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", load);
    };
  }, [location.pathname]);

  if (balance === null) return null;
  return (
    <Link to="/billing" className="header-credits" title="Credit balance - open billing">
      <span className="grad-text">{formatCredits(balance)}</span>
      <span className="muted">credits</span>
    </Link>
  );
}

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const label = theme === "light" ? "Switch to dark theme" : "Switch to light theme";
  return (
    <button
      type="button"
      className="btn btn-sm btn-ghost"
      onClick={toggleTheme}
      title={label}
      aria-label={label}
    >
      {theme === "light" ? (
        // Moon: click to go dark
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      ) : (
        // Sun: click to go light
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="5" />
          <line x1="12" y1="1" x2="12" y2="3" />
          <line x1="12" y1="21" x2="12" y2="23" />
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
          <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
          <line x1="1" y1="12" x2="3" y2="12" />
          <line x1="21" y1="12" x2="23" y2="12" />
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
          <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
        </svg>
      )}
    </button>
  );
}

// Small stroke-icon wrapper for sidebar entries (same visual language as the
// theme toggle: 24-viewBox feather-style paths, currentColor).
function NavIcon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="nav-icon"
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

// One sidebar entry. The label is its own element so the collapsed rail can hide
// it and fall back to the icon + tooltip.
function NavItem({
  to,
  label,
  icon,
  end,
}: {
  to: string;
  label: string;
  icon: ReactNode;
  end?: boolean;
}) {
  return (
    <NavLink to={to} end={end} className="nav-link" title={label}>
      <NavIcon>{icon}</NavIcon>
      <span className="nav-label">{label}</span>
    </NavLink>
  );
}

export default function Layout() {
  const { me, isAdmin, logout } = useAuth();
  const { pathname } = useLocation();
  // The project page shows its work column + the chat rail side by side and needs
  // more room - on every tab AND its sub-routes (e.g. a request detail), which are
  // the same shell.
  const wide = /^\/projects\/(?!new$)[^/]+(\/[^/]+){0,2}$/.test(pathname);
  // The sidebar starts expanded and remembers a collapse, like the project chat
  // rail. Collapsed it keeps every entry - icons only, labels as tooltips.
  const [navOpen, setNavOpen] = useState(() => readCardCollapsed(SIDEBAR_KEY) !== true);
  const toggleNav = () => {
    setNavOpen(!navOpen);
    writeCardCollapsed(SIDEBAR_KEY, navOpen);
  };

  return (
    <div className={`app-shell${navOpen ? "" : " nav-collapsed"}`}>
      <aside className="app-sidebar" id="app-sidebar">
        <div className="sidebar-brand">
          <Link to="/" className="brand-link" title="Projects">
            <Wordmark />
          </Link>
          <button
            type="button"
            className="sidebar-toggle"
            onClick={toggleNav}
            aria-expanded={navOpen}
            aria-controls="app-sidebar"
            aria-label={navOpen ? "Collapse the sidebar" : "Expand the sidebar"}
            title={navOpen ? "Collapse the sidebar" : "Expand the sidebar"}
          >
            <svg
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <line x1="9" y1="4" x2="9" y2="20" />
            </svg>
          </button>
        </div>
        <nav className="nav-group" aria-label="Workspace">
          <div className="nav-section">Workspace</div>
          <NavItem
            to="/"
            end
            label="Projects"
            icon={<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />}
          />
          <NavItem
            to="/programs"
            label="Programs"
            icon={
              <>
                <circle cx="12" cy="12" r="10" />
                <polygon points="10 8 16 12 10 16 10 8" />
              </>
            }
          />
          <NavItem
            to="/memory"
            label="Global memory"
            icon={
              <>
                <ellipse cx="12" cy="5" rx="9" ry="3" />
                <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
                <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
              </>
            }
          />
        </nav>
        <nav className="nav-group" aria-label="Account">
          <div className="nav-section">Account</div>
          <NavItem
            to="/billing"
            label="Billing"
            icon={
              <>
                <rect x="1" y="4" width="22" height="16" rx="2" ry="2" />
                <line x1="1" y1="10" x2="23" y2="10" />
              </>
            }
          />
          <NavItem
            to="/settings/account"
            label="Account"
            icon={
              <>
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                <circle cx="12" cy="7" r="4" />
              </>
            }
          />
          <NavItem
            to="/settings/tokens"
            label="API tokens"
            icon={
              <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
            }
          />
        </nav>
        {isAdmin && (
          <nav className="nav-group nav-group-admin" aria-label="Admin">
            <div className="nav-section">Admin</div>
            <NavItem
              to="/admin"
              end
              label="Overview"
              icon={<polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />}
            />
            <NavItem
              to="/admin/users"
              label="Users"
              icon={
                <>
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                  <circle cx="9" cy="7" r="4" />
                  <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                  <path d="M16 3.13a4 4 0 0 1 0 7.75" />
                </>
              }
            />
            <NavItem
              to="/admin/programs"
              label="Programs"
              icon={
                <>
                  <polyline points="4 17 10 11 4 5" />
                  <line x1="12" y1="19" x2="20" y2="19" />
                </>
              }
            />
            <NavItem
              to="/admin/tools"
              label="Tools"
              icon={
                <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.1-2.1z" />
              }
            />
            <NavItem
              to="/admin/knowledge-bases"
              label="Knowledge bases"
              icon={
                <>
                  <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
                  <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
                </>
              }
            />
            <NavItem
              to="/admin/model-endpoints"
              label="Model configuration"
              icon={
                <>
                  <rect x="4" y="4" width="16" height="16" rx="2" ry="2" />
                  <rect x="9" y="9" width="6" height="6" />
                  <line x1="9" y1="1" x2="9" y2="4" />
                  <line x1="15" y1="1" x2="15" y2="4" />
                  <line x1="9" y1="20" x2="9" y2="23" />
                  <line x1="15" y1="20" x2="15" y2="23" />
                  <line x1="20" y1="9" x2="23" y2="9" />
                  <line x1="20" y1="14" x2="23" y2="14" />
                  <line x1="1" y1="9" x2="4" y2="9" />
                  <line x1="1" y1="14" x2="4" y2="14" />
                </>
              }
            />
            <NavItem
              to="/admin/settings"
              label="Settings"
              icon={
                <>
                  <circle cx="12" cy="12" r="3" />
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
                </>
              }
            />

          </nav>
        )}
      </aside>

      <header className="app-header">
        <div className="row">
          <span className="alpha-banner">⚠ alpha</span>
        </div>
        <div className="header-user">
          <HeaderCredits />
          <ThemeToggle />
          <span className="muted small">{me?.user.email}</span>
          <button type="button" className="btn btn-sm" onClick={logout}>
            Log out
          </button>
        </div>
      </header>

      <main className="app-main">
        <div className={`app-main-inner${wide ? " wide" : ""}`}>
          <Outlet />
        </div>
      </main>
    </div>
  );
}
