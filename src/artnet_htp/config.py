"""Configuration models, YAML load/save, and conversion to MergerState types.

Schema is validated with Pydantic v2. Saves are atomic (write to temp + rename).
"""

from __future__ import annotations

import ipaddress
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .protocol import PORT_ADDRESS_MAX
from .state import OutputSpec, SourceSpec

SourceMode = Literal["htp", "priority"]


def _is_ipv4(s: str) -> str:
    try:
        ip = ipaddress.IPv4Address(s)
    except (ipaddress.AddressValueError, ValueError) as e:
        raise ValueError(f"invalid IPv4 address: {s!r}") from e
    return str(ip)


class SourceCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ip: str
    label: str = ""
    # "htp" sources merge by channel-wise max with all other htp sources.
    # "priority" sources, when any have non-zero data, override every htp source.
    # Among multiple active priority sources, the one with the HIGHEST `priority`
    # number wins exclusively.
    mode: SourceMode = "htp"
    priority: Annotated[int, Field(ge=0, le=999)] = 100  # higher = wins (only meaningful when mode="priority")
    # When false, packets from this source are dropped before merging — same
    # effect as deleting the row, but reversible via the UI On toggle.
    enabled: bool = True

    @field_validator("ip")
    @classmethod
    def _ip_valid(cls, v: str) -> str:
        return _is_ipv4(v)


class OutputCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ip: str
    label: str = ""
    broadcast: bool = False
    port: Annotated[int, Field(ge=1, le=65535)] = 6454
    # When false, the sender skips this destination — same effect as deleting,
    # but reversible via the UI On toggle.
    enabled: bool = True

    @field_validator("ip")
    @classmethod
    def _ip_valid(cls, v: str) -> str:
        return _is_ipv4(v)


class WebCfg(BaseModel):
    # Port 80 lets operators reach the UI at http://<pi>/ with no port
    # suffix — matches the FPP/etc. appliance UX. The systemd unit grants
    # CAP_NET_BIND_SERVICE so the non-root `artnet` user can bind a
    # privileged port. Override here for dev / custom setups.
    model_config = ConfigDict(extra="forbid")
    host: str = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 80


class NodeCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    short_name: Annotated[str, Field(max_length=17)] = "HTP Merger"
    long_name: Annotated[str, Field(max_length=63)] = "ArtNet HTP Merger (Pi)"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bind_ip: str = "0.0.0.0"
    send_rate_hz: Annotated[float, Field(ge=1.0, le=60.0)] = 44.0
    source_timeout_s: Annotated[float, Field(gt=0.0, le=60.0)] = 2.5
    send_keepalive_when_silent: bool = True
    auto_allow_unknown_sources: bool = False
    sources: list[SourceCfg] = Field(default_factory=list)
    outputs: list[OutputCfg] = Field(default_factory=list)
    universes: list[Annotated[int, Field(ge=0, le=PORT_ADDRESS_MAX)]] = Field(default_factory=list)
    web: WebCfg = Field(default_factory=WebCfg)
    node: NodeCfg = Field(default_factory=NodeCfg)

    @field_validator("bind_ip")
    @classmethod
    def _bind_ip_valid(cls, v: str) -> str:
        # 0.0.0.0 is allowed (means "all interfaces"); anything else must be a valid IPv4
        if v == "0.0.0.0":
            return v
        return _is_ipv4(v)

    @field_validator("universes")
    @classmethod
    def _universes_unique(cls, v: list[int]) -> list[int]:
        seen = set()
        out = []
        for u in v:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return sorted(out)

    @field_validator("sources")
    @classmethod
    def _sources_unique(cls, v: list[SourceCfg]) -> list[SourceCfg]:
        seen = set()
        for s in v:
            if s.ip in seen:
                raise ValueError(f"duplicate source IP: {s.ip}")
            seen.add(s.ip)
        return v

    @field_validator("outputs")
    @classmethod
    def _outputs_unique(cls, v: list[OutputCfg]) -> list[OutputCfg]:
        seen = set()
        for o in v:
            if o.ip in seen:
                raise ValueError(f"duplicate output IP: {o.ip}")
            seen.add(o.ip)
        return v

    # ----- conversions -----
    # Disabled sources/outputs are silently dropped during conversion to specs,
    # so the merger never sees them. This is simpler than threading an `enabled`
    # flag through state.py / sender.py — disabling a source is structurally
    # identical to deleting it for the runtime; we just persist the disabled
    # row in the YAML config so the UI can flip it back On.
    def to_source_specs(self) -> list[SourceSpec]:
        return [
            SourceSpec(ip=s.ip, label=s.label or s.ip, mode=s.mode, priority=s.priority)
            for s in self.sources
            if s.enabled
        ]

    def to_output_specs(self) -> list[OutputSpec]:
        return [
            OutputSpec(ip=o.ip, label=o.label or o.ip, broadcast=o.broadcast, port=o.port)
            for o in self.outputs
            if o.enabled
        ]


# --------------------------- load / save ---------------------------- #

def load_config(path: str | os.PathLike) -> Config:
    """Load and validate a YAML config file."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)


def save_config(path: str | os.PathLike, cfg: Config) -> None:
    """Atomically write a Config to YAML."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    # Use a temp file in the same directory so rename is atomic on the same FS.
    fd, tmp = tempfile.mkstemp(
        prefix=p.name + ".",
        suffix=".tmp",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
