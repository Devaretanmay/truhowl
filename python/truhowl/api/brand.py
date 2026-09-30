# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Canonical Brand Assets & Mascot Vector Generator for Truhowl.

Strictly implements the canonical mascot specification (B11):
- Small rounded glowing creature (fox/coyote-like)
- Simple, instantly recognizable silhouette
- Soft white/cyan/azure/violet glow aura
- Dark navy canvas compatibility
- 100% vector SVG rendering across Favicon, Logo, Hero, Avatar, Empty & Loading states.
"""

from __future__ import annotations


def _mascot_defs(glow_id: str, grad_id: str) -> str:
    return f"""
    <defs>
      <linearGradient id="{grad_id}" x1="0%" y1="0%" x2="100%" y2="100%">
        <stop offset="0%" stop-color="#FFFFFF" />
        <stop offset="25%" stop-color="#38BDF8" />
        <stop offset="65%" stop-color="#2563EB" />
        <stop offset="100%" stop-color="#7C3AED" />
      </linearGradient>
      <linearGradient id="{grad_id}-inner" x1="50%" y1="0%" x2="50%" y2="100%">
        <stop offset="0%" stop-color="#BAE6FD" />
        <stop offset="100%" stop-color="#38BDF8" />
      </linearGradient>
      <radialGradient id="{grad_id}-tail" cx="50%" cy="50%" r="50%">
        <stop offset="0%" stop-color="#7DD3FC" />
        <stop offset="80%" stop-color="#3B82F6" />
        <stop offset="100%" stop-color="#1E3A8A" />
      </radialGradient>
      <filter id="{glow_id}" x="-30%" y="-30%" width="160%" height="160%">
        <feGaussianBlur in="SourceGraphic" stdDeviation="4" result="blur" />
        <feMerge>
          <feMergeNode in="blur" />
          <feMergeNode in="SourceGraphic" />
        </feMerge>
      </filter>
    </defs>
    """


def canonical_mascot_svg(size: int = 128, animated: bool = False) -> str:
    """Generate the canonical glowing coyote/fox mascot SVG."""
    anim_style = """
      @keyframes howlPulse {
        0%, 100% { transform: scale(1); filter: drop-shadow(0 0 8px rgba(56, 189, 248, 0.5)); }
        50% { transform: scale(1.03); filter: drop-shadow(0 0 16px rgba(56, 189, 248, 0.85)); }
      }
      .glowing-creature { transform-origin: 50% 60%; animation: howlPulse 3s ease-in-out infinite; }
    """ if animated else ""

    return f"""<svg width="{size}" height="{size}" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
  <style>{anim_style}</style>
  {_mascot_defs("glow-canonical", "grad-canonical")}
  <g class="{'glowing-creature' if animated else ''}" filter="url(#glow-canonical)">
    <!-- Bushy Curled Tail with glowing tip -->
    <path d="M 68 76 C 85 75 88 56 78 46 C 72 40 64 45 66 54 C 68 62 76 68 68 76 Z"
          fill="url(#grad-canonical-tail)" opacity="0.95" />
    <circle cx="75" cy="46" r="3.5" fill="#E0F2FE" />

    <!-- Rounded Body / Poised Sitting Posture -->
    <path d="M 34 82 C 34 62 42 54 50 54 C 58 54 66 62 66 82 C 66 86 34 86 34 82 Z"
          fill="url(#grad-canonical)" />

    <!-- Left Ear (Upright Fox/Coyote) -->
    <path d="M 36 42 L 30 20 C 35 22 42 27 45 34 Z" fill="url(#grad-canonical)" />
    <path d="M 34 38 L 31 23 C 35 25 40 29 42 34 Z" fill="url(#grad-canonical-inner)" opacity="0.8" />

    <!-- Right Ear (Upright Fox/Coyote) -->
    <path d="M 64 42 L 70 20 C 65 22 58 27 55 34 Z" fill="url(#grad-canonical)" />
    <path d="M 66 38 L 69 23 C 65 25 60 29 58 34 Z" fill="url(#grad-canonical-inner)" opacity="0.8" />

    <!-- Sleek Rounded Head & Cheeks -->
    <ellipse cx="50" cy="40" rx="16" ry="14" fill="url(#grad-canonical)" />

    <!-- Calm Luminous Crescent Eyes (Vigilant & Trustworthy) -->
    <path d="M 42 38 Q 45 35 48 38" stroke="#FFFFFF" stroke-width="1.8" stroke-linecap="round" fill="none" />
    <path d="M 58 38 Q 55 35 52 38" stroke="#FFFFFF" stroke-width="1.8" stroke-linecap="round" fill="none" />

    <!-- Delicate Muzzle & Nose Point -->
    <ellipse cx="50" cy="43.5" rx="3" ry="2" fill="#E0F2FE" opacity="0.7" />
    <path d="M 49 43 L 51 43 L 50 44.5 Z" fill="#1E1B4B" />

    <!-- Chest Aura Light -->
    <circle cx="50" cy="62" r="5" fill="#E0F2FE" opacity="0.4" />
  </g>
