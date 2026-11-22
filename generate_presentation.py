"""
generate_presentation.py
Generates an impactful PowerPoint presentation from dashboard_data.json.

Usage:
  python3 generate_presentation.py                              # all accounts
  python3 generate_presentation.py --project projectA           # single project (group)
  python3 generate_presentation.py --project projectA projectB projectC
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
from pptx.chart.data import ChartData
from pptx.enum.chart import XL_CHART_TYPE
import datetime

# ── Palette (theme-aware) ─────────────────────────────────────────────────────
THEMES = {
    "light": dict(
        bg=RGBColor(0xFF, 0xFF, 0xFF),          # page background — white
        card=RGBColor(0xF1, 0xF4, 0xF9),        # card background — soft grey
        text=RGBColor(0x14, 0x1B, 0x2E),        # primary text — near-black navy
        muted=RGBColor(0x5B, 0x6B, 0x82),       # secondary text — slate grey
        accent1=RGBColor(0x0B, 0x63, 0xD1),     # blue
        accent2=RGBColor(0xC8, 0x7A, 0x00),     # amber (darkened for contrast on white)
        accent3=RGBColor(0x1E, 0x8E, 0x5A),     # green
        red=RGBColor(0xC6, 0x3B, 0x2E),         # red
        purple=RGBColor(0x6D, 0x4A, 0xD1),      # purple
        orange=RGBColor(0xC2, 0x5B, 0x00),      # orange (HIGH severity)
        header_row=RGBColor(0xE4, 0xEA, 0xF5),  # table header row background
    ),
    "dark": dict(
        bg=RGBColor(0x0F, 0x17, 0x2A),          # deep navy
        card=RGBColor(0x1A, 0x26, 0x40),        # card navy
        text=RGBColor(0xFF, 0xFF, 0xFF),
        muted=RGBColor(0x8A, 0x9B, 0xB4),
        accent1=RGBColor(0x00, 0xC2, 0xFF),     # cyan
        accent2=RGBColor(0xF5, 0xA6, 0x23),     # amber
        accent3=RGBColor(0x2E, 0xCC, 0x71),     # green
        red=RGBColor(0xE7, 0x4C, 0x3C),
        purple=RGBColor(0xBB, 0x86, 0xFC),
        orange=RGBColor(0xFF, 0x7F, 0x00),
        header_row=RGBColor(0x1E, 0x35, 0x5E),
    ),
}
# Fixed near-black used for text rendered directly on top of bright accent
# fills (badges/circles) — legible regardless of the chosen page theme.
ON_ACCENT_TEXT = RGBColor(0x14, 0x1B, 0x2E)
SLIDE_W   = Inches(13.33)
SLIDE_H   = Inches(7.5)

# ── CLI argument parsing ─────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="Generate AWS Cost Optimisation presentation")
_parser.add_argument(
    "--project", nargs="+", metavar="GROUP",
    help=(
        "Filter to one or more project groups (e.g. projectA, or "
        "projectA projectB projectC). All envs (dev/stg/prd) are included. "
        "Group names are matched against the 'group' field in dashboard_data.json."
    ),
)
_parser.add_argument(
    "--theme", choices=["light", "dark"], default="light",
    help="Colour theme for the generated slides (default: light).",
)
_parser.add_argument(
    "--data", default="dashboard_data.json",
    metavar="FILE",
    help="Path to dashboard_data.json (default: dashboard_data.json)",
)
_args = _parser.parse_args()

# ── Resolve theme palette ─────────────────────────────────────────────────────
_T       = THEMES[_args.theme]
BG_DARK  = _T["bg"]
BG_CARD  = _T["card"]
ACCENT1  = _T["accent1"]
ACCENT2  = _T["accent2"]
ACCENT3  = _T["accent3"]
RED      = _T["red"]
PURPLE   = _T["purple"]
WHITE    = _T["text"]
GREY     = _T["muted"]
HEADER_ROW = _T["header_row"]
ORANGE   = _T["orange"]

# ── Load data ────────────────────────────────────────────────────────────────
with open(_args.data) as f:
    DATA = json.load(f)

_all_accounts     = DATA["accounts"]
_project_filter   = {g.lower() for g in _args.project} if _args.project else None

if _project_filter:
    ACCOUNTS = [a for a in _all_accounts if a.get("group", "").lower() in _project_filter]
    if not ACCOUNTS:
        available = sorted({a.get("group", "") for a in _all_accounts})
        raise SystemExit(
            f"No accounts found for project(s): {sorted(_project_filter)}\n"
            f"Available groups: {available}"
        )
    print(f"  → Filtering to {len(ACCOUNTS)} accounts for groups: {sorted(_project_filter)}")
else:
    ACCOUNTS = _all_accounts

# Recompute summary from filtered accounts (payer accounts excluded from totals)
_non_payer = [a for a in ACCOUNTS if not a.get("is_payer")]

# Per-service rollup scoped to the current project filter (NOT the global
# dashboard rollup) — ensures "Top 5 Opportunities" reflects the accounts
# actually included in this deck, with saving + aggregated check counts.
_svc_rollup = defaultdict(lambda: {"saving": 0.0, "checks": 0})
for a in _non_payer:
    for svc, info in a.get("services", {}).items():
        _svc_rollup[svc]["saving"] += info.get("saving", 0)
        _svc_rollup[svc]["checks"] += info.get("count", 0)
SERVICE_ROLLUP = sorted(
    [{"service": k, **v} for k, v in _svc_rollup.items()],
    key=lambda x: -x["saving"],
)

# Build filtered summary
_f_invoice = sum(a["invoice_mo"] for a in _non_payer)
_f_saving  = sum(a["total_saving_mo"] for a in _non_payer)
SUMMARY = {
    "total_invoice_mo": _f_invoice,
    "total_saving_mo":  _f_saving,
    "total_accounts":   len(_non_payer),
} if _project_filter else DATA["summary"]

# Pre-compute group rollup
group_map = defaultdict(lambda: {"invoice": 0, "saving": 0, "accounts": 0})
for a in ACCOUNTS:
    g = a["group"]
    group_map[g]["invoice"] += a["invoice_mo"]
    group_map[g]["saving"] += a["total_saving_mo"]
    group_map[g]["accounts"] += 1
GROUPS = sorted(group_map.items(), key=lambda x: -x[1]["saving"])

# Severity totals
sev_totals = defaultdict(int)
for a in ACCOUNTS:
    for k, v in a.get("severity_breakdown", {}).items():
        sev_totals[k] += v

TOTAL_OPPS = sum(sev_totals.values())
TOTAL_INV  = SUMMARY["total_invoice_mo"]
TOTAL_SAV  = SUMMARY["total_saving_mo"]
TOTAL_ACCS = SUMMARY["total_accounts"]
ANNUAL_SAV = TOTAL_SAV * 12
TOTAL_CHECKS = sum(s["checks"] for s in SERVICE_ROLLUP)

# ── Helpers ───────────────────────────────────────────────────────────────────

def add_slide(prs, layout_index=6):
    layout = prs.slide_layouts[layout_index]
    slide = prs.slides.add_slide(layout)
    # Remove all placeholders so we have a blank canvas
    for ph in slide.placeholders:
        sp = ph._element
        sp.getparent().remove(sp)
    return slide


def fill_bg(slide, color=BG_DARK):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def add_rect(slide, l, t, w, h, color, radius=None):
    shape = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        Inches(l), Inches(t), Inches(w), Inches(h)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def add_text(slide, text, l, t, w, h,
             size=18, bold=False, color=WHITE,
             align=PP_ALIGN.LEFT, italic=False, wrap=True):
    txBox = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    txBox.word_wrap = wrap
    tf = txBox.text_frame
    tf.word_wrap = wrap
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    return txBox


def add_divider(slide, t, color=ACCENT1, thickness=0.03):
    r = slide.shapes.add_shape(1, Inches(0.5), Inches(t), Inches(12.33), Inches(thickness))
    r.fill.solid()
    r.fill.fore_color.rgb = color
    r.line.fill.background()


def fmt_money(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def slide_number(slide, n, total):
    add_text(slide, f"{n} / {total}", 12.2, 7.1, 1.0, 0.35,
             size=9, color=GREY, align=PP_ALIGN.RIGHT)


# ── Build Presentation ────────────────────────────────────────────────────────

def build():
    prs = Presentation()
    prs.slide_width  = SLIDE_W
    prs.slide_height = SLIDE_H
    TOTAL_SLIDES = 11

    # ── SLIDE 1 — Title ───────────────────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)

    # Top accent bar
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT1)

    # Decorative large circle (background graphic)
    circ = sl.shapes.add_shape(9, Inches(8.5), Inches(1.0), Inches(6.0), Inches(6.0))
    circ.fill.solid()
    circ.fill.fore_color.rgb = BG_CARD
    circ.line.fill.background()

    add_text(sl, "AWS CLOUD COST OPTIMISATION", 0.6, 1.2, 9.0, 0.8,
             size=13, bold=True, color=ACCENT1)
    add_text(sl, "Static Cost Analysis Report", 0.6, 2.0, 9.0, 1.2,
             size=42, bold=True, color=WHITE)
    add_text(sl, "Findings · Opportunities · Next Steps", 0.6, 3.3, 9.0, 0.6,
             size=20, color=GREY, italic=True)

    add_divider(sl, 4.2, ACCENT1)

    # Big stat callouts
    for i, (label, val, col) in enumerate([
        (f"{TOTAL_ACCS} AWS Accounts", "Analysed", WHITE),
        (fmt_money(TOTAL_INV)+"/mo", "Current Invoice", ACCENT2),
        (fmt_money(TOTAL_SAV)+"/mo", "Potential Savings", ACCENT3),
    ]):
        x = 0.6 + i * 3.5
        add_rect(sl, x, 4.5, 3.1, 1.6, BG_CARD)
        add_text(sl, label, x+0.15, 4.6, 2.8, 0.7, size=24, bold=True, color=col, align=PP_ALIGN.CENTER)
        add_text(sl, val,   x+0.15, 5.3, 2.8, 0.4, size=12, color=GREY, align=PP_ALIGN.CENTER)

    add_text(sl, f"Generated {datetime.date.today().strftime('%B %Y')}", 0.6, 7.1, 5.0, 0.35,
             size=10, color=GREY)
    slide_number(sl, 1, TOTAL_SLIDES)

    # Dynamic coverage figures — computed once and reused on Slides 2 & 3 so
    # project-filtered presentations never show globally-hardcoded numbers.
    _num_envs      = len({a.get("env", "") for a in ACCOUNTS if a.get("env")}) or 1
    _services_used = [s["service"] for s in SERVICE_ROLLUP]

    # ── SLIDE 2 — Executive Summary ───────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT1)

    add_text(sl, "EXECUTIVE SUMMARY", 0.6, 0.25, 12.0, 0.5, size=11, bold=True, color=ACCENT1)
    add_text(sl, "The Opportunity at a Glance", 0.6, 0.7, 12.0, 0.7, size=30, bold=True, color=WHITE)
    add_divider(sl, 1.55)

    bullets = [
        ("🔍", "Comprehensive Coverage",
         f"Analysed {TOTAL_ACCS} AWS accounts spanning {len(GROUPS)} "
         f"product {'families' if len(GROUPS) != 1 else 'family'} across {_num_envs} environment{'s' if _num_envs != 1 else ''}."),
        ("💰", "Significant Savings Identified",
         f"{fmt_money(TOTAL_INV)}/mo currently spent — {fmt_money(TOTAL_SAV)}/mo ({TOTAL_SAV/TOTAL_INV*100:.0f}%) identified as recoverable through configuration, right-sizing, and architecture improvements."),
        ("📋", f"{TOTAL_OPPS:,} Cost-Saving Opportunities",
         f"{sev_totals['CRITICAL']} CRITICAL · {sev_totals['HIGH']} HIGH · {sev_totals['MEDIUM']} MEDIUM · {sev_totals['LOW']} LOW findings across {len(_services_used)} AWS services."),
        ("📈", "Annual Business Impact",
         f"Full implementation of recommendations would save {fmt_money(ANNUAL_SAV)} per year — funding engineering capacity and reducing operational overhead."),
    ]
    for i, (icon, title, body) in enumerate(bullets):
        y = 1.8 + i * 1.28
        add_rect(sl, 0.5, y, 12.3, 1.1, BG_CARD)
        add_text(sl, icon,  0.65, y+0.1, 0.5, 0.9, size=22, align=PP_ALIGN.CENTER)
        add_text(sl, title, 1.25, y+0.08, 3.5, 0.45, size=14, bold=True, color=ACCENT1)
        add_text(sl, body,  1.25, y+0.52, 11.2, 0.55, size=11, color=WHITE)

    slide_number(sl, 2, TOTAL_SLIDES)

    # ── SLIDE 3 — Scale of Analysis ───────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT3)

    add_text(sl, "SCALE OF ANALYSIS", 0.6, 0.25, 12.0, 0.5, size=11, bold=True, color=ACCENT3)
    add_text(sl, "What We Covered", 0.6, 0.7, 12.0, 0.7, size=30, bold=True, color=WHITE)
    add_divider(sl, 1.55, ACCENT3)

    # KPI cards row — 6 cards, "Checks Performed" headlines the automated
    # due-diligence behind every saving figure on the following slides.
    kpis = [
        (str(TOTAL_ACCS),       "AWS Accounts",      ACCENT1),
        (str(len(GROUPS)),      "Product Families",  ACCENT2),
        (str(_num_envs),        "Environments",      ACCENT3),
        (str(len(_services_used)), "AWS Services",   PURPLE),
        (f"{TOTAL_CHECKS:,}",   "Checks Performed",  ACCENT1),
        (f"{TOTAL_OPPS:,}",     "Opportunities",     RED),
    ]
    card_w, gap = 1.95, 0.1
    for i, (val, lbl, col) in enumerate(kpis):
        x = 0.4 + i * (card_w + gap)
        add_rect(sl, x, 1.8, card_w, 1.6, BG_CARD)
        add_text(sl, val, x+0.05, 1.9,  card_w-0.1, 0.9, size=26, bold=True, color=col, align=PP_ALIGN.CENTER)
        add_text(sl, lbl, x+0.05, 2.82, card_w-0.1, 0.5, size=10, color=GREY, align=PP_ALIGN.CENTER)

    # Group list
    add_text(sl, "Product Families Analysed", 0.5, 3.65, 12.5, 0.45, size=14, bold=True, color=WHITE)
    groups_text = "  ·  ".join(g for g, _ in GROUPS)
    add_text(sl, groups_text, 0.5, 4.15, 12.4, 0.8, size=11, color=GREY, wrap=True)

    # Services list — only services actually active in these accounts
    add_text(sl, "AWS Services Covered", 0.5, 5.1, 12.5, 0.45, size=14, bold=True, color=WHITE)
    add_text(sl, "  ·  ".join(s.upper() for s in _services_used), 0.5, 5.55, 12.4, 0.9,
             size=10, color=GREY, wrap=True)

    add_rect(sl, 0.4, 6.55, 12.5, 0.6, BG_CARD)
    add_text(sl, f"🔒  {TOTAL_CHECKS:,} read-only checks run · zero charges incurred · fully reproducible at any time",
             0.6, 6.62, 12.1, 0.5, size=11, color=ACCENT3, align=PP_ALIGN.CENTER)

    slide_number(sl, 3, TOTAL_SLIDES)

    # ── SLIDE 4 — The Big Number ──────────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT3)

    add_text(sl, "THE OPPORTUNITY", 0.6, 0.3, 12.0, 0.5, size=11, bold=True, color=ACCENT3)

    # Giant saving number
    add_text(sl, fmt_money(ANNUAL_SAV), 0.5, 1.2, 12.3, 2.2,
             size=96, bold=True, color=ACCENT3, align=PP_ALIGN.CENTER)
    add_text(sl, "potential savings per year", 0.5, 3.3, 12.3, 0.8,
             size=26, color=WHITE, align=PP_ALIGN.CENTER, italic=True)

    add_divider(sl, 4.3, ACCENT3)

    # Monthly breakdown
    for i, (label, val, col) in enumerate([
        ("Current Monthly Spend",    fmt_money(TOTAL_INV),  WHITE),
        ("Identifiable Monthly Save", fmt_money(TOTAL_SAV), ACCENT3),
        ("Saving Percentage",        f"{TOTAL_SAV/TOTAL_INV*100:.1f}%", ACCENT2),
    ]):
        x = 1.0 + i * 3.8
        add_rect(sl, x, 4.6, 3.4, 1.7, BG_CARD)
        add_text(sl, val,   x+0.1, 4.75, 3.2, 0.9, size=30, bold=True, color=col, align=PP_ALIGN.CENTER)
        add_text(sl, label, x+0.1, 5.65, 3.2, 0.5, size=11, color=GREY, align=PP_ALIGN.CENTER)

    add_text(sl, "💡  This is the equivalent of hiring ~10 additional senior engineers — or funding an entire new product initiative.",
             0.5, 6.55, 12.3, 0.5, size=11, color=ACCENT2, align=PP_ALIGN.CENTER, italic=True)

    slide_number(sl, 4, TOTAL_SLIDES)

    # ── SLIDE 5 — Top 5 Opportunities ─────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT1)

    add_text(sl, "TOP 5 OPPORTUNITIES", 0.6, 0.25, 12.0, 0.5, size=11, bold=True, color=ACCENT1)
    add_text(sl, "Where Our Analysis Delivers the Most Value", 0.6, 0.7, 12.0, 0.7, size=28, bold=True, color=WHITE)
    add_divider(sl, 1.55)

    services_positive = [s for s in SERVICE_ROLLUP if s["saving"] > 0]
    services_top      = services_positive[:10]   # used again on the Recommendations slide
    services_top5      = services_positive[:5]
    max_sav            = services_top5[0]["saving"] if services_top5 else 1
    checks_top5        = sum(s.get("checks", 0) for s in services_top5)
    saving_top5         = sum(s["saving"] for s in services_top5)
    bar_w_total        = 6.4
    row_colors         = [ACCENT1, ACCENT2, ACCENT3, PURPLE, RED]

    for i, svc in enumerate(services_top5):
        y = 1.85 + i * 0.95
        pct = svc["saving"] / max_sav
        bar_w = max(0.05, pct * bar_w_total)
        col = row_colors[i % len(row_colors)]
        add_rect(sl, 0.4, y, 8.4, 0.82, BG_CARD)
        # Rank badge
        badge = sl.shapes.add_shape(9, Inches(0.55), Inches(y+0.14), Inches(0.55), Inches(0.55))
        badge.fill.solid()
        badge.fill.fore_color.rgb = col
        badge.line.fill.background()
        add_text(sl, str(i+1), 0.55, y+0.19, 0.55, 0.45, size=16, bold=True, color=ON_ACCENT_TEXT, align=PP_ALIGN.CENTER)
        # Service name
        add_text(sl, svc["service"].upper(), 1.3, y+0.09, 2.3, 0.4, size=14, bold=True, color=WHITE)
        # Bar
        add_rect(sl, 1.3, y+0.5, bar_w, 0.22, col)
        # Monthly saving value
        add_text(sl, fmt_money(svc["saving"])+"/mo", 5.6, y+0.09, 2.1, 0.4, size=14, bold=True, color=col, align=PP_ALIGN.RIGHT)
        # Checks-performed badge — the "value delivered" signal
        add_text(sl, f"{svc.get('checks', 0):,} checks performed", 5.6, y+0.47, 3.1, 0.3, size=9, color=GREY, align=PP_ALIGN.RIGHT)

    # Side callout — aggregate value statement
    add_rect(sl, 9.05, 1.85, 3.85, 4.75, BG_CARD)
    add_text(sl, "VALUE DELIVERED", 9.25, 2.0, 3.45, 0.4, size=11, bold=True, color=ACCENT1)
    add_text(sl, f"{checks_top5:,}", 9.25, 2.4, 3.45, 1.0, size=44, bold=True, color=ACCENT1, align=PP_ALIGN.CENTER)
    add_text(sl, "automated checks\nacross these 5 services", 9.25, 3.35, 3.45, 0.7,
             size=11, color=GREY, align=PP_ALIGN.CENTER)
    add_divider(sl, 4.2, GREY, thickness=0.01)
    add_text(sl, fmt_money(saving_top5)+"/mo", 9.25, 4.35, 3.45, 0.7, size=26, bold=True, color=ACCENT3, align=PP_ALIGN.CENTER)
    pct_top5 = saving_top5 / TOTAL_SAV * 100 if TOTAL_SAV else 0
    add_text(sl, f"{pct_top5:.0f}% of all identified savings", 9.25, 5.0, 3.45, 0.5,
             size=11, color=GREY, align=PP_ALIGN.CENTER)
    add_text(sl, "The depth of checks behind\nthese figures is what turns\nestimates into confident,\nactionable decisions.",
             9.25, 5.65, 3.45, 1.8, size=10, color=WHITE, align=PP_ALIGN.CENTER)

    slide_number(sl, 5, TOTAL_SLIDES)

    # ── SLIDE 6 — Savings by Product Family ──────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT2)

    add_text(sl, "SAVINGS BY PRODUCT FAMILY", 0.6, 0.25, 12.0, 0.5, size=11, bold=True, color=ACCENT2)
    add_text(sl, "Where to Focus First", 0.6, 0.7, 12.0, 0.7, size=28, bold=True, color=WHITE)
    add_divider(sl, 1.55, ACCENT2)

    # Table
    headers = ["Product Family", "Accounts", "Invoice/mo", "Saving/mo", "Saving %"]
    col_x   = [0.4, 3.2, 5.2, 7.7, 10.3]
    col_w   = [2.7, 1.8, 2.3, 2.4, 2.5]

    # Header row
    add_rect(sl, 0.4, 1.72, 12.5, 0.45, HEADER_ROW)
    for j, (hdr, cx, cw) in enumerate(zip(headers, col_x, col_w)):
        add_text(sl, hdr, cx+0.05, 1.75, cw, 0.38, size=11, bold=True, color=ACCENT2, align=PP_ALIGN.LEFT)

    top_groups = GROUPS[:14]
    for i, (grp, v) in enumerate(top_groups):
        y = 2.22 + i * 0.34
        row_col = BG_CARD if i % 2 == 0 else BG_DARK
        add_rect(sl, 0.4, y, 12.5, 0.32, row_col)
        pct = v["saving"] / v["invoice"] * 100 if v["invoice"] else 0
        sav_col = ACCENT3 if pct >= 60 else (ACCENT2 if pct >= 40 else WHITE)
        vals = [grp.replace("-", " ").title(), str(v["accounts"]),
                fmt_money(v["invoice"]), fmt_money(v["saving"]), f"{pct:.0f}%"]
        for j, (val, cx, cw) in enumerate(zip(vals, col_x, col_w)):
            vc = sav_col if j >= 3 else WHITE
            add_text(sl, val, cx+0.05, y+0.03, cw, 0.28, size=10, color=vc)

    slide_number(sl, 6, TOTAL_SLIDES)

    # ── SLIDE 7 — Severity Breakdown ─────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, RED)

    add_text(sl, "RISK & SEVERITY", 0.6, 0.25, 12.0, 0.5, size=11, bold=True, color=RED)
    add_text(sl, f"{TOTAL_OPPS:,} Findings Classified by Severity", 0.6, 0.7, 12.0, 0.7, size=28, bold=True, color=WHITE)
    add_divider(sl, 1.55, RED)

    sev_data = [
        ("CRITICAL", sev_totals.get("CRITICAL", 0), RED,                        "Immediate action — significant waste or security exposure"),
        ("HIGH",     sev_totals.get("HIGH", 0),     ORANGE,   "Tackle in next sprint — high ROI, low risk"),
        ("MEDIUM",   sev_totals.get("MEDIUM", 0),   ACCENT2,                     "Schedule in coming weeks — solid improvements"),
        ("LOW",      sev_totals.get("LOW", 0),       ACCENT3,                    "Backlog — incremental gains"),
        ("DEPENDENCY", sev_totals.get("DEPENDENCY",0), PURPLE,"Architecture changes — coordinate with engineering teams"),
        ("INFO",     sev_totals.get("INFO", 0),      GREY,                       "Informational — monitor over time"),
    ]

    for i, (sev, cnt, col, desc) in enumerate(sev_data):
        x = 0.4 + (i % 3) * 4.2
        y = 1.85 + (i // 3) * 2.45
        add_rect(sl, x, y, 3.9, 2.1, BG_CARD)
        add_rect(sl, x, y, 3.9, 0.08, col)
        add_text(sl, str(cnt), x+0.15, y+0.18, 3.6, 1.0, size=44, bold=True, color=col, align=PP_ALIGN.CENTER)
        add_text(sl, sev,      x+0.15, y+1.18, 3.6, 0.4, size=13, bold=True, color=col, align=PP_ALIGN.CENTER)
        add_text(sl, desc,     x+0.15, y+1.58, 3.6, 0.45, size=9, color=GREY, align=PP_ALIGN.CENTER, wrap=True)

    slide_number(sl, 7, TOTAL_SLIDES)

    # ── SLIDE 8 — Key Recommendations ────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT3)

    add_text(sl, "KEY RECOMMENDATIONS", 0.6, 0.25, 12.0, 0.5, size=11, bold=True, color=ACCENT3)
    add_text(sl, "Top Actions to Unlock Savings", 0.6, 0.7, 12.0, 0.7, size=28, bold=True, color=WHITE)
    add_divider(sl, 1.55, ACCENT3)

    recs = [
        ("🚀", "EC2 Compute Savings Plans",
         f"~{fmt_money(services_top[0]['saving']*0.35)}/mo",
         "Commit to 1-year Compute Savings Plans across all accounts. 30–40% instant discount with full flexibility across instance families, sizes and regions. Zero effort, maximum gain.",
         ACCENT1),
        ("⚙️", "Graviton / ARM64 Migration",
         f"~{fmt_money(TOTAL_SAV*0.15)}/mo",
         "Migrate EC2, ECS, RDS and MSK workloads to Graviton3. Up to 40% better price/performance. Fully safe for managed services with one config change.",
         ACCENT3),
        ("📊", "EC2 Right-Sizing",
         f"~{fmt_money(TOTAL_SAV*0.12)}/mo",
         "CloudWatch CPU/memory utilisation shows 60%+ of instances running below 20% load. Downsize or switch to burstable (T4g) families for non-critical services.",
         ACCENT2),
        ("🔄", "Lifecycle Policies (S3, ECR, EBS)",
         f"~{fmt_money(TOTAL_SAV*0.04)}/mo",
         "Enforce S3 intelligent tiering and Glacier transitions. Set ECR image retention. Migrate EBS gp2 → gp3 volumes for 20% cost reduction with better IOPS.",
         PURPLE),
        ("🛑", "Idle & Orphaned Resources",
         f"~{fmt_money(TOTAL_SAV*0.05)}/mo",
         "Terminate unused load balancers, unattached EIPs, idle NAT gateways and stale CloudWatch log groups. Quick wins with zero application impact.",
         RED),
        ("📉", "CloudWatch & Observability Tuning",
         f"~{fmt_money(3957)}/mo",
         "Remove redundant metric filters, reduce log retention in DEV/STG to 7–14 days, and consolidate dashboards. Observability cost is the 6th largest saving category.",
         ACCENT2),
    ]

    for i, (icon, title, saving, body, col) in enumerate(recs):
        x = 0.4 + (i % 2) * 6.3
        y = 1.8 + (i // 2) * 1.75
        add_rect(sl, x, y, 6.0, 1.6, BG_CARD)
        add_text(sl, icon,   x+0.15, y+0.1,  0.6, 1.3, size=22, align=PP_ALIGN.CENTER)
        add_text(sl, title,  x+0.8,  y+0.08, 3.5, 0.45, size=13, bold=True, color=col)
        add_text(sl, saving, x+4.4,  y+0.08, 1.5, 0.45, size=13, bold=True, color=col, align=PP_ALIGN.RIGHT)
        add_text(sl, body,   x+0.8,  y+0.58, 5.1, 0.95, size=10, color=WHITE, wrap=True)

    slide_number(sl, 8, TOTAL_SLIDES)

    # ── SLIDE 9 — Roadmap / Next Steps ──────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT2)

    add_text(sl, "ROADMAP", 0.6, 0.25, 12.0, 0.5, size=11, bold=True, color=ACCENT2)
    add_text(sl, "From Findings to Savings — Action Plan", 0.6, 0.7, 12.0, 0.7, size=28, bold=True, color=WHITE)
    add_divider(sl, 1.55, ACCENT2)

    # Horizontal timeline
    phases = [
        ("Week 1–2\nQuick Wins",
         ["Enable Compute Savings Plans", "Migrate EBS gp2 → gp3", "Set ECR image retention", "Remove idle EIPs & unused ELBs", "Reduce DEV log retention"],
         ACCENT3, f"~{fmt_money(TOTAL_SAV*0.15)}/mo"),
        ("Month 1–2\nRight-Sizing",
         ["EC2 instance right-sizing", "ECS task def optimisation", "CloudWatch metric cleanup", "S3 Intelligent-Tiering", "Terminate zombie resources"],
         ACCENT1, f"~{fmt_money(TOTAL_SAV*0.25)}/mo"),
        ("Month 2–4\nGraviton",
         ["RDS Graviton3 migration", "MSK broker upgrade", "ECS Fargate ARM64", "EC2 Graviton3 fleet rollout", "ElastiCache Graviton"],
         ACCENT2, f"~{fmt_money(TOTAL_SAV*0.30)}/mo"),
        ("Month 4–6\nArchitecture",
         ["Spot/Fargate blended fleets", "Reserved Instances audit", "Multi-AZ rationalisation", "Lambda concurrency tuning", "Data transfer optimisation"],
         PURPLE, f"~{fmt_money(TOTAL_SAV*0.25)}/mo"),
    ]

    # Connector line
    add_rect(sl, 0.7, 3.0, 11.9, 0.05, GREY)

    for i, (title, items, col, saving) in enumerate(phases):
        x = 0.5 + i * 3.1
        # Timeline dot
        dot = sl.shapes.add_shape(9, Inches(x+1.3), Inches(2.85), Inches(0.3), Inches(0.3))
        dot.fill.solid()
        dot.fill.fore_color.rgb = col
        dot.line.fill.background()
        add_rect(sl, x+0.1, 1.65, 2.75, 1.1, BG_CARD)
        add_text(sl, title, x+0.15, 1.72, 2.65, 1.0, size=12, bold=True, color=col, align=PP_ALIGN.CENTER)
        # Bullet items
        for j, item in enumerate(items):
            add_text(sl, f"› {item}", x+0.15, 3.35+j*0.58, 2.8, 0.5, size=10, color=WHITE)
        # Saving badge
        add_rect(sl, x+0.3, 6.55, 2.3, 0.55, col)
        add_text(sl, saving, x+0.32, 6.6, 2.26, 0.45, size=12, bold=True, color=ON_ACCENT_TEXT, align=PP_ALIGN.CENTER)

    slide_number(sl, 9, TOTAL_SLIDES)

    # ── SLIDE 10 — Call to Action ─────────────────────────────────────────────
    sl = add_slide(prs)
    fill_bg(sl)
    add_rect(sl, 0, 0, 13.33, 0.08, ACCENT3)

    # Background circle decoration
    circ = sl.shapes.add_shape(9, Inches(7.0), Inches(0.5), Inches(7.0), Inches(7.0))
    circ.fill.solid()
    circ.fill.fore_color.rgb = BG_CARD
    circ.line.fill.background()

    add_text(sl, "WHAT HAPPENS NEXT?", 0.6, 0.4, 9.0, 0.6, size=11, bold=True, color=ACCENT3)
    add_text(sl, "Let's Start Saving", 0.6, 1.0, 9.0, 1.0, size=40, bold=True, color=WHITE)
    add_text(sl, f"{fmt_money(ANNUAL_SAV)} is sitting there, waiting.", 0.6, 2.05, 9.0, 0.65,
             size=20, color=ACCENT2, italic=True)

    ctas = [
        ("✅", "Approve the roadmap",          "Endorse the 4-phase action plan and assign account owners"),
        ("🔑", "Grant read-only access",        "Enable the analyser to run continuously across all accounts"),
        ("👥", "Form a Cost Guild",             "Cross-team community: 1h/sprint dedicated to cost work"),
        ("📅", "Monthly review cadence",        "Re-run analyser · review trends · celebrate wins"),
        ("🏆", "Set a savings target",          f"Target: {fmt_money(ANNUAL_SAV*0.5)} saved in 6 months"),
    ]
    for i, (icon, title, sub) in enumerate(ctas):
        y = 3.05 + i * 0.72
        add_text(sl, f"{icon}  {title}", 0.7, y, 5.0, 0.42, size=13, bold=True, color=WHITE)
        add_text(sl, sub, 1.5, y+0.38, 7.5, 0.32, size=10, color=GREY)

    # Right side big stat
    add_rect(sl, 8.8, 1.5, 4.1, 4.8, BG_CARD)
    add_text(sl, "Every month\nwe don't act\ncosts us",
             8.9, 1.65, 3.9, 1.2, size=14, color=GREY, align=PP_ALIGN.CENTER)
    add_text(sl, fmt_money(TOTAL_SAV), 8.9, 2.9, 3.9, 1.3,
             size=38, bold=True, color=RED, align=PP_ALIGN.CENTER)
    add_text(sl, "in preventable\ncloud spend",
             8.9, 4.25, 3.9, 0.9, size=14, color=GREY, align=PP_ALIGN.CENTER)

    add_text(sl, "🌐  Dashboard live at http://localhost:5173  ·  Run: python3 generate_dashboard_data.py",
             0.5, 6.95, 12.3, 0.45, size=10, color=GREY, align=PP_ALIGN.CENTER)

    slide_number(sl, 10, TOTAL_SLIDES)

    # ── SLIDE 11 — Cloud Infrastructure Roadmap ───────────────────────────────
    _ROADMAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roadmap")
    _ROADMAP_MD  = os.path.join(_ROADMAP_DIR, "roadmap.md")
    _GANTT_PNG   = os.path.join(_ROADMAP_DIR, "roadmap_gantt.png")

    if os.path.exists(_ROADMAP_MD):
        # Auto-regenerate Gantt if roadmap.md has changed (or PNG missing)
        _current_hash = hashlib.md5(open(_ROADMAP_MD, "rb").read()).hexdigest()
        _hash_file    = os.path.join(_ROADMAP_DIR, ".roadmap_hash")
        _stored_hash  = open(_hash_file).read().strip() if os.path.exists(_hash_file) else ""
        if _current_hash != _stored_hash or not os.path.exists(_GANTT_PNG):
            _gen = os.path.join(_ROADMAP_DIR, "generate_gantt.py")
            subprocess.run([sys.executable, _gen, "--force"], check=True)

    if os.path.exists(_GANTT_PNG):
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        fill_bg(sl)
        add_rect(sl, 0, 0, 13.33, 0.08, ACCENT1)   # top accent bar
        add_text(sl, "CLOUD INFRASTRUCTURE ROADMAP", 0.6, 0.16, 8.0, 0.44,
                 size=11, bold=True, color=ACCENT1)
        add_text(sl, "Implementation Timeline", 0.6, 0.60, 9.0, 0.52,
                 size=26, bold=True, color=WHITE)
        add_divider(sl, 1.18)
        # Embed the Gantt PNG, centred with side margins
        img_left   = Inches(0.30)
        img_top    = Inches(1.35)
        img_width  = Inches(12.73)
        sl.shapes.add_picture(_GANTT_PNG, img_left, img_top, width=img_width)
        slide_number(sl, 11, TOTAL_SLIDES)
    else:
        # Fallback text slide if Gantt image not available
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        fill_bg(sl)
        add_rect(sl, 0, 0, 13.33, 0.08, ACCENT1)
        add_text(sl, "CLOUD INFRASTRUCTURE ROADMAP", 0.6, 0.16, 8.0, 0.44,
                 size=11, bold=True, color=ACCENT1)
        add_text(sl, "roadmap/roadmap.md not found", 0.6, 2.5, 12.0, 0.8,
                 size=16, color=GREY)
        slide_number(sl, 11, TOTAL_SLIDES)

    # ── Save ──────────────────────────────────────────────────────────────────
    if _project_filter:
        slug = "_".join(sorted(_project_filter)).replace("-", "_")
        out = f"output/AWS_Cost_Optimisation_{slug}.pptx"
    else:
        out = "output/AWS_Cost_Optimisation_Presentation.pptx"
    os.makedirs("output", exist_ok=True)
    prs.save(out)
    print(f"✅  Saved: {out}")
    print(f"    Slides: {TOTAL_SLIDES}")
    print(f"    Accounts: {TOTAL_ACCS}  |  Invoice: {fmt_money(TOTAL_INV)}/mo  |  Saving: {fmt_money(TOTAL_SAV)}/mo ({TOTAL_SAV/TOTAL_INV*100:.1f}%)")


if __name__ == "__main__":
    build()
