#!/usr/bin/env bash
# Rename the viewer's token columns and inject the computed Total column.
# The viewer ships a compiled SPA; labels are literal strings in the bundle and
# the column defs live in one assets file each. Re-run after a Harbor upgrade —
# bundle file names change with each build hash.
set -euo pipefail
ASSETS=".venv/lib/python3.12/site-packages/harbor/viewer/static/assets"
python3 - "$ASSETS" <<'PY'
import pathlib, sys, glob
assets = pathlib.Path(sys.argv[1])

# Per-trial table (rollouts) — field is total_tokens, formatter Q, jsx t.jsx
for f in assets.glob('rollouts-table-*.js'):
    t = f.read_text()
    t = t.replace('"Uncached Input Tokens"','"Input"').replace('"Cached Input Tokens"','"Cashed"').replace('"Output Tokens"','"Output"')
    t = t.replace('Uncached Input Tokens','Input').replace('Cached Input Tokens','Cashed').replace('Output Tokens','Output')
    col='{accessorKey:"total_tokens",header:({column:e})=>t.jsx("div",{className:"text-right",children:t.jsx(h,{column:e,children:"Total"})}),cell:({row:e})=>{const a=e.original.total_tokens;return a===null||a===void 0?t.jsx("div",{className:"text-right text-muted-foreground",children:"-"}):t.jsx("div",{className:"text-right tabular-nums",children:Q(a)})}},'
    a='{accessorKey:"cost_usd",header:'
    if a in t and 'total_tokens' not in t: t=t.replace(a,col+a,1)
    # The column is useless while it sits in the table's default-hide list.
    t=t.replace('"cached_input_tokens","total_tokens"]','"cached_input_tokens"]')
    f.write_text(t); print('patched',f.name)

# Job table (home) — field is total_all_tokens, formatter te, jsx e.jsx
for f in assets.glob('home-*.js'):
    t = f.read_text()
    t = t.replace('"Uncached Input Tokens"','"Input"').replace('"Cached Input Tokens"','"Cashed"').replace('"Output Tokens"','"Output"')
    t = t.replace('Uncached Input Tokens','Input').replace('Cached Input Tokens','Cashed').replace('Output Tokens','Output')
    col='{accessorKey:"total_all_tokens",header:({column:t})=>e.jsx("div",{className:"text-right",children:e.jsx(h,{column:t,children:"Total"})}),cell:({row:t})=>{const n=t.original.total_all_tokens;return n===null||n===void 0?e.jsx("div",{className:"text-right text-muted-foreground",children:"-"}):e.jsx("div",{className:"text-right tabular-nums",children:te(n)})}},'
    a='{accessorKey:"total_cost_usd",header:'
    if a in t and 'total_all_tokens' not in t: t=t.replace(a,col+a,1)
    # Unhide it. The previous version of this script appended the key to this
    # same array instead, so the column never rendered.
    t=t.replace('"total_cached_input_tokens","total_all_tokens"]','"total_cached_input_tokens"]')
    # The Columns menu is a hardcoded option array, so a new column needs an
    # entry or it can never be toggled.
    if 'label:"Total"' not in t:
        t=t.replace('{value:"total_cost_usd",label:"Cost USD"}]','{value:"total_cost_usd",label:"Cost USD"},{value:"total_all_tokens",label:"Total"}]',1)
    f.write_text(t); print('patched',f.name)
PY
echo "Restart './scripts/harbor_view.sh' or 'uv run harbor view jobs' afterwards."
