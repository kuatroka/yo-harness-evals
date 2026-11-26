"""Build the viewer's static directory, with the overlay's changes applied.

Harbor ships a compiled SvelteKit bundle. Rather than patch it in site-packages,
this copies it to `.viewer_static/` beside the repo, adds the Total column and
appends one script tag, so a Harbor upgrade rebuilds from the new bundle instead
of fighting an old patch. The copy is skipped whenever the source bundle and the
column patch are identical to the ones already built.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import columns, harbor_api

INDEX = "index.html"
STATE_FILE = ".overlay-source"
CLOSING_TAG = "</body>"
NAV_TAG = '<script src="/overlay/nav.js" defer></script>'


class StaticBuildError(RuntimeError):
    """The Harbor bundle is not shaped the way the overlay expects."""


@dataclass(frozen=True)
class BuildResult:
    target: Path
    rebuilt: bool


def build(
    target: Path,
    source: Path | None = None,
    *,
    force: bool = False,
) -> BuildResult:
    """Copy the bundle into `target` and apply the overlay's edits once."""
    harbor = harbor_version()
    source = Path(source) if source is not None else default_source()
    fingerprint = source_fingerprint(source) + "\0" + columns.VERSION
    index = target / INDEX
    state = target / STATE_FILE

    if not force and index.is_file() and _stamp(state) == fingerprint:
        return BuildResult(target=target, rebuilt=False)

    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    if not index.is_file():
        raise StaticBuildError(
            f"cannot inject the overlay nav: {source} has no {INDEX} (harbor {harbor})"
        )
    columns.apply(target / "assets", harbor)
    html = index.read_text(encoding="utf-8")
    index.write_text(inject_nav(html, str(index), harbor), encoding="utf-8")
    # The stamp is written last, so a copy that was interrupted rebuilds next time.
    state.write_text(fingerprint + "\n", encoding="utf-8")
    return BuildResult(target=target, rebuilt=True)


def inject_nav(html: str, index_path: str, harbor_version_text: str) -> str:
    """Put the nav script just before the closing body tag, at most once."""
    if NAV_TAG in html:
        return html
    marker = html.lower().rfind(CLOSING_TAG)
    if marker < 0:
        raise StaticBuildError(
            f"cannot inject the overlay nav: no {CLOSING_TAG} in {index_path} "
            f"(harbor {harbor_version_text}). The viewer build changed shape, so the "
            "overlay needs a look before it can start."
        )
    return html[:marker] + NAV_TAG + html[marker:]


def source_fingerprint(source: Path) -> str:
    """One hash over every name and byte in the bundle."""
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(source)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def harbor_version() -> str:
    return harbor_api.installed_version()


def default_source() -> Path:
    return harbor_api.static_dir()


def _stamp(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


NAV_JS = r"""(() => {
  "use strict";

  const HREF = "/launch";
  const LABEL = "New run";
  const LINK_ID = "overlay-new-run";
  const MENU = '[data-slot="sidebar-menu"]';
  const STYLE_ID = "overlay-new-run-style";
  const LINK_STYLE =
    "display:flex;align-items:center;gap:.5rem;height:2rem;padding:0 .5rem;" +
    "border-radius:.375rem;font-size:.875rem;font-weight:500;color:inherit;" +
    "text-decoration:none;";
  const BUTTON_STYLE =
    "position:fixed;top:.75rem;right:.75rem;z-index:2147483647;padding:.45rem .8rem;" +
    "border-radius:.5rem;font:500 13px/1 system-ui,-apple-system,sans-serif;" +
    "text-decoration:none;background:#2563eb;color:#fff;box-shadow:0 2px 8px rgb(0 0 0 / 25%);";

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = "#" + LINK_ID + ":hover{background:rgb(127 127 127 / 16%)}";
    document.head.appendChild(style);
  }

  function removeFallback() {
    const element = document.getElementById(LINK_ID);
    if (element && !element.closest(MENU)) element.remove();
  }

  function ensureSidebarLink() {
    const menu = document.querySelector(MENU);
    if (!menu) return false;
    if (!menu.querySelector('a[href="' + HREF + '"]')) {
      const item = document.createElement("li");
      item.setAttribute("data-slot", "sidebar-menu-item");
      item.setAttribute("data-sidebar", "menu-item");
      const link = document.createElement("a");
      link.id = LINK_ID;
      link.href = HREF;
      link.textContent = LABEL;
      link.style.cssText = LINK_STYLE;
      item.appendChild(link);
      menu.appendChild(item);
      ensureStyle();
    }
    removeFallback();
    return true;
  }

  function ensureFallback() {
    if (document.getElementById(LINK_ID)) return;
    const link = document.createElement("a");
    link.id = LINK_ID;
    link.href = HREF;
    link.textContent = "+ " + LABEL;
    link.style.cssText = BUTTON_STYLE;
    document.body.appendChild(link);
    ensureStyle();
  }

  let settled = false;

  function install() {
    if (ensureSidebarLink()) {
      settled = true;
      return;
    }
    if (settled) ensureFallback();
  }

  function start() {
    install();
    // The SPA hydrates after this script runs. Give the sidebar a moment before
    // falling back to a button the viewer's own chrome does not need.
    window.setTimeout(() => {
      if (!ensureSidebarLink()) {
        settled = true;
        ensureFallback();
      }
    }, 1200);

    let scheduled = false;
    new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(() => {
        scheduled = false;
        install();
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
"""
