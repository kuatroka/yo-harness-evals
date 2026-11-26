"""Guard the Harbor install against silent customization loss.

The viewer customization belongs in ``viewer_overlay/``, never in
``site-packages``. ``uv sync`` reverts edits on upgrade and leaves added files
behind as orphans, so an edit there disappears without a trace while the tests
still pass.

Two static assets are the one exception: the Total column is a string patch on
the built SvelteKit bundle, applied by ``scripts/patch_viewer_columns.sh``. An
upgrade replaces those files with pristine ones and the marker disappears, so
this test fails here, naming the fix, instead of the column quietly vanishing
from the UI.

Run directly: ``uv run --with pytest python -m pytest tests/test_harbor_upgrade_guard.py -q``
"""

from __future__ import annotations

import base64
import csv
import fnmatch
import hashlib
from pathlib import Path

import harbor

# The patched assets and the marker each one must contain. Asserting the marker
# rather than a file hash keeps the guard honest across harmless re-patches: the
# build names change with every Harbor release, and the bytes change with every
# edit to the patch script. What must survive is the column definition.
ASSET_DIR = "harbor/viewer/static/assets"
STATIC_TOTAL_COLUMN = {
    "home-*.js": 'accessorKey:"total_all_tokens"',
    "rollouts-table-*.js": 'accessorKey:"total_tokens"',
}
# A key in these arrays is hidden by default, so defining the column is not
# enough to make it render. An earlier version of the patch script appended the
# new key here and the column never appeared.
HIDDEN_BY_DEFAULT = {
    "home-*.js": '"total_cached_input_tokens","total_all_tokens"]',
    "rollouts-table-*.js": '"cached_input_tokens","total_tokens"]',
}


def _harbor_root() -> Path:
    return Path(harbor.__file__).resolve().parent


def _site_packages() -> Path:
    return _harbor_root().parent


def _record_hashes() -> dict[str, str]:
    dist_info = next(_site_packages().glob("harbor-*.dist-info"))
    record = dist_info / "RECORD"
    hashes: dict[str, str] = {}
    with record.open(newline="") as handle:
        for path, digest, _size in csv.reader(handle):
            if digest.startswith("sha256="):
                hashes[path] = digest.split("=", 1)[1]
    return hashes


def _hash(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _shipped_files() -> list[Path]:
    root = _harbor_root()
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    ]


def _is_patchable_asset(relative: str) -> bool:
    if not relative.startswith(ASSET_DIR + "/"):
        return False
    name = relative.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in STATIC_TOTAL_COLUMN)


def test_install_is_pristine_except_known_static_assets() -> None:
    """Any edit to a Python file under site-packages must move into the overlay."""
    record = _record_hashes()
    site_packages = _site_packages()

    modified = []
    added = []
    for path in _shipped_files():
        relative = str(path.relative_to(site_packages))
        if _is_patchable_asset(relative):
            continue
        if relative not in record:
            added.append(relative)
        elif _hash(path) != record[relative]:
            modified.append(relative)

    assert not modified, (
        "These harbor files differ from the wheel RECORD: "
        f"{modified}. The viewer customization must live in viewer_overlay/, not "
        "site-packages. Reinstall with `uv sync --reinstall-package harbor` and "
        "move the behaviour into the overlay."
    )
    assert not added, (
        "These files are not shipped by the harbor wheel: "
        f"{added}. Delete them and put the logic in viewer_overlay/, or the next "
        "upgrade will leave them behind as orphans."
    )


def test_static_total_column_patch_is_present() -> None:
    """The Total column lives in two built assets. An upgrade replaces them."""
    assets = _site_packages() / ASSET_DIR
    missing: list[str] = []

    for pattern, marker in STATIC_TOTAL_COLUMN.items():
        matches = sorted(assets.glob(pattern))
        if not matches:
            missing.append(f"{pattern} (no file matches)")
            continue
        hidden = HIDDEN_BY_DEFAULT[pattern]
        for path in matches:
            text = path.read_text(encoding="utf-8", errors="replace")
            if marker not in text:
                missing.append(f"{path.name} (no {marker})")
            elif hidden in text:
                missing.append(f"{path.name} (Total is in the default-hide list)")

    assert not missing, (
        f"The Total column patch is missing for harbor {harbor.__version__}: "
        f"{missing}. Re-apply it with scripts/patch_viewer_columns.sh, then "
        "restart the viewer. If the script reports the files patched but this "
        "still fails, the bundle changed shape, so check the column is not left "
        "in the table's default-hide array before re-pinning this guard."
    )
