"""FastAPI app: REST config CRUD + WebSocket live monitoring.

The app holds a reference to the `Controller` from main.py, which mediates
config persistence and hot-applies changes to MergerState and the sender.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from pathlib import Path
from typing import Annotated

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
import yaml

from .. import __version__
from ..config import Config, OutputCfg, SourceCfg

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Paths used by the in-place updater. Hard-coded to the system install layout
# (see image/stage-artnet-htp/01-install-merger/01-run-chroot.sh). A dev run
# from a local venv won't match these and the updater stays disabled.
SYSTEM_VENV_PIP = Path("/opt/artnet-htp/.venv/bin/pip")
UPDATE_DOWNLOAD_TIMEOUT_S = 120  # tarball is small (~50KB) but allow slow links
UPDATE_PIP_TIMEOUT_S = 180


# --------------------------- network config (v0.3.0+) --------------------------- #
#
# nmcli is the only supported backend (Bookworm Lite uses NetworkManager). We
# don't talk to systemd-networkd or dhcpcd. The image stage installs
# /etc/sudoers.d/artnet-nmcli giving the `artnet` user passwordless sudo for
# exactly nmcli + /sbin/reboot. Anything else still needs a real sudoer.

NMCLI = "/usr/bin/nmcli"
# The connection profile name shipped by Raspberry Pi OS Bookworm. If the
# user has multiple ethernet profiles this picks the first one matching
# "Wired connection *". For real-world venue Pis there's only one.
NMCLI_WIRED_CONN = "Wired connection 1"


def _nmcli_available() -> bool:
    from pathlib import Path as _P
    return _P(NMCLI).exists()


def _read_network_state() -> dict:
    """Query nmcli for the current ethernet profile state.

    Returns: {"mode": "dhcp"|"static", "ip": "...", "prefix": int,
              "gateway": "...", "dns": "...", "raw": "...", "available": bool}
    On a dev machine without nmcli, returns {"available": False}.
    """
    import subprocess
    if not _nmcli_available():
        return {"available": False, "mode": "dhcp"}
    try:
        proc = subprocess.run(
            [NMCLI, "-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns",
             "con", "show", NMCLI_WIRED_CONN],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"available": True, "error": str(e), "mode": "dhcp"}
    if proc.returncode != 0:
        return {"available": True, "error": proc.stderr.strip() or "nmcli failed", "mode": "dhcp"}

    parsed = {"mode": "dhcp", "ip": "", "prefix": 24, "gateway": "", "dns": "",
              "available": True}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if key == "ipv4.method":
            parsed["mode"] = "static" if val == "manual" else "dhcp"
        elif key == "ipv4.addresses" and val and val != "--":
            # "192.168.1.50/24" — split into ip + prefix
            addr = val.split(",")[0].strip()
            if "/" in addr:
                ip, _, p = addr.partition("/")
                parsed["ip"] = ip
                try:
                    parsed["prefix"] = int(p)
                except ValueError:
                    pass
            else:
                parsed["ip"] = addr
        elif key == "ipv4.gateway" and val and val != "--":
            parsed["gateway"] = val
        elif key == "ipv4.dns" and val and val != "--":
            parsed["dns"] = val
    return parsed


def _apply_network_state(state: "NetworkState") -> dict:
    """Apply a new IPv4 config via nmcli. Returns {"ok": bool, ...}.

    Does NOT bring the interface up — the operator reboots after to settle
    things cleanly. nmcli's "con up" can briefly leave the device in a half-
    state that confuses pixel controllers; a clean reboot is friendlier.
    """
    import subprocess
    if not _nmcli_available():
        return {"ok": False, "error": "nmcli not present (dev box?)"}

    cmds = []
    if state.mode == "dhcp":
        cmds.append([NMCLI, "con", "mod", NMCLI_WIRED_CONN,
                     "ipv4.method", "auto",
                     "ipv4.addresses", "",
                     "ipv4.gateway", "",
                     "ipv4.dns", ""])
    else:
        addr = f"{state.ip}/{state.prefix}"
        cmds.append([NMCLI, "con", "mod", NMCLI_WIRED_CONN,
                     "ipv4.method", "manual",
                     "ipv4.addresses", addr,
                     "ipv4.gateway", state.gateway,
                     "ipv4.dns", state.dns or state.gateway])

    for cmd in cmds:
        full = ["sudo", "-n", *cmd]
        try:
            proc = subprocess.run(full, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "error": f"nmcli invocation failed: {e}"}
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout).strip() or "nmcli returned non-zero"}
    return {"ok": True, "needs_reboot": True}


def _running_under_system_venv() -> bool:
    """True when the running Python is /opt/artnet-htp/.venv/bin/python.

    The in-place updater only works in that exact layout — we need pip in the
    same venv we're running from so the install replaces our own code.
    """
    import sys
    return sys.executable.startswith("/opt/artnet-htp/.venv/") and SYSTEM_VENV_PIP.exists()


def _install_tarball(src_tar_url: str, tag: str) -> dict:
    """Synchronous installer — call from asyncio.to_thread.

    Steps:
      1. Download src tarball to /tmp/artnet-htp-update-<tag>.tar.gz
      2. Extract to /tmp/artnet-htp-update-<tag>/
      3. Run `pip install --upgrade <extracted>` into the system venv
      4. Return {"ok": True} or {"ok": False, "error": "..."}

    On success the caller schedules os._exit(0) and systemd restarts us with
    the new code. On failure the running install is unchanged.

    NOTE: pip can overwrite files of the currently-running process on Linux —
    inodes stay open even when the file is unlinked or replaced. So pip
    completing successfully and then us exiting cleanly is safe.
    """
    import shutil
    import subprocess
    import tarfile
    import tempfile
    import urllib.request

    log.info("update: downloading %s", src_tar_url)
    work = Path(tempfile.mkdtemp(prefix=f"artnet-htp-update-{tag}-"))
    tar_path = work / "src.tar.gz"
    try:
        try:
            with urllib.request.urlopen(src_tar_url, timeout=UPDATE_DOWNLOAD_TIMEOUT_S) as resp:
                with tar_path.open("wb") as f:
                    shutil.copyfileobj(resp, f)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"download failed: {e}"}

        log.info("update: extracting %s", tar_path)
        try:
            with tarfile.open(tar_path, "r:gz") as tf:
                # Refuse absolute paths or .. traversal — tarball was built
                # with --transform "s,^,artnet-htp-<tag>/," so everything
                # should be rooted under that prefix.
                safe_prefix = f"artnet-htp-{tag}/"
                for member in tf.getmembers():
                    if member.name.startswith("/") or ".." in Path(member.name).parts:
                        return {"ok": False, "error": f"unsafe tar member: {member.name}"}
                    if not member.name.startswith(safe_prefix) and member.name != safe_prefix.rstrip("/"):
                        return {"ok": False, "error": f"tar member outside expected prefix: {member.name}"}
                tf.extractall(work)
        except tarfile.TarError as e:
            return {"ok": False, "error": f"extract failed: {e}"}

        extracted_dir = work / f"artnet-htp-{tag}"
        if not extracted_dir.is_dir():
            return {"ok": False, "error": f"expected dir {extracted_dir} not in tarball"}

        log.info("update: pip-installing %s", extracted_dir)
        try:
            proc = subprocess.run(
                [str(SYSTEM_VENV_PIP), "install", "--upgrade", str(extracted_dir)],
                capture_output=True, text=True, timeout=UPDATE_PIP_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"pip install timed out after {UPDATE_PIP_TIMEOUT_S}s"}
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-2000:]
            log.error("update: pip install failed (rc=%d): %s", proc.returncode, tail)
            return {"ok": False, "error": f"pip install rc={proc.returncode}: {tail}"}

        log.info("update: install OK, exiting for systemd restart")
        return {"ok": True}
    finally:
        # Cleanup best-effort — don't fail the install over a leftover /tmp
        # dir, just log.
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

# Optional CI-baked metadata. The pi-gen build stage writes this; on a dev
# install the file won't exist and we fall back to runtime-only fields.
BUILD_INFO_PATH = Path("/etc/artnet-htp/build-info.json")
# Written by the first-boot config-import service when an operator-dropped
# config on the boot partition fails validation. Surfaced to the UI so the
# operator can see *why* their config wasn't applied.
FIRSTBOOT_ERROR_PATH = Path("/etc/artnet-htp/firstboot-error.txt")

STATUS_PUSH_PERIOD_S = 0.5    # 2Hz
DMX_PUSH_PERIOD_S = 0.1       # 10Hz
DMX_FRAME_HEADER_LEN = 3      # u16 LE universe + u8 counter


def create_app(controller) -> FastAPI:
    """Build the FastAPI app bound to a Controller (see main.py)."""
    app = FastAPI(
        title="ArtNet HTP Merger",
        version=__version__,
        docs_url=None,    # no /docs by default
        redoc_url=None,
    )
    app.state.controller = controller

    # ---- static UI ----
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # ---- REST: version + build info ----
    @app.get("/api/version")
    async def get_version() -> JSONResponse:
        """Return the running version + optional CI-baked build metadata.

        Schema:
          {
            "version": "0.2.0",
            "git_sha": "abc1234" | null,
            "built_at": "2026-05-20T14:30:00Z" | null,
            "image": "artnet-htp-v0.2.0" | null,
            "last_firstboot_error": "..." | null
          }
        """
        info: dict = {
            "version": __version__,
            "git_sha": None,
            "built_at": None,
            "image": None,
            "last_firstboot_error": None,
        }
        # Optional build-info.json baked by CI into the image.
        try:
            if BUILD_INFO_PATH.is_file():
                with BUILD_INFO_PATH.open("r", encoding="utf-8") as f:
                    baked = json.load(f)
                for k in ("git_sha", "built_at", "image"):
                    if k in baked:
                        info[k] = baked[k]
                # Allow the build-info file to override the version too, useful
                # if a dev edits it for testing.
                if "version" in baked:
                    info["version"] = baked["version"]
        except (OSError, json.JSONDecodeError) as e:
            log.warning("could not read %s: %s", BUILD_INFO_PATH, e)
        # Optional firstboot-error.txt written by the first-boot service.
        try:
            if FIRSTBOOT_ERROR_PATH.is_file():
                err = FIRSTBOOT_ERROR_PATH.read_text(encoding="utf-8").strip()
                if err:
                    info["last_firstboot_error"] = err
        except OSError as e:
            log.warning("could not read %s: %s", FIRSTBOOT_ERROR_PATH, e)
        return JSONResponse(info)

    # ---- REST: state snapshot ----
    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(controller.state.snapshot())

    # ---- REST: restart self ----
    # Schedules os._exit(0) after a tiny delay so the response can flush.
    # Relies on the systemd unit's `Restart=always` to bring us back up.
    # Without systemd (a dev `python -m artnet_htp` run), this just kills
    # the process — operator restarts manually.
    @app.post("/api/restart")
    async def post_restart() -> JSONResponse:
        async def _exit_soon() -> None:
            await asyncio.sleep(0.3)
            log.info("restart requested via /api/restart — exiting for systemd to restart us")
            import os
            os._exit(0)
        asyncio.create_task(_exit_soon())
        return JSONResponse({"ok": True, "restarting": True})

    # ---- REST: GitHub update check ----
    # Read-only. Compares running version to the latest non-prerelease tag
    # at github.com/djkoren/artnet-htp/releases. Returns a deep-link to the
    # release page; the actual flash/update workflow lives off-Pi (step 8b
    # will add in-place install).
    @app.get("/api/update/status")
    async def get_update_status() -> JSONResponse:
        current = __version__
        try:
            if BUILD_INFO_PATH.is_file():
                with BUILD_INFO_PATH.open("r", encoding="utf-8") as f:
                    baked = json.load(f)
                if "version" in baked:
                    current = baked["version"]
        except (OSError, json.JSONDecodeError):
            pass

        import urllib.error
        import urllib.request

        def _fetch_latest() -> dict:
            req = urllib.request.Request(
                "https://api.github.com/repos/djkoren/artnet-htp/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "artnet-htp"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())

        try:
            data = await asyncio.to_thread(_fetch_latest)
        except urllib.error.URLError as e:
            return JSONResponse({"current": current, "error": f"can't reach GitHub: {e.reason}"})
        except Exception as e:  # noqa: BLE001 — surface to operator
            return JSONResponse({"current": current, "error": f"check failed: {e}"})

        latest = data.get("tag_name", "")
        release_url = data.get("html_url", "https://github.com/djkoren/artnet-htp/releases")

        # Normalize comparison: strip leading 'v' from both sides.
        def _norm(t: str) -> str:
            return t.lstrip("vV").strip()

        # Surface the source tarball asset URL so the in-place updater knows
        # what to download. Naming convention: artnet-htp-<tag>-src.tar.gz
        src_tar_url = None
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if name.startswith("artnet-htp-") and name.endswith("-src.tar.gz"):
                src_tar_url = asset.get("browser_download_url")
                break

        update_available = bool(latest) and _norm(latest) != _norm(current)
        return JSONResponse({
            "current": current,
            "latest": latest,
            "release_url": release_url,
            "update_available": update_available,
            "src_tar_url": src_tar_url,
            "can_install": bool(src_tar_url) and _running_under_system_venv(),
        })

    # ---- REST: in-place install of a newer release ----
    # Downloads the source tarball from GitHub, extracts to /tmp, pip-installs
    # into the existing venv at /opt/artnet-htp/.venv, then os._exit so systemd
    # restarts the service with the new code. If anything fails before the
    # exit, the running install is untouched and the operator gets an error.
    #
    # Restricted to github.com/djkoren/artnet-htp (URL pin in update/status).
    # Only works when running under /opt/artnet-htp/.venv — a dev run on a
    # laptop will get 503.
    @app.post("/api/update/install")
    async def post_update_install() -> JSONResponse:
        if not _running_under_system_venv():
            raise HTTPException(503, "not running under /opt/artnet-htp/.venv — "
                                "in-place update only works on a system install")

        # Re-fetch latest to get a fresh URL — don't trust client-provided URL.
        import urllib.error
        import urllib.request

        def _fetch_latest() -> dict:
            req = urllib.request.Request(
                "https://api.github.com/repos/djkoren/artnet-htp/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "artnet-htp"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())

        try:
            data = await asyncio.to_thread(_fetch_latest)
        except urllib.error.URLError as e:
            raise HTTPException(502, f"can't reach GitHub: {e.reason}")

        tag = data.get("tag_name", "")
        if not tag:
            raise HTTPException(502, "GitHub returned no tag_name")

        src_tar_url = None
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if name.startswith("artnet-htp-") and name.endswith("-src.tar.gz"):
                # Sanity-check the host so a compromised redirect can't point
                # us at a non-Anthropic-...err, non-djkoren artifact.
                url = asset.get("browser_download_url", "")
                if url.startswith("https://github.com/djkoren/artnet-htp/"):
                    src_tar_url = url
                break
        if not src_tar_url:
            raise HTTPException(502, f"release {tag} has no -src.tar.gz asset")

        # Heavy lifting in a worker thread so we don't block the event loop.
        result = await asyncio.to_thread(_install_tarball, src_tar_url, tag)
        if not result.get("ok"):
            raise HTTPException(500, result.get("error", "install failed"))

        # Hand control back to systemd. Tiny delay so the response can flush.
        async def _exit_soon() -> None:
            await asyncio.sleep(0.4)
            log.info("update installed (%s) — exiting for systemd restart", tag)
            import os
            os._exit(0)
        asyncio.create_task(_exit_soon())
        return JSONResponse({"ok": True, "installed": tag, "restarting": True})

    # ---- REST: config ----
    @app.get("/api/config")
    async def get_config() -> JSONResponse:
        return JSONResponse(controller.current_config().model_dump(mode="json"))

    @app.put("/api/config")
    async def put_config(req: Request) -> JSONResponse:
        body = await req.json()
        try:
            new_cfg = Config.model_validate(body)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        controller.apply_config(new_cfg)
        return JSONResponse({"ok": True})

    # ---- REST: config export/import (YAML) ----
    @app.get("/api/config/export")
    async def export_config() -> Response:
        """Download the current config as YAML.

        Drop this onto a fresh SD card's boot partition (as
        `artnet-htp-config.yaml`) and the next first-boot will auto-import it.
        """
        cfg = controller.current_config()
        data = yaml.safe_dump(
            cfg.model_dump(mode="json"),
            sort_keys=False,
            default_flow_style=False,
        )
        return Response(
            content=data,
            media_type="application/x-yaml",
            headers={
                "Content-Disposition":
                    'attachment; filename="artnet-htp-config.yaml"',
            },
        )

    @app.post("/api/config/import")
    async def import_config(req: Request) -> JSONResponse:
        """Replace the current config from an uploaded YAML body.

        Accepts the YAML as the raw request body (any content-type — we don't
        check, since the parser is YAML-or-bust regardless). Wholesale replace,
        not merge. Validates with Pydantic; rejects with 400 on bad YAML or
        schema errors.
        """
        raw = await req.body()
        if not raw:
            raise HTTPException(400, "empty request body")
        try:
            parsed = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as e:
            raise HTTPException(400, f"could not parse YAML: {e}")
        if not isinstance(parsed, dict):
            raise HTTPException(400, "config must be a YAML mapping at the top level")
        try:
            new_cfg = Config.model_validate(parsed)
        except ValidationError as e:
            raise HTTPException(400, f"invalid config: {e}")
        controller.apply_config(new_cfg)
        return JSONResponse({"ok": True})

    # ---- REST: source CRUD ----
    @app.post("/api/sources")
    async def add_source(body: SourceCfg) -> JSONResponse:
        cfg = controller.current_config().model_copy(deep=True)
        if any(s.ip == body.ip for s in cfg.sources):
            raise HTTPException(409, f"source {body.ip} already exists")
        cfg.sources.append(body)
        controller.apply_config(cfg)
        return JSONResponse({"ok": True})

    @app.delete("/api/sources/{ip}")
    async def del_source(ip: str) -> JSONResponse:
        cfg = controller.current_config().model_copy(deep=True)
        before = len(cfg.sources)
        cfg.sources = [s for s in cfg.sources if s.ip != ip]
        if len(cfg.sources) == before:
            raise HTTPException(404, f"source {ip} not found")
        controller.apply_config(cfg)
        return JSONResponse({"ok": True})

    @app.post("/api/sources/allow")
    async def allow_source(body: AllowSourceBody) -> JSONResponse:
        cfg = controller.current_config().model_copy(deep=True)
        if not any(s.ip == body.ip for s in cfg.sources):
            cfg.sources.append(SourceCfg(ip=body.ip, label=body.label or body.ip))
            controller.apply_config(cfg)
        return JSONResponse({"ok": True})

    # ---- REST: output CRUD ----
    @app.post("/api/outputs")
    async def add_output(body: OutputCfg) -> JSONResponse:
        cfg = controller.current_config().model_copy(deep=True)
        if any(o.ip == body.ip for o in cfg.outputs):
            raise HTTPException(409, f"output {body.ip} already exists")
        cfg.outputs.append(body)
        controller.apply_config(cfg)
        return JSONResponse({"ok": True})

    @app.delete("/api/outputs/{ip}")
    async def del_output(ip: str) -> JSONResponse:
        cfg = controller.current_config().model_copy(deep=True)
        before = len(cfg.outputs)
        cfg.outputs = [o for o in cfg.outputs if o.ip != ip]
        if len(cfg.outputs) == before:
            raise HTTPException(404, f"output {ip} not found")
        controller.apply_config(cfg)
        return JSONResponse({"ok": True})

    # ---- REST: universe CRUD ----
    # v0.3.0 dropped the standalone universe list. Universes now live on each
    # output as a `universes` field; manage them by editing the output and
    # PUTting the whole config. The /api/universes routes are gone — return
    # 410 Gone so any old clients see a clear signal rather than a silent
    # success that drops their data.
    @app.post("/api/universes")
    async def gone_post_universes() -> JSONResponse:
        raise HTTPException(
            410, "v0.3.0 moved universes onto each output. PUT /api/config "
            "with output.universes lists instead.",
        )

    @app.delete("/api/universes/{port_address}")
    async def gone_del_universe(port_address: int) -> JSONResponse:
        raise HTTPException(
            410, "v0.3.0 moved universes onto each output. PUT /api/config "
            "with output.universes lists instead.",
        )

    # ---- REST: network configuration ----
    # GET reports current state. POST writes a new state (DHCP or static) via
    # nmcli, which requires passwordless sudo for the `artnet` user — the
    # image stage installs /etc/sudoers.d/artnet-nmcli to grant it. Applying
    # changes does NOT bring the interface up; the operator clicks
    # POST /api/network/reboot afterward (or power-cycles), and the UI polls
    # both old + new URLs to redirect when the Pi comes back.
    @app.get("/api/network")
    async def get_network() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(_read_network_state))

    @app.post("/api/network")
    async def post_network(req: Request) -> JSONResponse:
        body = await req.json()
        try:
            new_state = NetworkState.model_validate(body)
        except ValidationError as e:
            raise HTTPException(400, f"invalid network payload: {e}")
        result = await asyncio.to_thread(_apply_network_state, new_state)
        if not result.get("ok"):
            raise HTTPException(500, result.get("error", "nmcli failed"))
        return JSONResponse({"ok": True, "applied": new_state.model_dump(), **result})

    @app.post("/api/network/reboot")
    async def post_network_reboot() -> JSONResponse:
        if not _nmcli_available():
            raise HTTPException(503, "no nmcli on this host — reboot from your terminal")
        async def _reboot_soon() -> None:
            await asyncio.sleep(0.4)
            log.info("reboot requested via /api/network/reboot — exec'ing /sbin/reboot")
            import subprocess
            subprocess.Popen(["sudo", "-n", "/sbin/reboot"])
        asyncio.create_task(_reboot_soon())
        return JSONResponse({"ok": True, "rebooting": True})

    # ---- WebSocket: live state ----
    @app.websocket("/ws/state")
    async def ws_state(ws: WebSocket) -> None:
        await ws.accept()
        watched: set[int] = set()
        last_dmx: dict[int, bytes] = {}
        last_dmx_sent_at: dict[int, float] = {}
        dmx_counter: dict[int, int] = {}

        status_task = asyncio.create_task(_push_status_loop(ws, controller))
        try:
            while True:
                # Wait for either client message OR a DMX push tick
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=DMX_PUSH_PERIOD_S)
                    await _handle_ws_message(msg, watched, last_dmx, last_dmx_sent_at)
                except asyncio.TimeoutError:
                    pass
                except WebSocketDisconnect:
                    break

                # Push DMX preview frames for watched universes
                for u in list(watched):
                    frame = _build_dmx_frame(controller, u, last_dmx, dmx_counter)
                    if frame is None:
                        continue
                    try:
                        await ws.send_bytes(frame)
                    except RuntimeError:
                        # connection closing
                        break
        except WebSocketDisconnect:
            pass
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass

    return app


# ---------- helper request bodies ----------

class AllowSourceBody(BaseModel):
    ip: str
    label: str = ""


class UniverseBody(BaseModel):
    # Kept for the /api/universes 410-Gone handlers — the field shape lets
    # FastAPI fail-soft on bodies that match the old schema instead of 422'ing.
    port_address: Annotated[int, Field(ge=0, le=0x7FFF)] | None = None
    port_addresses: list[Annotated[int, Field(ge=0, le=0x7FFF)]] | None = None


class NetworkState(BaseModel):
    """Body for POST /api/network.

    `mode: dhcp` → ignore the other fields; nmcli flips ipv4.method to auto.
    `mode: static` → ip + prefix + gateway required; dns defaults to gateway.
    """
    mode: str  # "dhcp" or "static"
    ip: str = ""
    prefix: Annotated[int, Field(ge=1, le=32)] = 24
    gateway: str = ""
    dns: str = ""

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        if v not in ("dhcp", "static"):
            raise ValueError(f"mode must be 'dhcp' or 'static', got {v!r}")
        return v

    @model_validator(mode="after")
    def _static_fields_present(self) -> "NetworkState":
        if self.mode == "static":
            if not self.ip:
                raise ValueError("static mode requires `ip`")
            if not self.gateway:
                raise ValueError("static mode requires `gateway`")
        return self


# ---------- WS helpers ----------

async def _handle_ws_message(
    msg: dict,
    watched: set[int],
    last_dmx: dict[int, bytes],
    last_dmx_sent_at: dict[int, float],
) -> None:
    text = msg.get("text")
    if not text:
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return
    t = payload.get("type")
    if t == "watch":
        u = int(payload.get("universe", -1))
        if 0 <= u <= 0x7FFF:
            watched.add(u)
    elif t == "unwatch":
        u = int(payload.get("universe", -1))
        watched.discard(u)
        last_dmx.pop(u, None)
        last_dmx_sent_at.pop(u, None)


async def _push_status_loop(ws: WebSocket, controller) -> None:
    """Push JSON status @ 2Hz until the socket closes."""
    try:
        while True:
            snap = controller.state.snapshot()
            try:
                await ws.send_text(json.dumps({"type": "status", "data": snap}))
            except RuntimeError:
                return
            await asyncio.sleep(STATUS_PUSH_PERIOD_S)
    except asyncio.CancelledError:
        return


def _build_dmx_frame(
    controller,
    universe: int,
    last_dmx: dict[int, bytes],
    dmx_counter: dict[int, int],
) -> bytes | None:
    """Build a binary DMX frame for one universe, or None if no fresh data."""
    merged = controller.state.get_merged_for_preview(universe)
    if merged is None:
        merged = bytes(512)  # all-zero placeholder
    prev = last_dmx.get(universe)
    # Diff threshold: send if >5% of channels changed, OR every 2s as keepalive
    counter = dmx_counter.get(universe, 0)
    if prev is not None:
        diffs = sum(1 for a, b in zip(merged, prev) if a != b)
        if diffs < 26 and counter % 20 != 0:  # ~2s at 10Hz tick
            dmx_counter[universe] = counter + 1
            return None
    last_dmx[universe] = merged
    dmx_counter[universe] = counter + 1
    header = struct.pack("<HB", universe, counter & 0xFF)
    return header + merged
