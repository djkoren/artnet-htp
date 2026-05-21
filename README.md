# ArtNet HTP Merger

A Raspberry Pi appliance that receives ArtNet from multiple sending computers
(Madrix, FPP, grandMA, etc.), merges them per-channel using **Highest Takes
Priority** (with optional per-source **Priority** override), and forwards the
merged stream to one or more receiving IP addresses. Includes a live web UI
for configuration and monitoring, and advertises itself as an ArtNet node so
consoles auto-discover it.

## Install (from a flashed SD card)

This is the normal path: download a pre-built `.img.xz`, flash an SD card,
boot, configure in the web UI.

### 1. Download the latest image

From the [Releases page](https://github.com/djkoren/artnet-htp/releases),
grab `artnet-htp-vX.Y.Z.img.xz`.

### 2. Flash with Raspberry Pi Imager (or balenaEtcher)

In **Pi Imager** → *Choose OS* → *Use custom* → select the `.img.xz` file →
choose your SD card → **Write**. Pi Imager handles the xz decompression
transparently.

### 3. (Optional) Pre-configure on the boot partition before ejecting

After flashing finishes, the SD card's `bootfs` partition is still mounted
on your Mac (or Windows/Linux). The image is **flash-and-go** — you don't
need to add anything for it to boot, SSH, or serve the web UI. The only
file worth dropping is your exported config so the unit comes up
pre-configured:

| File on `bootfs` | Effect |
|---|---|
| `artnet-htp-config.yaml` | Auto-import as the merger's config on first boot. **Export from a working merger via the UI** to skip reconfiguring every venue. |

### 4. Boot

Eject the SD card, slot it into the Pi, plug in **wired Ethernet** and
power. After ~45 seconds the merger is up and reachable at:

- **`http://artnet-htp.local:8080`** — works on any device on the same LAN
  with mDNS support (Mac, iOS, recent Windows, recent Linux).
- **`http://<pi-ip>:8080`** — works always. Find the IP via your router's
  DHCP table.

**SSH** is enabled by default with these credentials:

| Username | Password |
|---|---|
| `htp` | `artnet` |

```bash
ssh htp@artnet-htp.local        # or ssh htp@<pi-ip>
```

Same model as FPP (`fpp`/`falcon`). Identical defaults on every unit are
fine on a private show LAN; if a Pi will be reachable from a hostile
network, change the password (`sudo passwd htp`) or disable SSH
(`sudo systemctl disable --now ssh`).

### 5. Configure in the UI

Open the page, add your **Sources** (the IPs of computers sending you
ArtNet), **Outputs** (where you want the merged stream delivered),
**Universes**, and per-source **Mode** (HTP for additive merge, Priority for
exclusive override).

## Updating an installed Pi

The Pi runs offline at the venue, so updates work like FPP:

1. Open the merger's UI **before** taking the Pi to the venue.
2. Click **Export config (YAML)** → save the file.
3. Download the new release `.img.xz` from the Releases page.
4. Flash a fresh SD card with Pi Imager.
5. Drop the saved YAML onto the new card's boot partition as
   `artnet-htp-config.yaml`. (SSH is already enabled in the image — no
   extra files needed.)
6. Swap the new card into the Pi, power on. It boots with your old config
   already applied.

Old SD cards are your rollback path — keep them labeled with their version.

## What modes mean

For each source, you pick **HTP** or **Priority**:

- **HTP** sources merge channel-by-channel via max. Two HTP sources, one
  sending channel 1 at 100 and another at 200, give you 200 on channel 1.
  Classic ArtNet merging.
- **Priority** sources **override every HTP source** when they have any
  non-zero data. Use this for a controller that should take over the rig
  when it's playing (e.g. FPP running a sequence) and fall back to your
  HTP-merged sources when it goes to blackout. Among multiple Priority
  sources, the one with the highest `Priority` number wins exclusively.

## Build / develop from source

You don't need this path to use the merger — only if you're modifying the
code.

```bash
git clone https://github.com/djkoren/artnet-htp
cd artnet-htp
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest

# Run against the example config locally
cp config.example.yaml config.yaml      # edit IPs/universes
artnet-htp --config config.yaml
```

Open `http://localhost:8080`.

### Local end-to-end test (no Pi needed)

```bash
# Terminal 1: the merger pointed at loopback
artnet-htp --config config.loopback.yaml

# Terminal 2: synthetic source A
python tools/synth.py --universe 0 --level 100 --rate 20 --dest 127.0.0.1

# Terminal 3: synthetic source B
python tools/synth.py --universe 0 --level 200 --rate 20 --dest 127.0.0.1

# Terminal 4: listener on the merger's output port
python tools/synth.py --listen --port 6455 --universe 0 --channels 8
```

Kill source B; output drops to 100 within `source_timeout_s` (default 2.5s).

### Deploy from source to a running Pi (instead of flashing)

If you already have Raspberry Pi OS installed and want to run the merger
directly without our prebuilt image:

```bash
rsync -av --exclude='.venv' --exclude='__pycache__' \
  ./ <user>@<pi-ip>:/tmp/artnet-htp/
ssh -t <user>@<pi-ip> 'sudo bash /tmp/artnet-htp/deploy/install.sh --source /tmp/artnet-htp \
  && sudo systemctl restart artnet-htp'
```

## Building a new release image

Tag-push triggers the CI build:

```bash
git tag v0.3.0 -m "Release v0.3.0: <summary>"
git push origin v0.3.0
```

GitHub Actions then runs `pi-gen` against Bookworm Lite arm64, attaches the
resulting `.img.xz` to a new GitHub Release. Build time is ~30 minutes.

For ad-hoc builds without cutting a tag, use the manual trigger:

```bash
gh workflow run build-image.yml --ref main
```

The artifact uploads to the workflow run itself (no release).

## Architecture

- **Single Python process** with two threads + an asyncio loop.
- `receiver.py` — asyncio `DatagramProtocol` on UDP/6454. Routes ArtDmx to
  state, ArtPoll to `poll.py`.
- `sender.py` — dedicated `threading.Thread` running a fixed-rate tick
  (default 44Hz). Lives outside asyncio for timing precision.
- `state.py` — locked state, HTP and per-source Priority merge logic,
  per-(output, universe) sequence counters.
- `poll.py` — ArtPollReply on demand and every 2.5s (spec-required).
  Groups universes by Net/Sub into up to 4-port "binds".
- `config.py` — Pydantic-validated YAML.
- `web/app.py` — FastAPI: REST CRUD + WebSocket (2Hz status JSON + 10Hz
  binary DMX preview frames). Endpoints include `/api/version`,
  `/api/config/export`, `/api/config/import`.
- `main.py` — Controller wiring everything together; uvicorn for the web
  layer.

The image build adds:
- `image/pi-gen-config` — pi-gen build config.
- `image/stage-artnet-htp/` — custom pi-gen stage that pre-installs the
  merger into the image.
- `image/firstboot/` — first-boot service that auto-imports
  `artnet-htp-config.yaml` from the boot partition.
- `.github/workflows/build-image.yml` — tag-triggered CI build.

See `tools/synth.py` for the synthetic ArtNet source/listener used in
testing.
