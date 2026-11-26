"""Guard the Harbor install against silent customization loss.

The viewer customization belongs in ``viewer_overlay/``, never in
``site-packages``. ``uv sync`` reverts edits on upgrade and leaves added files
behind as orphans, so an edit there disappears without a trace while the tests
still pass.

The Total column is a string patch on the built SvelteKit bundle. The overlay
applies it to its own copy in ``.viewer_static/``, so the install stays
pristine. The build test proves that copy still gets the column after a Harbor
upgrade changes the bundle's file names.

Run directly: ``uv run --with pytest python -m pytest tests/test_harbor_upgrade_guard.py -q``
"""

from __future__ import annotations

import base64
import csv
import hashlib
import tempfile
from pathlib import Path

import harbor

from viewer_overlay import columns, static_build


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


def test_install_is_pristine() -> None:
    """Any edit to a file under site-packages must move into the overlay."""
    record = _record_hashes()
    site_packages = _site_packages()

    modified = []
    added = []
    for path in _shipped_files():
        relative = str(path.relative_to(site_packages))
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


def test_the_build_adds_the_total_column_to_its_own_copy() -> None:
    """The column is applied to ``.viewer_static/``, never to site-packages."""
    source = _harbor_root() / "viewer" / "static"
    before = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "built"
        static_build.build(target, source, force=True)
        assert columns.verify(target / "assets") == []

    after = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}
    assert before == after, "the build modified Harbor's installed bundle"
