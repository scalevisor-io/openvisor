import { useCallback, useEffect, useState } from "react";

import { authApi } from "./endpoints";
import { useAuth } from "./auth";

/**
 * Challenge lifecycle for the public auth forms (§captcha).
 *
 * A solved challenge is single-use on the server, so a failed submit must fetch
 * a fresh one or the retry is rejected as a replay and the user is stuck on an
 * error they cannot clear. `reset()` is what every failed submit calls.
 */
export function useAltcha() {
  const { settings } = useAuth();
  // Assume on until the settings land: the widget appearing a beat late is
  // better than a form that submits without a solution and 400s.
  const enabled = settings?.altcha_enabled ?? true;

  const [challenge, setChallenge] = useState<Record<string, unknown> | null>(null);
  const [payload, setPayload] = useState<string | null>(null);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    if (!enabled) return;
    setPayload(null);
    setError(false);
    authApi
      .altcha()
      .then(setChallenge)
      .catch(() => {
        setChallenge(null);
        setError(true);
      });
  }, [enabled]);

  // Waits for `settings` so no challenge is fetched on a deployment that has
  // the captcha switched off.
  useEffect(() => {
    if (settings) load();
  }, [settings, load]);

  return {
    enabled,
    challenge,
    payload,
    setPayload,
    /** The challenge could not be fetched; the form cannot be submitted. */
    error,
    /** Fetch a fresh challenge. Call after any failed submit. */
    reset: load,
    /** Whether the form may be submitted yet. */
    ready: !enabled || Boolean(payload),
  };
}
