# Openvisor - visual identity

Sober, functional, fast. Dark-first.

## Tokens

```css
:root {
  /* Navy gray scale */
  --gray-0: #090b11; --gray-50: #141925; --gray-100: #283044; --gray-200: #3d4663;
  --gray-300: #505d84; --gray-400: #6474a2; --gray-500: #8490b5; --gray-600: #a3acc8;
  --gray-700: #c3cadb; --gray-800: #e3e6ee; --gray-900: #f3f4f7; --gray-999: #ffffff;

  /* Legacy blue accent (links, secondary) */
  --accent-light: #61a1f6; --accent-dark: #002656;

  /* Speed accent - electric cyan → violet */
  --speed-from: #22d3ee; --speed-to: #7c3aed;
  --gradient-speed: linear-gradient(135deg, #22d3ee, #7c3aed);

  /* Alpha banner red */
  --alpha-red: #dc2626;

  --font-system: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  --font-brand: var(--font-system);
}
```

- Fonts: the landing defaults to the system font stack via `--font-brand`. To use a brand font, drop its files into `landing/public/fonts/`, declare the `@font-face` in the landing's global CSS, and repoint `--font-brand` at it.
- Dark theme default (`--gray-0` background, `--gray-800` text). The landing's light theme is a tokens flip (`html[data-theme='light']` inverts the navy scale and darkens the accent ink `--speed-ink`), enabled per instance by `theme.mode` in site.yml (`dark` | `light` | `toggle` - toggle adds a header switch remembered in localStorage); the SPA stays dark-only for the alpha.
- Consultant photo (§consultant photo): the portrait is the hero visual (gradient-framed square, name + first role as caption) and the 48 px face on the Direct quote card in place of the pen glyph. It comes from a committed `public/` asset named in `profile.hero.portrait` and/or the admin upload (Settings → Consultant photo), which replaces the committed one at runtime once the API serves it; without either, both spots render photo-less.
- Landing theme surface (§theme): fonts, colours, radius and the defence-tech ornaments (corner ticks, hero grid) are site.yml tokens emitted as `:root` overrides by `Base.astro`; components never read a brand value directly.
- CTAs and key highlights use `--gradient-speed` (gradient background on buttons, gradient text-clip on hero keywords). Everything else stays sober navy.
- Red "alpha" banner: thin bar or badge in the app header, `--alpha-red` background, white text, e.g. "⚠ alpha".
- Aesthetic: generous whitespace, 1px borders in `--gray-100`, subtle noise/gradient backgrounds, rounded corners ~0.75rem, shadows only on elevation.
- Logo: the brand-name text wordmark in `--font-brand`, with an accent span (e.g. the final syllable or TLD) in the speed gradient.
