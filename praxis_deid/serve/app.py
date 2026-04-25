"""FastAPI app + bind-host validation for `praxis-deid serve`.

PRIVACY INVARIANT: this app makes NO outbound network calls. No CDNs, no
analytics, no telemetry, no font fetches. The entire UI is served from
local templates + local static files. Every dependency in the [serve]
extra is offline-capable.

BIND-HOST INVARIANT: by default we ONLY bind 127.0.0.1 / ::1 / localhost.
Binding 0.0.0.0 (or anything else) requires `--allow-remote`, which
prints a loud warning to stderr. This is a safety net against an admin
typo exposing the practice's PM data on the LAN.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import jinja2
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .phi_scan import scan_output_dir
from .runner import INPUT_ROLES, RunRequest, execute

# Hosts the service is willing to bind without `--allow-remote`. Anything
# else is treated as "you might be exposing this to the LAN" and is gated.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

# Where this module's bundled assets live. Resolved once at import.
_PKG_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _PKG_DIR / "templates"
STATIC_DIR = _PKG_DIR / "static"


def validate_bind_host(host: str, *, allow_remote: bool) -> None:
    """Refuse non-loopback hosts unless the caller explicitly allows it.

    Raises ValueError on refusal. Prints a stderr warning when allow_remote
    is True and the host is non-loopback — IT admins routinely run with
    `--help`-fatigue and a quiet bind-anywhere is exactly the bug we want
    to be loud about.
    """
    if host in LOOPBACK_HOSTS:
        return
    if not allow_remote:
        raise ValueError(
            f"refusing to bind {host!r}: this UI exposes raw PM data uploads. "
            "Re-run with --allow-remote if you really mean it (and only behind "
            "a trusted firewall)."
        )
    print(
        f"WARNING: praxis-deid serve binding to {host!r} (non-loopback). "
        "This UI accepts raw PHI uploads — anyone who can reach this host "
        "can read your input files. Make sure the network is trusted.",
        file=sys.stderr,
    )


def build_app() -> FastAPI:
    """Construct the FastAPI app. No global state — safe to call per-test."""
    app = FastAPI(
        title="Praxis de-identification (local)",
        # Disable docs UIs — they pull in CDN-hosted Swagger assets in some
        # configs and we do not want to imply outbound calls are happening.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # cache_size=0 sidesteps a Jinja2 LRUCache key-hashing bug seen on
    # CPython 3.15 (jinja2 3.1.6) where dict context kwargs are stuffed
    # into the cache key. The local UI re-renders one tiny template per
    # page load — caching saves nothing meaningful.
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
        cache_size=0,
    )
    templates = Jinja2Templates(env=jinja_env)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        # FastAPI 0.112+ TemplateResponse signature: (request, name, context).
        # The legacy (name, {"request": ..., ...}) form was deprecated and
        # removed; we use the supported form so the UI works with the
        # current pinned FastAPI in the [serve] extra.
        return templates.TemplateResponse(
            request,
            "index.html",
            {"input_roles": INPUT_ROLES},
        )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/run")
    async def run(  # noqa: PLR0913 - form fields, not a refactor target
        practice_id: str = Form(...),
        patient_id_salt: str = Form(...),
        small_n_threshold: int = Form(5),
        audit_log_path: str = Form(""),
        patients: UploadFile | None = File(None),
        appointments: UploadFile | None = File(None),
        providers: UploadFile | None = File(None),
        procedures: UploadFile | None = File(None),
        referrals: UploadFile | None = File(None),
        invoices: UploadFile | None = File(None),
    ) -> JSONResponse:
        uploads: dict[str, UploadFile | None] = {
            "patients": patients,
            "appointments": appointments,
            "providers": providers,
            "procedures": procedures,
            "referrals": referrals,
            "invoices": invoices,
        }

        # Per-run sandbox: inputs in /in, outputs in /out. We do NOT delete
        # this on exit — the admin needs to retrieve the CSVs. The dir
        # name is printed back to the UI so they can find it.
        workdir = Path(tempfile.mkdtemp(prefix="praxis-deid-serve-"))
        in_dir = workdir / "in"
        out_dir = workdir / "out"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        inputs_on_disk: dict[str, Path] = {}
        for role, upload in uploads.items():
            if upload is None or not upload.filename:
                continue
            target = in_dir / f"{role}_raw.csv"
            with target.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
            inputs_on_disk[role] = target

        if not inputs_on_disk:
            raise HTTPException(
                status_code=400,
                detail="no input CSVs uploaded — pick at least patients_raw.csv",
            )

        audit_path = (
            Path(audit_log_path).expanduser()
            if audit_log_path.strip()
            else workdir / "audit.log"
        )

        req = RunRequest(
            practice_id=practice_id,
            patient_id_salt=patient_id_salt,
            small_n_threshold=int(small_n_threshold),
            audit_log_path=audit_path,
            output_dir=out_dir,
            inputs=inputs_on_disk,
        )

        result = execute(req)
        phi_scan = [asdict(s) for s in scan_output_dir(out_dir)]
        # PhiHit dataclasses inside .hits — asdict already recursed.

        payload: dict[str, Any] = {
            "status": result.status,
            "output_dir": result.output_dir,
            "audit_log_path": str(audit_path),
            "files": [asdict(f) for f in result.files],
            "audit_record": result.audit_record,
            "phi_scan": phi_scan,
            "error": result.error,
        }
        return JSONResponse(
            payload,
            status_code=200 if result.status == "success" else 500,
        )

    @app.post("/api/open-folder")
    async def open_folder(payload: dict[str, str]) -> dict[str, str]:
        """Cross-platform "reveal in finder" for the output dir.

        Bound to a POST so accidental GET prefetching can't shell out. The
        path must be a real directory we just wrote — we don't open
        arbitrary user input.
        """
        target = payload.get("path", "")
        p = Path(target).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {target}")
        try:
            _open_in_file_manager(p)
        except (OSError, RuntimeError) as err:
            raise HTTPException(status_code=500, detail=str(err)) from err
        return {"status": "opened", "path": str(p)}

    return app


def _open_in_file_manager(path: Path) -> None:
    """Best-effort 'show in Finder/Explorer' for the output directory.

    macOS: `open <dir>` — works for any path.
    Linux: `xdg-open` if present, else NotImplemented.
    Windows: `explorer.exe` (via `os.startfile`-style invocation).
    """
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)  # noqa: S603, S607
        return
    if sys.platform.startswith("win"):
        # `start` is a cmd built-in; needs shell=True. Path is a Path obj
        # we constructed from a directory we just wrote — safe.
        subprocess.run(  # noqa: S602
            ["cmd", "/c", "start", "", str(path)],
            check=False,
        )
        return
    # Assume freedesktop.
    if shutil.which("xdg-open"):
        subprocess.run(["xdg-open", str(path)], check=False)  # noqa: S603, S607
        return
    raise RuntimeError("no known file manager opener on this platform")


# JSON encoder helper for dataclasses that occasionally show up in payloads.
def _json_default(obj: object) -> object:  # pragma: no cover - defensive
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not serializable: {type(obj)!r}")


__all__ = ["build_app", "validate_bind_host", "LOOPBACK_HOSTS", "_json_default"]
