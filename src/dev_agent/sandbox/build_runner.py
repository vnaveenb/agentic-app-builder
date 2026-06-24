"""Front-end build runner — turns a multi-file React/Angular project into a
static ``dist/`` that the existing static-preview tier can serve.

Framework-agnostic by design: any project whose ``package.json`` declares a
``build`` script is built with ``npm install`` + ``npm run build``. The produced
output directory (``dist/``, ``build/`` or Angular's ``dist/<app>/``) is then
served exactly like a static preview — no long-lived process, port, or
WebSocket. A failing install or build raises ``RuntimeError`` carrying the log
tail so the UI can surface "your code doesn't compile" the same way server-mode
failures are reported.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import shutil
import subprocess
import tempfile

from src.dev_agent.sandbox.base import emit_terminal

logger = logging.getLogger(__name__)

# Generous timeouts — cold npm installs and bundler runs take tens of seconds
# (Angular more). Kept well under the gateway's 300s remote-call ceiling.
_INSTALL_TIMEOUT = 180
_BUILD_TIMEOUT = 180

# Candidate build-output dirs, in priority order. Angular nests under dist/<app>.
_OUTPUT_CANDIDATES = ("dist", "build", "out")

# Absolute root-relative asset refs in the built index.html that must become
# relative so they resolve under the ``/preview/<session>/`` proxy prefix.
_ABS_ASSET_RE = re.compile(rb'(\b(?:src|href)=)(["\'])/(?!/)', re.IGNORECASE)


def _run(cmd: list[str], cwd: str, timeout: int, queue: asyncio.Queue | None, label: str) -> None:
    """Run a build subprocess, streaming output to the terminal. Raise on failure."""
    emit_terminal(queue, "build", f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} failed: '{cmd[0]}' not found in PATH") from exc

    out = (result.stdout or "") + (result.stderr or "")
    if out.strip():
        emit_terminal(queue, "build", out)
    if result.returncode != 0:
        tail = out.strip()[-1500:] or f"exit code {result.returncode}"
        raise RuntimeError(f"{label} failed:\n{tail}")


def _find_build_output(root: pathlib.Path) -> pathlib.Path | None:
    """Locate the built dir containing an index.html (handles Angular's dist/<app>/)."""
    for name in _OUTPUT_CANDIDATES:
        base = root / name
        if not base.is_dir():
            continue
        if (base / "index.html").is_file():
            return base
        # Angular: dist/<app-name>/index.html
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "index.html").is_file():
                return child
    return None


def _relativize_assets(index_html: pathlib.Path) -> None:
    """Rewrite root-absolute asset URLs (/assets/x) to relative (./assets/x).

    Safety net so the build still renders under the proxy prefix even when the
    generated vite.config/base-href forgot the relative-base setting.
    """
    try:
        data = index_html.read_bytes()
    except OSError:
        return
    fixed = _ABS_ASSET_RE.sub(rb"\1\2./", data)
    if fixed != data:
        index_html.write_bytes(fixed)


def build_project(files: dict[str, str], event_queue: asyncio.Queue | None = None) -> tuple[str, str]:
    """Install deps and build a front-end project. Returns (tmpdir_root, dist_dir).

    Blocking — call inside a thread executor. Raises RuntimeError (with the build
    log tail) on install/build failure or if no build output is produced.
    """
    tmpdir = tempfile.mkdtemp(prefix="dev_agent_build_")
    tmp = pathlib.Path(tmpdir)
    try:
        for name, content in files.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        logger.info("Build preview: installing deps for %d files in %s", len(files), tmpdir)
        _run(["npm", "install"], tmpdir, _INSTALL_TIMEOUT, event_queue, "npm install")
        _run(["npm", "run", "build"], tmpdir, _BUILD_TIMEOUT, event_queue, "npm run build")

        dist = _find_build_output(tmp)
        if dist is None:
            raise RuntimeError(
                "Build succeeded but produced no index.html in dist/, build/ or out/"
            )

        _relativize_assets(dist / "index.html")
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)  # don't leak the build dir on failure
        raise

    emit_terminal(event_queue, "build", f"Build complete → serving {dist.name}/")
    logger.info("Build preview ready: dist=%s", dist)
    return tmpdir, str(dist)
