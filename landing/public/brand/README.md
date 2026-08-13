# Brand assets - the Openvisor mark

An open ring with one tapered spoke in orbit: the open hub of one spoke in the network (and an O). It shares its 32-unit grid and tapered-arc grammar with the Scalevisor mark it federates into, in the Openvisor gradient (violet `#7c3aed` → cyan `#22d3ee`).

The in-page mark is `landing/src/components/Mark.astro` (gradient stops read the build's brand colors); `/favicon.svg` is the same geometry on a dark plate, inset 20% (`landing/src/pages/favicon.svg.ts`); the SPA carries the identical geometry in `app/src/components/ui.tsx` (`BrandMark`) and `app/public/favicon.svg`.

## Files

| File | Use |
| --- | --- |
| `openvisor-mark.svg` | Vector master. Transparent, brand gradient baked in. Prefer this anywhere vectors are accepted. |
| `openvisor-mark-2048.png` | Transparent PNG for print, large slides, press. |
| `openvisor-mark-1024.png` | Transparent PNG, the general-purpose one. |
| `openvisor-mark-512.png` | Transparent PNG for avatars and small embeds. |
| `openvisor-mark-white-1024.png` | Flat white knockout, for dark or photographic grounds where the gradient loses contrast. |
| `openvisor-mark-ink-1024.png` | Flat ink (`#0b1220`), for light single-colour printing. |

## Regenerating

```sh
cd landing/public/brand
for s in 2048 1024 512; do rsvg-convert -w $s -h $s openvisor-mark.svg -o openvisor-mark-$s.png; done
```

For the single-colour variants, swap both `url(#omark)` references for `#ffffff` / `#0b1220` in a copy of the master, then render at 1024.

## Clear space and minimum size

The 32-unit box carries ~8% padding on each side; keep at least that much clear space around the mark. Below ~16px the ring gap closes up - use `/favicon.svg` there, whose dark plate keeps it readable.

White-labeling: replace the files here and the two inline copies (`Mark.astro`, `BrandMark` in the SPA) with your own mark; the gradient colors already follow `BRAND_COLOR_PRIMARY` / `BRAND_COLOR_SECONDARY` where they are wired.

## Lockup (mark + wordmark)

The full logo: the mark at the left of the wordmark, `Open` in ink (`#0b1220`) and `visor` in the brand gradient, set in Inter Bold with -0.02em tracking and outlined to paths so no font is required. The gradient tail mirrors the Scalevisor lockup, whose wordmark ends the same way: the shared `visor` is the family tie.

| File | Use |
| --- | --- |
| `openvisor-logo.svg` | Vector master for light grounds. |
| `openvisor-logo-white.svg` | White wordmark head, for dark grounds; the gradient tail and mark are unchanged. |
| `openvisor-logo-2048.png`, `openvisor-logo-1024.png` | Transparent PNG exports of the light-ground lockup. |
| `openvisor-logo-white-2048.png`, `openvisor-logo-white-1024.png` | Transparent PNG exports of the dark-ground lockup. |
