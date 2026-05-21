"""Entry point for the ArtNet HTP merger.

Wires together: receiver (asyncio), sender (thread), ArtPollReply (asyncio),
config persistence, and the FastAPI web UI (uvicorn).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from .config import Config, load_config, save_config
from .poll import NodeIdentity, PollReplyService, detect_local_ip, detect_mac
from .protocol import ARTNET_PORT
from .receiver import start_receiver
from .sender import SenderThread
from .state import MergerState
from .web.app import create_app

log = logging.getLogger(__name__)


class Controller:
    """Owns the full runtime: state, sender, receiver, poll service. Mediates
    config changes between the web UI and the live components.
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._config = load_config(config_path)
        self.state = MergerState(
            source_timeout_s=self._config.source_timeout_s,
            send_keepalive_when_silent=self._config.send_keepalive_when_silent,
            auto_allow_unknown_sources=self._config.auto_allow_unknown_sources,
        )
        self._apply_to_state(self._config)

        self.sender = SenderThread(
            self.state,
            send_rate_hz=self._config.send_rate_hz,
        )

        # Identity for ArtPollReply
        adv_ip = self._config.bind_ip if self._config.bind_ip != "0.0.0.0" else detect_local_ip()
        self.poll = PollReplyService(
            self.state,
            NodeIdentity(
                bind_ip=adv_ip,
                mac=detect_mac(),
                short_name=self._config.node.short_name,
                long_name=self._config.node.long_name,
            ),
        )

        self._receiver_transport = None
        self._poll_task: asyncio.Task | None = None

    # ----- config -----
    def current_config(self) -> Config:
        return self._config

    def apply_config(self, new_cfg: Config) -> None:
        """Hot-apply a new config: persist YAML, update state, update sender
        rate, refresh poll identity (including advertised IP).

        Web host/port still require a restart (uvicorn is bound at startup).
        bind_ip used to require a restart too; since v0.2.5 it's
        advertise-only and is applied live here.
        """
        save_config(self.config_path, new_cfg)
        self._config = new_cfg
        self._apply_to_state(new_cfg)
        self.sender.set_rate(new_cfg.send_rate_hz)
        # Refresh node identity (advertised IP + names).
        adv_ip = new_cfg.bind_ip if new_cfg.bind_ip != "0.0.0.0" else detect_local_ip()
        self.poll.identity.bind_ip = adv_ip
        self.poll.identity.short_name = new_cfg.node.short_name
        self.poll.identity.long_name = new_cfg.node.long_name

    def _apply_to_state(self, cfg: Config) -> None:
        self.state.apply_config(
            sources=cfg.to_source_specs(),
            outputs=cfg.to_output_specs(),
            universes=list(cfg.universes),
            source_timeout_s=cfg.source_timeout_s,
            send_keepalive_when_silent=cfg.send_keepalive_when_silent,
            auto_allow_unknown_sources=cfg.auto_allow_unknown_sources,
        )

    # ----- lifecycle -----
    async def start_async(self) -> None:
        self.sender.start()

        async def on_artpoll(src_ip: str, src_port: int) -> None:
            await self.poll.respond_to(src_ip, src_port)

        # Receiver ALWAYS binds 0.0.0.0 so ArtNet from any interface lands.
        # config.bind_ip is treated purely as the *advertised* IP (see line
        # ~51 where we feed it to PollReplyService). This separation prevents
        # a misconfigured bind_ip from crash-looping the service under
        # systemd, which is what bit us pre-v0.2.5.
        self._receiver_transport, _ = await start_receiver(
            self.state,
            on_artpoll,
            bind_ip="0.0.0.0",
            port=ARTNET_PORT,
        )
        self._poll_task = asyncio.create_task(self.poll.run_periodic())
        log.info(
            "merger online: advertised_ip=%s sources=%d outputs=%d universes=%s rate=%dHz",
            self.poll.identity.bind_ip,
            len(self._config.sources),
            len(self._config.outputs),
            self._config.universes,
            int(self._config.send_rate_hz),
        )

    async def stop_async(self) -> None:
        log.info("shutting down")
        if self._receiver_transport is not None:
            self._receiver_transport.close()
            self._receiver_transport = None
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        self.poll.stop()
        self.sender.stop(timeout=2.0)


async def _serve(controller: Controller) -> None:
    cfg = controller.current_config()
    app = create_app(controller)
    server_config = uvicorn.Config(
        app=app,
        host=cfg.web.host,
        port=cfg.web.port,
        log_level="info",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(server_config)

    await controller.start_async()

    # Install signal handlers that close the server cleanly
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / restricted envs — fall back to KeyboardInterrupt
            pass

    server_task = asyncio.create_task(server.serve())
    stop_task = asyncio.create_task(stop.wait())
    done, pending = await asyncio.wait(
        {server_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    server.should_exit = True
    for t in pending:
        t.cancel()
    try:
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception:
        pass

    await controller.stop_async()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="artnet-htp",
                                description="ArtNet HTP merger.")
    p.add_argument("--config", default="config.yaml",
                   help="Path to config.yaml. Default: ./config.yaml")
    p.add_argument("--init", action="store_true",
                   help="Write a starter config.yaml if it doesn't exist, then exit.")
    p.add_argument("--validate-config", metavar="PATH", default=None,
                   help="Load+validate the YAML at PATH. Exit 0 on success, "
                        "non-zero with a human error on failure. Used by the "
                        "first-boot service before applying operator-dropped configs.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable debug logging.")
    args = p.parse_args(argv)
    _setup_logging(args.verbose)

    if args.validate_config:
        try:
            load_config(args.validate_config)
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            # Pydantic ValidationError, YAML parse errors, etc.
            print(f"ERROR: {args.validate_config} is not a valid artnet-htp config:\n{e}",
                  file=sys.stderr)
            return 1
        print(f"OK: {args.validate_config} is a valid artnet-htp config")
        return 0

    cfg_path = Path(args.config)

    if args.init:
        if cfg_path.exists():
            print(f"{cfg_path} already exists; not overwriting.", file=sys.stderr)
            return 1
        example_src = Path(__file__).resolve().parent.parent.parent / "config.example.yaml"
        if example_src.exists():
            cfg_path.write_text(example_src.read_text())
            print(f"wrote starter config to {cfg_path}")
            return 0
        # Fallback: write minimal Pydantic default
        save_config(cfg_path, Config())
        print(f"wrote default config to {cfg_path}")
        return 0

    if not cfg_path.exists():
        print(
            f"No config file at {cfg_path}. Run with --init to create one, "
            "or pass --config <path>.",
            file=sys.stderr,
        )
        return 1

    controller = Controller(cfg_path)
    try:
        asyncio.run(_serve(controller))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
