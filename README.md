# ArtNet HTP Merger

A Raspberry Pi appliance that receives ArtNet from multiple sending computers,
performs per-channel **Highest Takes Priority** merge for each DMX universe, and
forwards the merged stream to one or more receiving IP addresses. Includes a
live web UI for configuration and monitoring, and advertises itself as an
ArtNet node so consoles auto-discover it.

## Quick start (development on Mac/Linux)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Run the merger against the example config
cp config.example.yaml config.yaml      # edit IPs/universes to match your setup
artnet-htp --config config.yaml
```

Open `http://localhost:8080` for the UI.

## Local end-to-end test (no Pi needed)

Run two synthetic sources at different levels, send their merged output to a
synthetic listener, and verify HTP behavior:

```bash
# Terminal 1: the merger (config has 127.0.0.1 in sources, 127.0.0.1:6455 in outputs)
artnet-htp --config tests/loopback.yaml

# Terminal 2: source A at level 100
python tools/synth.py --universe 0 --level 100 --rate 20 --dest 127.0.0.1

# Terminal 3: source B at level 200
python tools/synth.py --universe 0 --level 200 --rate 20 --dest 127.0.0.1

# Terminal 4: listener on the output port — should see 200s (HTP of 100 and 200)
python tools/synth.py --listen --port 6455 --universe 0 --channels 8
```

Kill source B; output drops to 100 within `source_timeout_s` (default 2.5s).

## Deploy to a Raspberry Pi

1. **Prep the Pi** (Raspberry Pi OS Bookworm Lite recommended):
   - Wired Ethernet on the lighting LAN
   - Static IP (`/etc/dhcpcd.conf` or NetworkManager)
   - SSH enabled

2. **Copy the source tree to the Pi**:
   ```bash
   rsync -av --exclude='.venv' --exclude='__pycache__' \
     ./ pi@<pi-ip>:/tmp/artnet-htp/
   ```

3. **Install**:
   ```bash
   ssh pi@<pi-ip>
   sudo bash /tmp/artnet-htp/deploy/install.sh --source /tmp/artnet-htp
   ```

   This creates an `artnet` system user, installs to `/opt/artnet-htp`, seeds
   `/etc/artnet-htp/config.yaml` from the example, and enables the systemd unit.

4. **Edit the config**:
   ```bash
   sudo nano /etc/artnet-htp/config.yaml
   sudo systemctl start artnet-htp
   sudo journalctl -u artnet-htp -f
   ```

5. **Open the UI** at `http://<pi-ip>:8080`.

## Architecture

- **Single Python process** with two threads + asyncio loop.
- `receiver.py` — asyncio `DatagramProtocol` on UDP/6454. Routes ArtDmx to state, ArtPoll to `poll.py`.
- `sender.py` — dedicated `threading.Thread` running a fixed-rate tick (default 44Hz). Lives outside asyncio for timing precision.
- `state.py` — locked state, HTP merge, per-(output, universe) sequence counters.
- `poll.py` — ArtPollReply on demand and every 2.5s (spec-required). Groups universes by Net/Sub into up to 4-port "binds".
- `config.py` — Pydantic-validated YAML.
- `web/app.py` — FastAPI: REST CRUD + WebSocket (2Hz status JSON + 10Hz binary DMX preview frames).
- `main.py` — Controller wiring everything together; uvicorn for the web layer.

See `tools/synth.py` for the synthetic ArtNet source/listener used in testing.
