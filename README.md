# Towel Bar Agent

```text
                       __..----..__
                  _.-'  :::::::::  `-._
               .-'  :::::::::::::::::  `-.
              /  :::::::::::::::::::::::  \
             / ::::::  .------. .------. ::\
            | ::::::  /  _     V     _  \ ::|
            | :::::: |  (_)         (_)  |:: |
            | ::::::  \      .-.      /  :: |
            | :::::::  `----'   `----' ::::: |
            | :::::::::     .---.      ::::: |
       _    | :::::::::    / ___ \     ::::: |    _
     _/ `---| :::::::::   | (___) |    ::::: |---' \_
    (___     \ :::::::::   \_____/    ::::: /     ___)
        `--.  \ ::::::::::::::::::::: /  .--'
            \  `:::::::::::::::::::::'  /
             | ::::::::::::::::::::::: |
             | ::::::::::::::::::::::: |
             | ::::::::::::::::::::::: |
             | ::::::::::::::::::::::: |
             '._:::::::::::::::::::::_.'
                `--.__::::::::__.--'
                      |  |  |  |
                    __|  |  |  |__
                   /_____/  \_____\

                    "Wanna get dry?"
```

`towelbar-agent` turns an Ethernet-connected Raspberry Pi with one Wi-Fi
adapter into a rotating controller for Amba TDHC/TDHCR heated towel bars.
The Wi-Fi adapter associates with each `emmesteel...` hotspot in turn, reads
state, applies queued commands, and moves on. MQTT remains reachable through
Ethernet and Home Assistant entities are created using MQTT discovery.

The HTTP protocol is deliberately configuration-driven because the exact
TDHC endpoints have not yet been captured. The same package includes a CLI
and an optional MCP server for discovering those endpoints from the Pi.

Discovered EmmeSteel controllers can use the built-in `emmesteel` driver. It
reads state and applies power, level, and temperature changes over the local
WebSocket, and controls the hardware countdown through `/timerSet`. All
controller sockets are pinned to the configured Wi-Fi interface, including
when Ethernet and the captive network use overlapping IPv4 subnets.

Python 3.10 or newer is supported. The project targets current Raspberry Pi OS
Trixie and also supports Raspberry Pi OS Bookworm.

## Architecture and constraints

```text
Home Assistant ── MQTT ── Ethernet ── Raspberry Pi
                                          │
                                  rotating wlan0
                                      ┌───┴───┐
                                   TDHC A   TDHC B
```

- Raspberry Pi OS Bookworm and Trixie use NetworkManager to rotate `wlan0`.
- Ethernet must carry the Pi's default route. The generated Wi-Fi profiles
  have `ipv4.never-default=yes`, preventing captive hotspots from hijacking it.
- Only one towel bar is immediately reachable at a time. Home Assistant
  commands remain queued until that controller's next turn.
- If two controllers use identical SSIDs, give their NetworkManager profiles
  distinct BSSIDs manually or use separate Wi-Fi adapters. Serial-numbered
  SSIDs normally avoid this.
- A protocol-free controller can be scanned and snapshotted but cannot yet be
  controlled.

## Install on Raspberry Pi OS

Use the current 64-bit Raspberry Pi OS Lite image when the hardware supports
it. Trixie and Bookworm are supported. Bullseye is not supported because it
uses `dhcpcd` instead of NetworkManager by default.

First configure Ethernet and verify that it remains reachable while Wi-Fi
changes. Clone/copy this repository to the Pi, then:

```bash
sudo bash scripts/install-raspberry-pi-os.sh --install-dependencies
sudoedit /etc/towelbar-agent/config.yaml
```

The installer verifies the Raspberry Pi OS release and NetworkManager service,
then creates a locked-down `towelbar` service account, a Python virtual
environment, a narrowly scoped NetworkManager polkit rule, and the systemd
unit. It does not start the service with the placeholder configuration.
It installs the `polkitd` package directly; the older `policykit-1`
transitional package is not available on Trixie.

Dependency installation is opt-in. Omit `--install-dependencies` when updating
an existing installation whose OS and Python dependencies are already present.
Without the flag, the installer does not run `apt-get`, upgrade pip, invoke a
Python build backend, or resolve Python dependencies. It verifies that the
required modules already exist, copies the application into the established
virtual environment, and refreshes its command launchers. A first-time install
must use `--install-dependencies`.

Configure an MQTT user in the Home Assistant MQTT broker, place those
credentials in `config.yaml`, and enable the service:

```bash
sudo systemctl enable --now towelbar-agent
sudo journalctl -u towelbar-agent -f
```

Home Assistant's MQTT integration should be configured through
**Settings → Devices & services**. The agent publishes native switch, number,
timestamp, and diagnostic entities through MQTT discovery; no Home Assistant
YAML or `.storage` edits are required.

For an EmmeSteel controller, configure:

```yaml
controllers:
  - id: guest_bath
    name: Guest Bathroom Towel Bar
    ssid: EMMESTEEL_24TS001267
    password: replace-with-controller-password
    driver: emmesteel
    base_url: http://192.168.1.1/
    default_timer_enabled: true
    default_timer_minutes: 120
    max_timer_minutes: 240
