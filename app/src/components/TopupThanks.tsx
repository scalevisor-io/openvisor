import { useEffect, useRef, useState } from "react";
import { formatCredits } from "./ui";

// Eases the credited amount from 0 to `target` (metered count-up, the one
// bold element of the confirmation). Reduced motion renders the final value
// immediately.
function useCountUp(target: number, durationMs = 900, delayMs = 250): number {
  const reduced =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const [value, setValue] = useState(reduced ? target : 0);

  useEffect(() => {
    if (reduced) {
      setValue(target);
      return;
    }
    let raf = 0;
    const timer = window.setTimeout(() => {
      const start = performance.now();
      const tick = (now: number) => {
        const p = Math.min(1, (now - start) / durationMs);
        const eased = 1 - Math.pow(1 - p, 3);
        setValue(p < 1 ? Math.round(target * eased) : target);
        if (p < 1) raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    }, delayMs);
    return () => {
      window.clearTimeout(timer);
      cancelAnimationFrame(raf);
    };
  }, [target, durationMs, delayMs, reduced]);

  return value;
}

// Post-checkout confirmation: shown on /billing?status=success once the
// topup transaction is visible in the ledger (see Billing.tsx).
export default function TopupThanks({
  amount,
  balance,
  reference,
  onClose,
}: {
  amount: number;
  balance: number;
  reference: string;
  onClose: () => void;
}) {
  const shown = useCountUp(amount);
  const doneRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    doneRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay topup-thanks-overlay" onMouseDown={onClose}>
      <div
        className="topup-thanks"
        role="dialog"
        aria-modal="true"
        aria-label="Top-up confirmed"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <i className="tt-tick tl" aria-hidden="true" />
        <i className="tt-tick tr" aria-hidden="true" />
        <i className="tt-tick bl" aria-hidden="true" />
        <i className="tt-tick br" aria-hidden="true" />

        <div className="tt-eyebrow">Top-up confirmed</div>
        <div className="tt-amount grad-text">
          +{formatCredits(shown)}
        </div>
        <div className="tt-unit">credits</div>

        <div className="tt-rows">
          <div className="tt-row">
            <span>New balance</span>
            <b>{formatCredits(balance)}</b>
          </div>
          <div className="tt-row">
            <span>Reference</span>
            <b className="mono">{reference}</b>
          </div>
        </div>

        <p className="tt-note">
          Thank you for the trust. Your credits are live: every run is metered against them and
          itemized in the ledger below.
        </p>

        <button ref={doneRef} type="button" className="btn btn-primary btn-block" onClick={onClose}>
          Done
        </button>
      </div>
    </div>
  );
}
