# Truhowl Brand System & Canonical Identity Specification

## Brand Positioning
**Truhowl** — Your codebase, kept compatible.
Capital T, lowercase rest, single word. Never `TruHowl`, `TrueHowl`, or `TRUHOWL` (screaming caps reserved for CLI banners only).

Truhowl watches the SDKs and APIs your software depends on, repairs breaking changes, verifies the result with clean-room replay, and delivers the pull request.

---

## Canonical Mascot Specification

The Truhowl mascot is a small, rounded, bioluminescent creature inspired by a vigilant coyote/fox. It represents quiet vigilance, autonomy, and mathematical trust.

### Master Geometry & Proportions
- **Body & Posture**: Compact, rounded, poised sitting posture. Organic curvilinear geometry with width-to-height ratio approximately `1 : 1.15`.
- **Ears**: Prominent upright triangular coyote ears (~35-40% of head height), angled slightly outward (`15°`), with glowing inner ear pinna gradients.
- **Muzzle & Face**: Sleek tapering muzzle ending in a gentle rounded chin. Minimal, elegant facial contours.
- **Eyes**: Vigilant yet calm, stylized crescent arcs or luminous almond orbs emitting soft celestial light.
- **Nose**: Subtle miniature rounded triangle in deep violet (`#1E1B4B`).
- **Tail**: Expressive, fluffy curved tail wrapping gracefully along the base with a high-luminescence tip.
- **Silhouette**: High-contrast, instantly recognizable silhouette maintaining clear legibility down to 16×16 px favicon scales.

### Color Palette & Bioluminescent Gradient
- **Deep Canvas Background**: Dark navy (`#0A0E17` / `#0D1117`) providing high contrast.
- **Luminescent Gradients**:
  - Primary Core: White Celestial (`#FFFFFF`) transitioning into Cyan Flare (`#38BDF8`).
  - Mid-Tone Flow: Azure Blue (`#2563EB`) blending seamlessly into Electric Indigo (`#6366F1`).
  - Perimeter & Shadows: Deep Violet (`#8B5CF6`) down to Twilight Navy (`#1E1B4B`).
- **Aura & Glow**: Multi-stage Gaussian blur glow (`filter: drop-shadow(0 0 12px rgba(56, 189, 248, 0.45))`).

### Derived Asset System
All visual representations are strictly derived from this identical master vector geometry:
1. **Logo / Wordmark**: Canonical mascot silhouette positioned left of modern geometric sans typography "Truhowl" (`#F0F6FC`) with status-green dot.
2. **Favicon**: Optimized high-contrast 32×32 vector icon with amplified ear and eye silhouettes and concentrated cyan glow.
3. **GitHub / Social Avatar**: Centered circular avatar on `#0A0E17` with subtle orbital rim lighting and inner gradient depth.
4. **Website Hero Mascot**: Full-fidelity vector mascot featuring ambient particle glow and floating aura rings.
5. **Empty State Mascot**: Peaceful resting/coiled pose of the mascot with lowered glow intensity, signaling calm stability ("All systems verified green").
6. **Loading State**: Mascot with an active breathing / pulsing CSS keyframe radial glow (`0.8` to `1.2` scale, 2.4s cycle).

---

## Typography & UI Theme
- **Headings & Brand**: Modern geometric sans (`Inter`, `-apple-system`, `BlinkMacSystemFont`, `"Segoe UI"`).
- **Code & Proofs**: Monospace (`SFMono-Regular`, `Consolas`, `Liberation Mono`, `Menlo`).
- **UI Surface**: Dark theme with high contrast border hierarchy (`#30363d`), card background (`#161b22`), and status colors:
  - Green (`#238636` / `#3fb950`): Behavioral Verified & Replay Pass.
  - Amber (`#d29922`): Compile Only / Warning / Needs Attention.
  - Red (`#da3633` / `#f85149`): Refusal / Verification Failure.
  - Blue (`#2f81f7` / `#58a6ff`): Delivered PR / Public Actions.