```

The agent exposes every configured EmmeSteel controller through MQTT Discovery
with power, power level, target temperature, current temperature, heating,
countdown, default-timer, and diagnostics entities. Commands are coalesced
while the Pi rotates between hotspots. A retained pending sensor turns on as
soon as Home Assistant queues a command and clears only after the controller
has been updated and read back.

Power is safe despite the controller's toggle-only protocol: the agent reads
the current state before sending `on-off`. Timer duration is expressed in
minutes and capped by `max_timer_minutes`. When default timer mode is enabled,
an Home Assistant power-on command arms the controller's hardware countdown in
the same poll. On every poll, an already-on towel bar with no active hardware
timer is armed with the configured default. This covers physical button
presses, missed transitions, agent restarts, and Home Assistant having no prior
state. Runtime timer settings and the last confirmed state of every controller
are written atomically to `/var/lib/towelbar-agent/runtime.json`. After a
restart or polling failure, a cached controller state is trusted for at most 30
minutes. Older state is discarded and published as unknown rather than being
used as current truth. Unconfirmed commands are not blindly replayed from disk.

## Deploy from another computer

Run from the project directory:

```bash
./scripts/deploy-to-pi.sh
```

The script rebuilds `towelbar-agent-raspberry-pi.zip` from the current workspace,
then prompts for the Pi host, SSH user, and whether dependencies should
be refreshed. `scp`, `ssh`, and remote `sudo` remain interactive, so required
passwords are entered directly into those programs and are not stored. The
script uploads the archive, runs the remote installer, restarts the agent, and
shows its systemd status. Reconnect a running stdio MCP client after deployment
so it launches the updated executable.

## Discovery workflow

Copy `config.example.yaml` and list every controller, initially without its
`protocol` section. On the Pi:

```bash
sudo -u towelbar /opt/towelbar-agent/venv/bin/towelbar-agent \
  --config /etc/towelbar-agent/config.yaml scan

sudo -u towelbar /opt/towelbar-agent/venv/bin/towelbar-agent \
  --config /etc/towelbar-agent/config.yaml snapshot primary_bath \
  --output /var/lib/towelbar-agent/captures
```

`snapshot` connects to the controller, derives its web-server address from the
DHCP gateway, saves the landing page and same-origin JavaScript/CSS, and writes
`snapshot.json` with candidate API paths.

Inspect the result:

```bash
rg -n -i 'fetch|xmlhttprequest|status|power|heat|timer|count' \
  /var/lib/towelbar-agent/captures
```

Probe a candidate with a read-only request first:

```bash
sudo -u towelbar /opt/towelbar-agent/venv/bin/towelbar-agent \
  --config /etc/towelbar-agent/config.yaml \
  request primary_bath GET /api/status
```

Explicit mutations are supported after identifying the request:

```bash
sudo -u towelbar /opt/towelbar-agent/venv/bin/towelbar-agent \
  --config /etc/towelbar-agent/config.yaml \
  request primary_bath POST /api/power --form power=1
```

The paths and field names above are examples, not claims about the TDHC API.
Promote verified requests into that controller's `protocol` section. Supported
encodings are `none`, `query`, `form`, and `json`. `{value}` is substituted in
configured values. Status responses are expected to be JSON and use dotted
paths such as `device.power`; numeric list components are supported.

## MCP experiment

Install with the `mcp` optional dependency (the Raspberry Pi OS installer does)
and configure an MCP client to launch:

```json
{
  "mcpServers": {
    "towelbar-discovery": {
      "command": "/opt/towelbar-agent/venv/bin/towelbar-mcp",
      "env": {
        "TOWELBAR_CONFIG": "/etc/towelbar-agent/config.yaml"
      }
    }
  }
}
```

The stdio server offers:

- `wifi_scan`
- `portal_snapshot`
- `http_request`

When launched through SSH with `sudo -u towelbar`, the server automatically
switches away from an inherited working directory that the service account
cannot read. Set `TOWELBAR_WORKDIR` to override its normal
`/var/lib/towelbar-agent` working directory.

`http_request` intentionally requires an explicit controller, method, and path.
An MCP client should ask before invoking a state-changing method. Run the MCP
server as the `towelbar` account so its NetworkManager permission and capture
directory match the daemon.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,mcp]'
.venv/bin/pytest
```

Never commit the real `/etc/towelbar-agent/config.yaml`; it contains hotspot and
MQTT credentials.
