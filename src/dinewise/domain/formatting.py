"""Display formatting shared by template explanations and, later, the UI."""

from __future__ import annotations


def format_indian(number: int) -> str:
    """Group digits the Indian way: 1200 -> "1,200", 120000 -> "1,20,000" (edge case D-09)."""
    sign = "-" if number < 0 else ""
    digits = str(abs(int(number)))
    if len(digits) <= 3:
        return sign + digits
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups) + "," + tail


def format_inr(amount: int) -> str:
    return "₹" + format_indian(amount)


def join_words(items: list[str], conjunction: str = "and") -> str:
    """["a"] -> "a"; ["a", "b"] -> "a and b"; ["a", "b", "c"] -> "a, b and c"."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"
