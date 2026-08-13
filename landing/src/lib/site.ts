// Env-injected white-label identity + app links, resolved at build time. The
// landing bakes these into the static HTML (rebuild the image after changing
// them); the SPA reads its own copy at runtime from the API. The Openvisor
// defaults keep an unconfigured build shippable placeholder content.
const env = import.meta.env;

// App links. APP_URL is injected per environment (compose.dev.yml locally,
// Docker build arg in production) and falls back to a placeholder app origin.
export const appUrl = env.APP_URL || 'https://app.openvisor.example.com';
export const signupUrl = `${appUrl}/signup`;
export const aiIntakeUrl = `${appUrl}/ai`;
export const programsUrl = `${appUrl}/programs`;

// Brand identity. BRAND_NAME/CONSULTANT_NAME also feed the {{BRAND_NAME}} /
// {{CONSULTANT_NAME}} substitution in the content loader (see content.ts).
export const brandName = env.BRAND_NAME || 'Openvisor';
export const consultantName = env.CONSULTANT_NAME || 'Consultant';
export const brandColorPrimary = env.BRAND_COLOR_PRIMARY || '#22d3ee';
export const brandColorSecondary = env.BRAND_COLOR_SECONDARY || '#7c3aed';

// The gradient default hexes global.css falls back to; used to decide whether a
// build needs a :root colour override emitted in <head> (see Base.astro).
export const defaultColorPrimary = '#22d3ee';
export const defaultColorSecondary = '#7c3aed';

// Wordmark split: a domain-like brand renders two-tone on its last ".", so
// "example.io" reads "example" + gradient ".io". The stock "Openvisor" brand
// splits before its "visor" tail instead, matching the brand-kit lockup
// (landing/public/brand/openvisor-logo.svg); any other dotless name stays one
// tone.
const lastDot = brandName.lastIndexOf('.');
export const wordmarkHead =
  lastDot > 0 ? brandName.slice(0, lastDot) : brandName === 'Openvisor' ? 'Open' : brandName;
export const wordmarkTail =
  lastDot > 0 ? brandName.slice(lastDot) : brandName === 'Openvisor' ? 'visor' : '';
