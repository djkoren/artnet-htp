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
from pydantic import BaseModel, Field, ValidationError
import yaml

from .. import __version__
from ..config import Config, OutputCfg, SourceCfg

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

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

        update_available = bool(latest) and _norm(latest) != _norm(current)
        return JSONResponse({
            "current": current,
            "latest": latest,
            "release_url": release_url,
            "update_available": update_available,
        })

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
    @app.post("/api/universes")
    async def add_universe(body: UniverseBody) -> JSONResponse:
        cfg = controller.current_config().model_copy(deep=True)
        if body.port_address in cfg.universes:
            raise HTTPException(409, f"universe {body.port_address} already exists")
        cfg.universes = sorted(cfg.universes + [body.port_address])
        controller.apply_config(cfg)
        return JSONResponse({"ok": True})

    @app.delete("/api/universes/{port_address}")
    async def del_universe(port_address: int) -> JSONResponse:
        cfg = controller.current_config().model_copy(deep=True)
        if port_address not in cfg.universes:
            raise HTTPException(404, f"universe {port_address} not found")
        cfg.universes = [u for u in cfg.universes if u != port_address]
        controller.apply_config(cfg)
        return JSONResponse({"ok": True})

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
    port_address: Annotated[int, Field(ge=0, le=0x7FFF)]


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
