# Penpal brand assets

- `penpal-mark.svg` / `penpal-mark-dark.svg`: transparent standalone symbols.
- `penpal-lockup-light.svg`: blue mark and dark navy Penpal outlined wordmark, transparent; use on light backgrounds.
- `penpal-lockup-dark.svg`: blue mark and white Penpal outlined wordmark, transparent; use on dark backgrounds.
- A 1280 × 640 `penpal-social-preview.png` is included in the downloadable asset package (not yet part of this repository). To set it as the link thumbnail, upload it under GitHub repository **Settings → General → Social preview**.

**Geometry:** original B3 translucent overlay: left edge + asymmetric continuous cubic Bézier; complete circle in front. The Bézier body and circle are editable vector shapes.

**Palette:** circle `#4A7AF8 → #76A2FA → #BAD0FF` (90% opacity); stem `#85AAF9 → #A5BDFC → #D0DEFF` (69% shape opacity with tapered stop opacity). Wordmark `#0F172A` on light, `#F6F8FF` on dark.

**Typography:** Inter SemiBold converted to SVG paths for consistent rendering. No font file embedded or distributed.

**Recommended README display:** 400px wide, left aligned, with positioning line and tagline underneath.

**Seam refinement:** the stem now uses a vertical alpha mask. Its visibility fades smoothly to zero inside the translucent circle, removing the conspicuous internal contour without changing the asymmetric Bézier outline or the frontmost circle.
