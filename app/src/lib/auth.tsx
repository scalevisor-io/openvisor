import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { authApi, metaApi, settingsApi } from "./endpoints";
import { resetCsrf } from "./api";
import type { Me, MetaConfig, PublicSettings } from "../types";

interface AuthCtx {
  me: Me | null;
  config: MetaConfig | null;
  settings: PublicSettings | null;
  loading: boolean;
  isAdmin: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx>({
  me: null,
  config: null,
  settings: null,
  loading: true,
  isAdmin: false,
  refresh: async () => {},
  logout: async () => {},
});

// The stylesheet ships the default Openvisor accent; only stamp inline overrides
// on <html> when a spoke actually diverges, to keep the style attribute clean.
const DEFAULT_PRIMARY = "#22d3ee";
const DEFAULT_SECONDARY = "#7c3aed";

function applyBranding(s: PublicSettings) {
  document.title = s.brand_name;
  const primary = s.brand_color_primary;
  const secondary = s.brand_color_secondary;
  if (
    primary.toLowerCase() === DEFAULT_PRIMARY &&
    secondary.toLowerCase() === DEFAULT_SECONDARY
  ) {
    return;
  }
  const root = document.documentElement.style;
  root.setProperty("--speed-from", primary);
  root.setProperty("--speed-to", secondary);
  root.setProperty("--gradient-speed", `linear-gradient(135deg, ${primary}, ${secondary})`);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [config, setConfig] = useState<MetaConfig | null>(null);
  const [settings, setSettings] = useState<PublicSettings | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = await authApi.me();
      setMe(data);
    } catch {
      setMe(null);
    }
    // config carries the runtime pause flags; reload it so an admin toggle
    // takes effect app-wide without a hard refresh.
    metaApi
      .config()
      .then(setConfig)
      .catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // config + settings are public and harmless to fetch before auth resolves.
      metaApi
        .config()
        .then((c) => !cancelled && setConfig(c))
        .catch(() => {});
      // Brand identity drives the wordmark, document title and accent colors, so
      // apply it as soon as it lands (a spoke rebrands with no SPA rebuild).
      settingsApi
        .get()
        .then((s) => {
          if (cancelled) return;
          setSettings(s);
          applyBranding(s);
        })
        .catch(() => {});
      await refresh();
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      // ignore
    }
    resetCsrf();
    setMe(null);
    window.location.assign("/login");
  }, []);

  return (
    <Ctx.Provider
      value={{ me, config, settings, loading, isAdmin: me?.user.role === "admin", refresh, logout }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useAuth() {
  return useContext(Ctx);
}
