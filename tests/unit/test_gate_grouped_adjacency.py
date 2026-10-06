"""Regression: comma-grouped values adjacent to a currency marker.

Leftmost-first matching must not let the marker+short-value branch consume the
leading digit group of a comma-grouped value ("JPY 1,234" is one monetary
token, not marker + "1"). On-grid grouped values stay byte-identical; off-grid
grouped values snap as their full amount.
"""

import pytest

from src.nodes.post_process_node import _enforce_precision


@pytest.mark.parametrize(
    "text, expected",
    [
        ("JPY 1,000", "JPY 1,000"),
        ("JPY  1,000", "JPY  1,000"),
        ("JPY 1,234", "JPY 1,000"),
        ("USD 1,234,567", "USD 1,235,000"),
        ("¥1,234", "¥1,000"),
        ("JPY +1,234", "JPY +1,000"),
        ("1,234 JPY", "1,000 JPY"),
    ],
)
def test_marker_adjacent_grouped_values(text: str, expected: str) -> None:
    out, _ = _enforce_precision(text)
    assert out == expected
