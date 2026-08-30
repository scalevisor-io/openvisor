import { useEffect, useState } from "react";

/* Below this width the sidebar stops being a grid column and becomes an overlay
   drawer opened from the header, and the project page's chat rail stops being a
   column and becomes a floating sheet. Keep in sync with the `max-width: 820px`
   block in styles/global.css - the two describe the same breakpoint from each
   side. */
export const DRAWER_QUERY = "(max-width: 820px)";

export function isNarrowLayout(): boolean {
  return window.matchMedia?.(DRAWER_QUERY).matches ?? false;
}

/* Which layout applies is a fact the markup needs, not only the stylesheet: the
   brand-row button closes the drawer on narrow screens and collapses the rail on
   wide ones, and the chat sheet must open on a tap instead of on arrival. */
export function useNarrowLayout(): boolean {
  const [narrow, setNarrow] = useState(isNarrowLayout);
  useEffect(() => {
    const mq = window.matchMedia?.(DRAWER_QUERY);
    if (!mq) return;
    const onChange = (e: MediaQueryListEvent) => setNarrow(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return narrow;
}
