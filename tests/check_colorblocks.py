"""Standard color-block anomaly inspector for MomentShift (v0.2.4+).

Background
----------
The "色块异常" (color-block anomaly) bug = some text shows an unexpected solid
background block that bleeds through a UI component and reveals the *window/view*
background colour. The classic root cause (see memory/2026-07-29.md, Iteration 9
#6) is a **bare** `background-color` set on a container widget with an *empty* or
un-scoped selector — Qt cascades that fill to every descendant, so a card's child
labels pick up the view's background instead of the card surface.

This script is the *standard inspection process* requested for every UI change:
run it after editing any GUI file to confirm no unscoped opaque background leaks.

What it checks
--------------
For every ``setStyleSheet("...")`` call in the GUI/core source it parses the CSS
into ``selector { declarations }`` rules and flags:

* CRITICAL — a rule with an **empty** or ``*`` selector that sets an **opaque**
  background (a hex colour, not ``transparent``/``none``). This is the #82 class of
  bug and MUST be fixed (scope it with ``#objectName { ... }`` or a class selector).
* REVIEW  — a rule with an **empty** selector that sets a ``transparent``/``none``
  background (usually harmless, but worth a glance) or a *scoped* rule that paints
  an opaque background (intentional for cards/pills, but listed for awareness).

It deliberately does NOT fail the build (exit code 0) — it is an advisory linter
meant to be read by a human during review. A CRITICAL finding should block merge.

Usage
-----
    python tests/check_colorblocks.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUI_DIR = REPO / "src" / "momentshift" / "gui"
CORE_DIR = REPO / "src" / "momentshift" / "core"

# Match setStyleSheet("...") or setStyleSheet(f"...") — single-line literals.
# Captures the raw string body (we don't resolve f-string expressions).
_STYLE_RE = re.compile(
    r"""setStyleSheet\(\s*(?:f)?(r?['"])(.*?)\1""", re.DOTALL
)

# A background declaration (color or shorthand) inside a rule.
_BG_RE = re.compile(r"background(?:-color)?\s*:\s*([^;}]+)")


def _is_opaque(value: str) -> bool:
    v = value.strip().lower()
    if not v:
        return False
    if v in ("transparent", "none", "inherit", "auto"):
        return False
    # rgba(...) / hsla(...) with alpha 0 is transparent-ish; treat alpha<1 as safe.
    m = re.match(r"rgba?\(([^)]*)\)", v)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 4:
            try:
                alpha = float(parts[3])
                return alpha >= 1.0
            except ValueError:
                return True
        return True
    # Anything else (hex, named colour) is opaque.
    return True


def _analyze(css: str, file: Path, lineno: int, report: list) -> None:
    # Split into rules on '}'.
    for raw in css.split("}"):
        if "{" not in raw:
            continue
        selector, decls = raw.split("{", 1)
        selector = selector.strip()
        decls = decls.strip()
        for bm in _BG_RE.finditer(decls):
            value = bm.group(1).strip()
            opaque = _is_opaque(value)
            if not selector or selector == "*":
                if opaque:
                    report.append((file, lineno, "CRITICAL",
                                   f"unscoped opaque background '{value}' "
                                   f"(selector={selector or '<empty>'})"))
                else:
                    report.append((file, lineno, "REVIEW",
                                   f"unscoped transparent background "
                                   f"(selector={selector or '<empty>'})"))
            else:
                if opaque:
                    report.append((file, lineno, "REVIEW",
                                   f"scoped opaque background '{value}' "
                                   f"(selector='{selector}')"))


def main() -> int:
    report: list = []
    scanned = 0
    for d in (GUI_DIR, CORE_DIR):
        for path in sorted(d.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            scanned += 1
            for m in _STYLE_RE.finditer(text):
                body = m.group(2)
                # best-effort line number
                lineno = text[: m.start()].count("\n") + 1
                _analyze(body, path.relative_to(REPO), lineno, report)

    critical = [r for r in report if r[2] == "CRITICAL"]
    review = [r for r in report if r[2] == "REVIEW"]

    print("=" * 70)
    print("  MomentShift — color-block anomaly inspection")
    print("=" * 70)
    print(f"  Files scanned : {scanned}")
    print(f"  CRITICAL      : {len(critical)}")
    print(f"  REVIEW        : {len(review)}")
    print("-" * 70)
    for f, ln, sev, msg in report:
        print(f"  [{sev:8}] {f}:{ln}  {msg}")
    print("-" * 70)
    if critical:
        print("  RESULT: FAIL — unscoped opaque background detected (#82 class).")
        print("          Scope it with '#objectName { ... }' or a class selector.")
    else:
        print("  RESULT: PASS — no unscoped opaque background (no #82-class bug).")
        if review:
            print("          (REVIEW items are transparent/unscoped or intentionally")
            print("           scoped; verify they are harmless.)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
