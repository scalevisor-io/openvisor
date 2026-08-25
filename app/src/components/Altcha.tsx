import { useEffect, useRef } from "react";
// Registers the <altcha-widget> custom element.
import "altcha";

// Wraps the Altcha web component. Feeds the challenge JSON fetched from the API
// and reports the solved base64 payload up via onVerified.
export default function Altcha({
  challenge,
  onVerified,
}: {
  challenge: Record<string, unknown> | null;
  onVerified: (payload: string | null) => void;
}) {
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    function onStateChange(ev: Event) {
      const detail = (ev as CustomEvent).detail as { state?: string; payload?: string } | undefined;
      if (detail?.state === "verified" && detail.payload) {
        onVerified(detail.payload);
      } else {
        onVerified(null);
      }
    }
    el.addEventListener("statechange", onStateChange);
    return () => el.removeEventListener("statechange", onStateChange);
    // `challenge` matters: the widget only mounts once the challenge arrives,
    // so the listener must attach on that render, not the initial null one.
  }, [onVerified, challenge]);

  if (!challenge) return null;

  return (
    // Keyed on the challenge so a fresh one REPLACES the widget rather than
    // reusing it. The widget latches "verified" and will not re-solve in place,
    // so after a failed submit (which burns the solved challenge server-side)
    // the same element would sit there looking verified while holding a payload
    // the server now rejects as a replay - and a submit button that never
    // re-enables. The key goes on the wrapper because the altcha package's own
    // JSX typing for <altcha-widget> does not accept one.
    <div className="field" key={String(challenge.challenge ?? "")}>
      <altcha-widget
        ref={ref as never}
        challengejson={JSON.stringify(challenge)}
        hidefooter
        hidelogo
      />
    </div>
  );
}