</svg>"""


def favicon_svg() -> str:
    """Generate 32x32 high-contrast favicon SVG for browser tabs."""
    return canonical_mascot_svg(size=32, animated=False)


def avatar_svg(size: int = 120) -> str:
    """Generate a circular GitHub/account avatar badge on dark navy with orbital glow."""
    mascot = canonical_mascot_svg(size=int(size * 0.82), animated=False)
    offset = int(size * 0.09)
    return f"""<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" fill="none" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="avatar-bg" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#161B22" />
      <stop offset="85%" stop-color="#0A0E17" />
      <stop offset="100%" stop-color="#030712" />
    </radialGradient>
  </defs>
  <circle cx="{size/2}" cy="{size/2}" r="{size/2 - 2}" fill="url(#avatar-bg)" stroke="#38BDF8" stroke-width="1.5" stroke-opacity="0.4" />
  <g transform="translate({offset}, {offset})">
    {mascot}
  </g>
</svg>"""


def logo_svg(height: int = 36) -> str:
    """Generate horizontal brand logo (mascot + geometric wordmark)."""
    icon_size = int(height * 1.1)
    return f"""<svg height="{height}" viewBox="0 0 170 42" fill="none" xmlns="http://www.w3.org/2000/svg">
  <g transform="translate(0, 0)">
    {canonical_mascot_svg(size=icon_size)}
  </g>
  <text x="46" y="27" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, sans-serif"
        font-size="20" font-weight="700" fill="#F0F6FC" letter-spacing="-0.5px">Truhowl</text>
  <circle cx="128" cy="18" r="3" fill="#3FB950" />
</svg>"""


def empty_state_svg(size: int = 140) -> str:
    """Generate calm resting posture mascot SVG for empty states."""
    return f"""<svg width="{size}" height="{size}" viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg">
  {_mascot_defs("glow-empty", "grad-empty")}
  <g filter="url(#glow-empty)" opacity="0.8">
    <ellipse cx="60" cy="92" rx="35" ry="8" fill="#161B22" />
    <!-- Coiled Peaceful Resting Coyote -->
    <path d="M 40 85 C 32 75 32 60 48 55 C 65 50 82 58 84 72 C 86 85 65 90 40 85 Z" fill="url(#grad-empty)" />
    <!-- Tail draped over back -->
    <path d="M 75 75 C 88 70 86 52 70 56 C 60 58 64 74 75 75 Z" fill="url(#grad-empty-tail)" />
    <circle cx="70" cy="56" r="3" fill="#E0F2FE" />
    <!-- Ears resting gently back -->
    <path d="M 42 56 L 33 44 C 38 46 45 50 46 54 Z" fill="url(#grad-empty)" />
    <path d="M 52 54 L 46 42 C 50 45 55 48 56 53 Z" fill="url(#grad-empty)" />
    <!-- Sleeping crescent eyes -->
    <path d="M 45 64 Q 48 67 51 64" stroke="#FFFFFF" stroke-width="1.5" stroke-linecap="round" fill="none" />
  </g>
</svg>"""


def loading_state_svg(size: int = 64) -> str:
    """Generate active pulsing creature SVG for background loading & verification."""
    return canonical_mascot_svg(size=size, animated=True)
