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
    <div className="field">
      <altcha-widget
        ref={ref as never}
        challengejson={JSON.stringify(challenge)}
        hidefooter
        hidelogo
      />
    </div>
  );
}
