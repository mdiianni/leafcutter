"""
box_utils.py — Shared terminal box-drawing utilities for AWS Cost Analyser.

IMPORTANT — Emoji & Wide-Character Alignment
=============================================
Python's f-string `:<N>` format spec counts *characters*, not *display columns*.
Emoji characters (💸 💰 🔍 ✅) are typically 2 display columns wide on most
terminals, but Python counts them as 1 character.  Variation-selector emoji
(⚠️ = U+26A0 + U+FE0F) also display as 2 columns while Python sees 2 chars
that are individually "narrow".

Rule: **never use `:<N>` padding on a line that contains emoji or other wide
chars**.  Instead, build the content string freely and pass it to `_bl()`,
which measures the true display width via `_dw()` and applies the correct
amount of trailing space so the right `║` border always lands in the same
column.

Usage
-----
    from box_utils import _bl, BORDER_TOP, BORDER_MID, BORDER_BOT

    print(BORDER_TOP)
    print(_bl("  My header line"))
    print(BORDER_MID)
    print(_bl(f"  💸 Cost : ${cost:>8.2f} / month"))
    print(_bl(f"  🔍 Opps : {n}"))
    print(BORDER_BOT)
"""

import unicodedata

# Inner width of the box (display columns between the ║ borders).
# Set wide enough to accommodate the longest typical content line.
BOX_W = 76


def _dw(s: str) -> int:
    """Return the terminal display width of *s*.

    Rules applied:
    * Wide (W) and Fullwidth (F) characters count as 2 columns.
    * A base character followed immediately by the variation selector U+FE0F
      (which upgrades it to emoji presentation) counts as 2 columns total.
    * Standalone variation selectors (U+FE0F, U+FE0E) count as 0 columns.
    * All other characters count as 1 column.
    """
    w = 0
    i = 0
    while i < len(s):
        ch = s[i]
        # Base char + U+FE0F variation selector → emoji, render as 2 wide
        if i + 1 < len(s) and s[i + 1] == "\ufe0f":
            w += 2
            i += 2
            continue
        # Lone variation selector (U+FE0E / U+FE0F) → 0-width, skip
        if ch in ("\ufe0f", "\ufe0e"):
            i += 1
            continue
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
        i += 1
    return w


def _bl(content: str, width: int = BOX_W) -> str:
    """Wrap *content* in box borders: ``║<content padded to *width* cols>║``.

    Content that exceeds *width* display columns is truncated with ``…`` so
    the box never breaks.  Trailing whitespace in *content* is stripped first
    because the function handles all padding itself.
    """
    content = content.rstrip()
    dw = _dw(content)
    if dw > width:
        # Truncate, respecting multi-byte characters
        out, used = "", 0
        for ch in content:
            cw = _dw(ch)
            if used + cw > width - 1:
                out += "…"
                used += 1
                break
            out += ch
            used += cw
        content, dw = out, used
    return f"║{content}{' ' * (width - dw)}║"


# Pre-built border strings (width = BOX_W + 2 for the corner chars).
BORDER_TOP = "╔" + "═" * BOX_W + "╗"
BORDER_MID = "╠" + "═" * BOX_W + "╣"
BORDER_BOT = "╚" + "═" * BOX_W + "╝"
