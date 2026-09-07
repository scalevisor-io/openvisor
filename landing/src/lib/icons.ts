// Hand-drawn inline SVG icon set shared by the home sections, referenced by id
// from site.yml. One stroke grammar (24px grid, 1.7 stroke, round caps) so every
// visual on the page reads as the same family.
// Hand-drawn inline SVG icon set - one stroke grammar (24px grid, 1.7 stroke,
// round caps) so every visual on the page reads as the same family.
const icon = (paths: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

export const icons: Record<string, string> = {
  pulse: icon('<path d="M3 12h4l3-7 4 14 3-7h4"/>'),
  thread: icon(
    '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><rect x="9.5" y="9.2" width="5" height="4" rx="0.8"/><path d="M10.7 9.2V8.1a1.3 1.3 0 0 1 2.6 0v1.1"/>'
  ),
  terminal: icon(
    '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="m8 9 3 3-3 3"/><path d="M13 15h4"/>'
  ),
  pr: icon(
    '<circle cx="6" cy="6" r="2.6"/><circle cx="6" cy="18" r="2.6"/><circle cx="18" cy="18" r="2.6"/><path d="M6 8.6v6.8"/><path d="M12 6h3a3 3 0 0 1 3 3v6.4"/>'
  ),
  meter: icon(
    '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l4.2-5"/><circle cx="12" cy="16" r="1"/>'
  ),
  key: icon(
    '<circle cx="7.5" cy="16.5" r="3.5"/><path d="m10.2 13.8 8.8-8.8"/><path d="m15.5 8.5 2.6 2.6"/><path d="m19 5 2 2"/>'
  ),
  shieldNode: icon(
    '<path d="M12 3l7 2.6v5c0 4.4-2.9 7.5-7 9.4-4.1-1.9-7-5-7-9.4v-5L12 3z"/><circle cx="12" cy="10.8" r="1.6"/><path d="M12 12.4v3"/><path d="M10.6 9.8 8.8 8.4"/><path d="m13.4 9.8 1.8-1.4"/>'
  ),
  pen: icon(
    '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>'
  ),
  globe: icon(
    '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.8 2.6 2.8 14.4 0 17-2.8-2.6-2.8-14.4 0-17z"/>'
  ),
  badge: icon(
    '<circle cx="12" cy="9" r="5.2"/><path d="m8.6 13.1-1.1 7.6 4.5-2.6 4.5 2.6-1.1-7.6"/>'
  ),
  box: icon(
    '<path d="M21 8.2 12 3 3 8.2v7.6L12 21l9-5.2V8.2z"/><path d="m3 8.2 9 5.2 9-5.2"/><path d="M12 13.4V21"/>'
  ),
  layers: icon('<path d="m12 3-9 5 9 5 9-5-9-5z"/><path d="m3 13 9 5 9-5"/>'),
  zap: icon('<path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/>'),
  check: icon('<path d="m5 13 4 4L19 7"/>'),
  message: icon(
    '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
  ),
};

// Animated status glyphs shown inside the chip - each signals what the phase is
// doing: hourglass (draining) = waiting on a person, chrono = timing an
// estimate, spinner = the agent is actively building (Claude Code style),
// broadcast = the demo is live, check = delivered. Styled + animated in the
// <style> block; `stroke/color: currentColor` so they inherit the chip's tone
// (amber / cyan / green).
export const glyphs: Record<string, string> = {
  hourglass:
    '<svg class="chip-glyph chip-glyph--hourglass" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 4h12M6 20h12M7.5 4 12 12 7.5 20M16.5 4 12 12 16.5 20"/><path class="hg-sand" d="M8.6 18.8h6.8L12 13z" fill="currentColor" stroke="none"/></svg>',
  chrono:
    '<svg class="chip-glyph chip-glyph--chrono" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="13.5" r="7"/><path d="M12 6.5V4M10 4h4"/><path class="chrono-hand" d="M12 13.5V8.2" stroke-width="2"/></svg>',
  spinner: '<span class="ascii-spin" aria-hidden="true">/</span>',
  broadcast:
    '<svg class="chip-glyph chip-glyph--live" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/><path class="live-w1" d="M8.3 8.3a5.2 5.2 0 0 0 0 7.4M15.7 8.3a5.2 5.2 0 0 1 0 7.4"/><path class="live-w2" d="M5.8 5.8a8.8 8.8 0 0 0 0 12.4M18.2 5.8a8.8 8.8 0 0 1 0 12.4"/></svg>',
  check:
    '<svg class="chip-glyph chip-glyph--check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path class="done-check" d="M5 13l4 4L19 7"/></svg>',
};

// Social marks for the profile's "elsewhere" row (simple-icons geometry,
// currentColor fill). `link` is the fallback for an unknown id.
const mark = (d: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="${d}"/></svg>`;

export const socialIcons: Record<string, string> = {
  github: mark(
    'M12 .3a12 12 0 0 0-3.8 23.4c.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.6-1.4-1.4-1.8-1.4-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.5.1-3.2 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0c2.3-1.5 3.3-1.2 3.3-1.2.6 1.7.2 2.9.1 3.2.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 12 .3z'
  ),
  linkedin: mark(
    'M20.4 20.4h-3.5v-5.6c0-1.3 0-3-1.9-3s-2.1 1.4-2.1 2.9v5.7H9.4V9h3.4v1.6c.5-.9 1.6-1.9 3.4-1.9 3.6 0 4.3 2.4 4.3 5.5v6.2zM5.3 7.4a2.1 2.1 0 1 1 0-4.1 2.1 2.1 0 0 1 0 4.1zm1.8 13H3.6V9h3.5v11.4zM22.2 0H1.8C.8 0 0 .8 0 1.7v20.6c0 .9.8 1.7 1.8 1.7h20.4c1 0 1.8-.8 1.8-1.7V1.7C24 .8 23.2 0 22.2 0z'
  ),
  medium: mark(
    'M13.5 12a6.8 6.8 0 1 1-13.5 0 6.8 6.8 0 0 1 13.5 0zm7.4 0c0 3.5-1.5 6.4-3.4 6.4s-3.4-2.9-3.4-6.4 1.5-6.4 3.4-6.4 3.4 2.9 3.4 6.4zm3.1 0c0 3.2-.5 5.7-1.2 5.7S21.6 15.2 21.6 12s.5-5.7 1.2-5.7 1.2 2.5 1.2 5.7z'
  ),
  book: mark(
    'M4 2h13a3 3 0 0 1 3 3v15.5a.5.5 0 0 1-.5.5H6a2 2 0 0 0-2 2V2zm2 2v13.2A4 4 0 0 1 7 17h11V5a1 1 0 0 0-1-1H6z'
  ),
  link: mark(
    'M10.6 13.4a1 1 0 0 1 0 1.4 3 3 0 0 1-4.2 0l-2.1-2.1a3 3 0 0 1 0-4.2l2.1-2.1a3 3 0 0 1 4.2 0 1 1 0 0 1-1.4 1.4 1 1 0 0 0-1.4 0L5.7 9.9a1 1 0 0 0 0 1.4l2.1 2.1a1 1 0 0 0 1.4 0 1 1 0 0 1 1.4 0zm2.8-2.8a1 1 0 0 1 0-1.4 3 3 0 0 1 4.2 0l2.1 2.1a3 3 0 0 1 0 4.2l-2.1 2.1a3 3 0 0 1-4.2 0 1 1 0 0 1 1.4-1.4 1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4l-2.1-2.1a1 1 0 0 0-1.4 0 1 1 0 0 1-1.4 0zM9 15l6-6a1 1 0 0 1 1.4 1.4l-6 6A1 1 0 0 1 9 15z'
  ),
};
