"""Rename the viewer's token columns and add the computed Total column.

Harbor ships a compiled SvelteKit bundle. The labels are literal strings in the
bundle and the column definitions live in one assets file each. This patches the
overlay's copy in ``.viewer_static/``, never site-packages, so a Harbor upgrade
only has to rebuild that copy. Bump ``VERSION`` after changing a patch so an
existing build is not reused with stale bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

VERSION = "1"

TOTAL_MARKERS = {
    "home-*.js": 'accessorKey:"total_all_tokens"',
    "rollouts-table-*.js": 'accessorKey:"total_tokens"',
}
HIDDEN_BY_DEFAULT = {
    "home-*.js": '"total_cached_input_tokens","total_all_tokens"]',
    "rollouts-table-*.js": '"cached_input_tokens","total_tokens"]',
}


class ColumnPatchError(RuntimeError):
    """The viewer bundle no longer has the shape the column patch expects."""


def apply(assets: Path, harbor_version_text: str = "unknown") -> list[str]:
    """Patch every bundle asset, returning the names written."""
    written: list[str] = []
    for pattern, patch in _PATCHES.items():
        for path in sorted(assets.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            patched = patch(_rename_labels(text))
            if patched != text:
                path.write_text(patched, encoding="utf-8")
                written.append(path.name)

    problems = verify(assets)
    if problems:
        raise ColumnPatchError(
            f"cannot patch the viewer columns for harbor {harbor_version_text}: "
            f"{problems}. The bundle changed shape, so update viewer_overlay/columns.py."
        )
    return written


def verify(assets: Path) -> list[str]:
    """Every problem that would keep the Total column out of the built copy."""
    problems: list[str] = []
    for pattern, marker in TOTAL_MARKERS.items():
        matches = sorted(assets.glob(pattern))
        if not matches:
            problems.append(f"{pattern} (no file matches)")
            continue
        hidden = HIDDEN_BY_DEFAULT[pattern]
        for path in matches:
            text = path.read_text(encoding="utf-8", errors="replace")
            if marker not in text:
                problems.append(f"{path.name} (no {marker})")
            elif hidden in text:
                problems.append(f"{path.name} (Total is in the default-hide list)")
    return problems


def _rename_labels(text: str) -> str:
    return (
        text.replace("Uncached Input Tokens", "Input")
        .replace("Cached Input Tokens", "Cashed")
        .replace("Output Tokens", "Output")
    )


def _patch_rollouts(text: str) -> str:
    if 'accessorKey:"total_tokens"' in text:
        return text
    anchor = '{accessorKey:"cost_usd",header:'
    if anchor not in text:
        return text
    column = (
        '{accessorKey:"total_tokens",header:({column:e})=>t.jsx("div",'
        '{className:"text-right",children:t.jsx(h,{column:e,children:"Total"})}),'
        "cell:({row:e})=>{const a=e.original.total_tokens;"
        "return a===null||a===void 0?"
        't.jsx("div",{className:"text-right text-muted-foreground",children:"-"})'
        ':t.jsx("div",{className:"text-right tabular-nums",children:Q(a)})}},'
    )
    text = text.replace(anchor, column + anchor, 1)
    # A hidden column never renders, whatever its definition says.
    return text.replace('"cached_input_tokens","total_tokens"]', '"cached_input_tokens"]')


def _patch_home(text: str) -> str:
    if 'accessorKey:"total_all_tokens"' in text:
        return text
    anchor = '{accessorKey:"total_cost_usd",header:'
    if anchor not in text:
        return text
    column = (
        '{accessorKey:"total_all_tokens",header:({column:t})=>e.jsx("div",'
        '{className:"text-right",children:e.jsx(h,{column:t,children:"Total"})}),'
        "cell:({row:t})=>{const n=t.original.total_all_tokens;"
        "return n===null||n===void 0?"
        'e.jsx("div",{className:"text-right text-muted-foreground",children:"-"})'
        ':e.jsx("div",{className:"text-right tabular-nums",children:te(n)})}},'
    )
    text = text.replace(anchor, column + anchor, 1)
    text = text.replace('"total_cached_input_tokens","total_all_tokens"]', '"total_cached_input_tokens"]')
    # The Columns menu is a hardcoded option array, so a new column needs an
    # entry or it can never be toggled back on.
    return text.replace(
        '{value:"total_cost_usd",label:"Cost USD"}]',
        '{value:"total_cost_usd",label:"Cost USD"},'
        '{value:"total_all_tokens",label:"Total"}]',
        1,
    )


_PATCHES: dict[str, Callable[[str], str]] = {
    "home-*.js": _patch_home,
    "rollouts-table-*.js": _patch_rollouts,
}
