"""The overlay's HTTP surface, mounted on the app Harbor's viewer builds.

`build_app` returns Harbor's own app with the launch routes spliced in front of
it. Nothing under site-packages is written: the static bundle is rebuilt into
`.viewer_static/` from whatever Harbor currently ships.

    python -m viewer_overlay.app --jobs jobs --port 8099
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

from . import harbor_api, launch as specs
from . import registry, runner, static_build
from .middleware import OverlayJSONMiddleware

REPO_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = REPO_ROOT / "jobs"
DATASETS_ROOT = REPO_ROOT / "datasets"
STATIC_DIR = REPO_ROOT / ".viewer_static"
LAUNCH_PAGE = Path(__file__).resolve().parent / "static" / "launch"


def build_app(
    jobs_dir: Path = JOBS_DIR,
    static_target: Path = STATIC_DIR,
    datasets_root: Path = DATASETS_ROOT,
) -> FastAPI:
    """The viewer plus the overlay, sharing one job folder."""
    reg = registry.load()
    static = static_build.build(static_target)
    # Absolute, so a job name in a URL and a log path in a response mean the
    # same thing however the process was started.
    jobs_dir = Path(jobs_dir).expanduser().resolve()
    app = harbor_api.create_app(jobs_dir, "jobs", static_dir=static.target)
    # Harbor registers a catch-all `GET /{path:path}` that serves the SPA, and
    # Starlette matches routes in order. Routes added after create_app returns
    # would answer /launch with the SPA, so the overlay's go in front.
    app.router.routes[0:0] = _overlay_router(reg, Path(jobs_dir), Path(datasets_root)).routes
    app.add_middleware(OverlayJSONMiddleware, jobs_dir=Path(jobs_dir))
    return app


def _overlay_router(
    reg: registry.Registry,
    jobs_dir: Path,
    datasets_root: Path,
) -> APIRouter:
    router = APIRouter()
    # run_matrix.sh keeps its logs beside the job folder, so the overlay does
    # the same: a viewer pointed at another checkout still records there.
    repo_root = jobs_dir.parent

    @router.get("/api/overlay/options")
    def options() -> dict[str, Any]:
        return {
            "agents": [{"id": agent, "label": label} for agent, label in reg.agents()],
            "efforts": list(reg.efforts()),
            "default_dataset": specs.DEFAULTS["dataset"],
            "datasets": [
                {"name": name, "tasks": dataset_tasks(datasets_root / name)}
                for name in dataset_names(datasets_root)
            ],
            "models": [entry.as_dict() for entry in reg.entries],
        }

    @router.get("/api/overlay/jobs/{job_name}/spec")
    def job_spec(job_name: str) -> Any:
        job_dir = job_dir_named(jobs_dir, job_name)
        if job_dir is None:
            return JSONResponse(
                status_code=404, content={"detail": f"unknown job: {job_name}"}
            )
        resolution = specs.resolve_from_job(job_dir, reg, datasets_root, job_name)
        return {
            "spec": resolution.spec.as_dict() if resolution.spec else resolution.partial,
            "unresolved": list(resolution.unresolved),
        }

    @router.post("/api/overlay/launches")
    async def create_launch(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400, content={"detail": "body must be JSON"}
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400, content={"detail": "body must be a JSON object"}
            )
        resolution = resolve_payload(body, reg, datasets_root, jobs_dir)
        if not resolution.ok:
            return JSONResponse(
                status_code=409,
                content={
                    "unresolved": list(resolution.unresolved),
                    "partial": resolution.partial,
                },
            )
        started = runner.start(resolution.spec, repo_root=repo_root)
        return JSONResponse(
            status_code=201,
            content={
                "launch_id": started.launch_id,
                "job_prefix": started.job_prefix,
                "log_path": str(started.log_path),
            },
        )

    @router.get("/api/overlay/launches")
    def list_launches() -> list[dict[str, Any]]:
        return runner.list_launches(repo_root=repo_root)

    # Harbor's own /run form builds its agent picker from its built-in agents,
    # so Amp and Droid are missing there. The overlay form builds from the
    # registry. Send both blank runs and job reruns to the registry-aware page.
    @router.get("/run")
    def run_page(request: Request) -> RedirectResponse:
        target = "/launch"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=307)

    NO_STORE = {"cache-control": "no-store"}

    @router.get("/launch")
    def launch_page() -> FileResponse:
        return FileResponse(
            LAUNCH_PAGE / "index.html", media_type="text/html", headers=NO_STORE
        )

    @router.get("/launch/launch.js")
    def launch_script() -> FileResponse:
        return FileResponse(
            LAUNCH_PAGE / "launch.js", media_type="text/javascript", headers=NO_STORE
        )

    @router.get("/launch/launch.css")
    def launch_styles() -> FileResponse:
        return FileResponse(
            LAUNCH_PAGE / "launch.css", media_type="text/css", headers=NO_STORE
        )

    @router.get("/overlay/nav.js")
    def nav_script() -> PlainTextResponse:
        return PlainTextResponse(
            static_build.NAV_JS,
            media_type="application/javascript",
            headers={"cache-control": "no-store"},
        )

    return router


def resolve_payload(
    body: dict[str, Any],
    reg: registry.Registry,
    datasets_root: Path,
    jobs_dir: Path,
) -> specs.Resolution:
    """Merge a rerun's derived fields with whatever the request states outright."""
    candidates: dict[str, Any] = {}
    from_job = body.get("from_job")
    if from_job is not None:
        if not isinstance(from_job, str) or not from_job:
            return specs.Resolution(
                unresolved=("job: from_job must be a name",),
                spec=None,
                partial=specs.partial_fields({}),
            )
        base_dir = job_dir_named(jobs_dir, from_job)
        if base_dir is None:
            return specs.unresolved_job(from_job)
        candidates.update(specs.resolve_from_job(base_dir, reg, datasets_root, from_job).partial)

    # Anything the request states outright wins over the job it copies.
    candidates.update(
        {
            key: value
            for key, value in body.items()
            if key in specs.SPEC_FIELDS and value is not None
        }
    )
    return specs.resolve(candidates, reg, datasets_root)


def dataset_names(root: Path) -> list[str]:
    """Every dataset directory a run can name, sorted."""
    if not root.is_dir():
        return []
    return sorted(
        path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )


def dataset_tasks(root: Path) -> list[str]:
    """The task directories a run can name, sorted."""
    if not root.is_dir():
        return []
    return sorted(
        path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )


def job_dir_named(jobs_dir: Path, job_name: str) -> Path | None:
    """Resolve one job folder, refusing anything that escapes the jobs dir."""
    if not job_name or job_name in (".", "..") or "/" in job_name or "\\" in job_name:
        return None
    candidate = (jobs_dir / job_name).resolve()
    try:
        candidate.relative_to(jobs_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harbor viewer with the launch overlay.")
    parser.add_argument("--jobs", default=str(JOBS_DIR), help="folder the viewer reads")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(build_app(Path(args.jobs)), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
