"""Optional web UI for the de-identification tool.

Loaded by `praxis-deid serve`. Importing anything from this package requires
the `[serve]` extra (FastAPI, uvicorn, jinja2, python-multipart). The CLI
checks for those imports up-front and prints an actionable install message
if they're missing — see `cli._cmd_serve`.

Privacy invariant: this UI is localhost-only by default. Nothing in this
package makes outbound network calls. The whole point of the de-id tool
is offline operation; the UI must preserve that.
"""

from __future__ import annotations

__all__ = ["build_app", "validate_bind_host"]


def build_app(*args: object, **kwargs: object):  # pragma: no cover - thin re-export
    from .app import build_app as _build_app

    return _build_app(*args, **kwargs)


def validate_bind_host(host: str, *, allow_remote: bool) -> None:  # pragma: no cover - thin re-export
    from .app import validate_bind_host as _v

    _v(host, allow_remote=allow_remote)
