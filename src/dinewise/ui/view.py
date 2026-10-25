"""Pure display helpers for the Streamlit app: text formatting and card HTML.

Nothing here imports Streamlit, so it can be unit-tested directly. Every value that
comes from the API is escaped before it is rendered (edge case UI-14).
"""

from __future__ import annotations

import html
import re
from typing import Any

from dinewise.domain.formatting import format_indian, format_inr, join_words

MAX_CHIPS = 6  # cuisine chips shown per card before "+N more" (edge case UI-12)

# Characters that Streamlit's Markdown treats specially; "$" starts LaTeX.
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")

CSS = """
<style>
.dw-card {
  border: 1px solid rgba(128, 128, 128, 0.35);
  border-radius: 12px;
  padding: 14px 16px;
  margin: 0 0 12px 0;
  overflow-wrap: anywhere;
}
.dw-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.dw-rank {
  font-weight: 700; font-size: 0.85rem; padding: 1px 8px; border-radius: 999px;
  background: rgba(232, 89, 12, 0.15); color: #e8590c;
}
.dw-name { font-weight: 700; font-size: 1.15rem; flex: 1 1 auto; min-width: 0; }
.dw-source {
  font-size: 0.75rem; padding: 1px 8px; border-radius: 999px;
  border: 1px solid rgba(128, 128, 128, 0.45); opacity: 0.85; white-space: nowrap;
}
.dw-source.dw-ai { border-color: rgba(112, 72, 232, 0.6); color: #7048e8; }
.dw-facts {
  margin: 6px 0 4px 0; font-size: 0.92rem;
  display: flex; flex-wrap: wrap; gap: 4px 14px;
}
.dw-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 8px 0; }
.dw-chip {
  font-size: 0.78rem; padding: 2px 9px; border-radius: 999px;
  background: rgba(128, 128, 128, 0.15);
}
.dw-chip.dw-more { background: transparent; border: 1px dashed rgba(128, 128, 128, 0.5); }
.dw-expl { margin: 4px 0 0 0; line-height: 1.5; }
.dw-caveat { margin: 6px 0 0 0; font-size: 0.85rem; opacity: 0.7; font-style: italic; }
</style>
"""


def md_escape(text: str) -> str:
    """Escape text for st.markdown / st.info so it renders literally."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def rating_text(restaurant: dict[str, Any]) -> str:
    """ "★ 4.5 (3,200 votes)", or "New / not rated" when there is no rating (UI-09)."""
    rating = restaurant.get("rating")
    if rating is None:
        return "New / not rated"
    votes = int(restaurant.get("votes") or 0)
    noun = "vote" if votes == 1 else "votes"
    return f"★ {rating:.1f} ({format_indian(votes)} {noun})"


def cost_text(restaurant: dict[str, Any]) -> str:
    """ "₹1,200 for two", or "Cost not available" (UI-10, UI-11)."""
    cost = restaurant.get("cost_for_two")
    return "Cost not available" if cost is None else f"{format_inr(int(cost))} for two"


def chips(values: list[str], limit: int = MAX_CHIPS) -> tuple[list[str], int]:
    """The chips to show, and how many more were left out."""
    return values[:limit], max(0, len(values) - limit)


def source_label(source: str) -> str:
    return "AI-generated" if source == "llm" else "Basic ranking"


def card_html(rec: dict[str, Any]) -> str:
    """One recommendation as an HTML card. All API values are escaped."""
    r = rec["restaurant"]
    esc = html.escape
    shown, more = chips(list(r.get("cuisines") or []))
    chip_html = "".join(f'<span class="dw-chip">{esc(c)}</span>' for c in shown)
    if more:
        chip_html += f'<span class="dw-chip dw-more">+{more} more</span>'

    source = rec.get("source", "heuristic")
    source_cls = "dw-source dw-ai" if source == "llm" else "dw-source"
    caveat = rec.get("caveats")
    parts = [
        '<div class="dw-card">',
        '<div class="dw-head">',
        f'<span class="dw-rank">#{int(rec["rank"])}</span>',
        f'<span class="dw-name">{esc(r["name"])}</span>',
        f'<span class="{source_cls}">{esc(source_label(source))}</span>',
        "</div>",
        '<div class="dw-facts">',
        f"<span>{esc(rating_text(r))}</span>",
        f"<span>{esc(cost_text(r))}</span>",
        # Always shown, so branches of one chain can be told apart (UI-13).
        f"<span>📍 {esc(r.get('locality') or '')}</span>",
        "</div>",
        f'<div class="dw-chips">{chip_html}</div>' if chip_html else "",
        f'<p class="dw-expl">{esc(rec.get("explanation") or "")}</p>',
        f'<p class="dw-caveat">⚠ {esc(caveat)}</p>' if caveat else "",
        "</div>",
    ]
    return "".join(parts)


def corrections_text(corrections: list[dict[str, Any]]) -> str | None:
    """'Showing results for **Koramangala** (you typed "koramangla").' (UI-18), escaped."""
    if not corrections:
        return None
    pieces = [
        f'**{md_escape(c["value"])}** (you typed "{md_escape(c["input"])}")' for c in corrections
    ]
    return f"Showing results for {join_words(pieces)}."


def empty_state_tips(preferences: dict[str, Any], *, city_wide: bool = False) -> list[str]:
    """Suggestions for an empty result, based on what was searched (UI-07)."""
    tips: list[str] = []
    if preferences.get("min_rating"):
        tips.append("Lower the minimum rating.")
    if preferences.get("budget"):
        tips.append("Set the budget to Any.")
    if preferences.get("cuisines"):
        tips.append("Remove a cuisine or two.")
    if preferences.get("extras"):
        tips.append("Shorten or clear the extra preferences.")
    if not city_wide:
        tips.append("Pick a different neighbourhood, or search the whole city.")
    return tips
