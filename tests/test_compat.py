"""gitlane supports Python 3.10+, but development often happens on newer Pythons.

Python 3.12 accepts f-strings that reuse their own quote inside {...}
(PEP 701); 3.10 and 3.11 refuse to even load such a file. This test finds
them on any Python version.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted([*ROOT.glob("src/gitlane/*.py"), *ROOT.glob("tests/*.py"), *ROOT.glob("scripts/*.py")])
_PREFIX = re.compile(r"^[rRbBfFuU]*")


def _quote(token_text: str) -> str:
    rest = _PREFIX.sub("", token_text)
    return rest[:3] if rest[:3] in ('"""', "'''") else rest[:1]


def same_quote_nesting(source: str) -> list[int]:
    """Line numbers where a string inside an f-string uses the f-string's own quote."""
    if not hasattr(tokenize, "FSTRING_START"):  # Python < 3.12 can't even parse them: fine
        return []
    lines, open_quotes = [], []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.FSTRING_START:
            q = _quote(tok.string)
            if q in open_quotes:
                lines.append(tok.start[0])
            open_quotes.append(q)
        elif tok.type == tokenize.FSTRING_END:
            open_quotes.pop()
        elif tok.type == tokenize.STRING and open_quotes and _quote(tok.string) in open_quotes:
            lines.append(tok.start[0])
    return lines


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_runs_on_python_310(path):
    if not path.exists():
        pytest.skip("source not available (installed copy)")
    assert same_quote_nesting(path.read_text(encoding="utf-8")) == []


def test_detector_finds_the_problem():
    assert same_quote_nesting('''x = f'{f"{y('a')}"}'\n''') == [1]
    assert same_quote_nesting('''x = f'{y("a")}'\n''') == []
