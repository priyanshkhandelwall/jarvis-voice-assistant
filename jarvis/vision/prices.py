"""F9 price table: a local CSV the developer edits by hand
(jarvis/settings/prices.csv; [camera] price_table). Columns: object, low,
high, currency, date_updated (YYYY-MM-DD), source.

Figures are rough estimates (R19), always spoken as a range: "Roughly $800
to $2,500." An entry older than [camera] price_stale_months, or with no
date, is stale: she still gives the range but says it is out of date. She
always says when the figures are from. An object with no entry has no price.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Optional

from jarvis.settings import SETTINGS

_CFG = SETTINGS["camera"]
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class PriceEntry:
    """A rough price range for one object, its currency, and when it was last checked."""
    obj: str
    low: float
    high: float
    currency: str
    updated: Optional[date]
    source: str

    def is_stale(self, today: date) -> bool:
        """True if the price is older than [camera] price_stale_months."""
        if self.updated is None:
            return True
        months = (today.year - self.updated.year) * 12 + (today.month - self.updated.month)
        return months > int(_CFG["price_stale_months"])


def money(amount: float, currency: str = "AUD") -> str:
    """$800, $2,500, $12.50 -- dollars for AUD/USD, otherwise the code."""
    text = f"{amount:,.0f}" if amount >= 100 or float(amount).is_integer() else f"{amount:,.2f}"
    return f"${text}" if currency.upper() in ("AUD", "USD", "NZD", "CAD") else f"{text} {currency.upper()}"


def load_prices(path: Optional[Path] = None) -> Dict[str, PriceEntry]:
    """Read prices.csv into {object label: PriceEntry}."""
    path = path or _PROJECT_ROOT / str(_CFG["price_table"])
    table: Dict[str, PriceEntry] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("object") or "").strip().lower()
            if not name:
                continue
            try:
                low, high = float(row["low"]), float(row["high"])
            except (KeyError, TypeError, ValueError):
                continue
            raw_date = (row.get("date_updated") or "").strip()
            try:
                updated = date.fromisoformat(raw_date) if raw_date else None
            except ValueError:
                updated = None
            table[name] = PriceEntry(name, min(low, high), max(low, high), (row.get("currency") or "AUD").strip(),
                                     updated, (row.get("source") or "").strip())
    return table


def price_sentence(entry: PriceEntry, today: date) -> str:
    """"Roughly $800 to $2,500." plus when the figures are from (and stale)."""
    replies = _CFG["replies"]
    sentence = str(replies["price"]).format(low=money(entry.low, entry.currency), high=money(entry.high, entry.currency))
    if entry.updated is None:
        return f"{sentence} {replies['price_undated']}"
    if (entry.updated.year, entry.updated.month) == (today.year, today.month):
        return sentence  # Figures from this month need no "from <month>" note
    if entry.updated > today:
        return sentence
    when = f"{entry.updated:%B %Y}"
    key = "price_stale" if entry.is_stale(today) else "price_dated"
    return f"{sentence} {str(replies[key]).format(when=when)}"
