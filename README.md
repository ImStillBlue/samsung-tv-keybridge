# Samsung TV KeyBridge

Bridge Samsung TV remote buttons to installed apps, with a supervised controller
intended to run as a Home Assistant app.

KeyBridge handles the buttons; your launcher and media apps stay on the TV.

**Status:** version 0.6.0 runs as a protected Home Assistant app on a Raspberry
Pi 5 (HAOS 18.3), with automatic re-root after TV reboots, configurable safe-key
mappings, discovery mode, remote auto-detection and a developer SDB bridge.
New installations default to disabled; see [DOCS.md](DOCS.md) to set it up.

## What it does

Configure verified remote buttons to installed applications. For example only:

| Button | Destination | Installed app ID |
| --- | --- | --- |
| Home | Theatre custom launcher | `Theatre001.Home` |
| Samsung TV Plus | Moonfin | `MoonfinApp.moonfin` |

Mappings have no defaults and must be explicitly configured in Home Assistant.
Destination apps must already be installed. The Theatre widget is a
separate project and is not included here.

- Discovers the paired remote by identity rather than a changing event-device number.
- Filters only configured keys whose destinations are installed and forwards other input.
- Offers continuous, non-grabbing discovery of app IDs and physical key events.
- Uses authenticated root and input sessions with fresh session credentials.
- Checks that the TV explicitly reports on before acquiring root.
- Reconnects only when configured, without opening an app over active playback.
- Releases interception on connection loss, normal shutdown or heartbeat expiry.

The controller does not install a permanent TV boot hook or rewrite firmware.
The installed launcher survives a reboot; live interception must be recreated.
Without a healthy mapping session, the intended fallback is Samsung's native
button handling.

## How it works

```text
Controller host                         Samsung TV
---------------                         ----------
Power-state check --------------------> TV status endpoint
Developer connection / archive route -> Temporary root helper
Authenticated root/input sessions <---- Managed input agent
Mapped key received ------------------> Launch installed app
```

The host retains the code and configuration. Each session stages temporary TV
helpers and owns their cleanup. A private host-side ledger tracks only owned
staging that could not be removed before the TV became unavailable.

## Compatibility and verification

The development target is a Samsung QA85QN90BAWXXY, firmware 1720, Tizen 6.5,
with a paired Bluetooth `Smart Control 2016` remote. A short physical trial of
both mappings succeeded on that setup. This does not establish support for
other models, firmware versions or remotes.

The implementation requires developer connectivity and a working compatible
archive-root route. A stock TV without those prerequisites cannot run this
controller. The root integration is pinned in `upstream.lock.json`.

Local checks pass 147 controller tests and 36 managed-agent tests, with Python
lint and type checks clean. Hardware reboot/failure timing and
extended unchanged-key acceptance remain unverified. The managed ABI round-trip
tests share production encoding and parsing, so they alone do not independently
prove native record offsets.

### Home Assistant and Raspberry Pi

The deployment target is a protected, Supervisor-managed app on an ARM64
Raspberry Pi 5, with its own persistent data and no Home Assistant Core changes.

The app manifest and Dockerfile live at this repository's root. Its build uses
native aarch64 or amd64 Python/OpenSSL. ARM64 uses checksum-pinned, container-local
QEMU for Samsung's x86-64 SDB; amd64 executes the private loader directly.
The app explicitly owns the SDB server, checks its wire
protocol, and stops it after controller cleanup. It never registers host binfmt.

For a local installation, copy the source folder (without `.git`, caches or
private files) into Home Assistant's `/local_apps/samsung-tv-keybridge`, reload
the app store, and install **Samsung TV KeyBridge** under Local apps. Source must
be available on the configured repository branch before Git-based installation
can use these changes; uncommitted local changes are not published automatically.

See [DOCS.md](DOCS.md) for configuration, Developer Mode, single-controller
cutover, automatic startup and recovery. Start with `enabled: false`.

## Source map

| Path | Responsibility |
| --- | --- |
| `controller/theatre/config.py`, `keys.py` | Explicit mappings, safe keys and configuration policy |
| `controller/theatre/main.py` | Mapping dispatch and session supervision |
| `controller/theatre/remote.py` | Authenticated input transport and heartbeat |
| `controller/theatre/{archive,bootstrap,state}.py` | Root lease, staging and cleanup |
| `controller/theatre/power.py` | Explicit-on power gate |
| `controller/theatre/ha_app.py` | Home Assistant options and owned SDB lifecycle |
| `controller/theatre/bridge.py` | Opt-in, allowlisted developer SDB relay independent of mappings |
| `config.yaml`, `Dockerfile`, `packaging/` | Protected aarch64/amd64 app and pinned build inputs |
| `payloads/remote/` | TV-side filtering, forwarding and watchdog |
| `controller/tests/`, `payloads/remote-tests/` | Regression tests |
| `scripts/build-payloads.js` | Verified upstream extraction and payload compilation |
| `scripts/dev-bridge.js` | PC helper for sharing the app-owned SDB server |
| `controller/uv.lock`, `upstream.lock.json` | Dependency and upstream pins |

The internal Python package remains `theatre`; KeyBridge is the public project
name. This repository is the authoritative source for future controller and
Home Assistant app work.

## Development

Requirements:

- Bun 1.4.2.
- Python within the declared `>=3.12,<3.15` range, uv and OpenSSL.
- For TV payload builds: a .NET SDK capable of targeting `netcoreapp3.1`.
  The established build uses SDK 6.0.428; host tests target .NET 6.

From the repository root:

```sh
# Download and verify only the pinned upstream source subset.
bun scripts/build-payloads.js --source-only

cd controller
uv sync --locked
uv run ruff check theatre tests
uv run basedpyright
uv run pytest -q
```

From the repository root, build TV payloads:

```sh
bun scripts/build-payloads.js
```

Set `DOTNET` to select an SDK executable. `UPSTREAM_ARCHIVE` can select a local
upstream archive; its checksum is still verified against the lock.

Run the managed host tests from the repository root:

```sh
DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 dotnet run \
  --project payloads/remote-tests/TheatreInputTests.csproj --configuration Release
```

These tests do not contact a TV or enable mappings.

## Configuration and privacy

Root acquisition, mapping and reconnection require explicit configuration.
Keep actual TV/host addresses, runtime credentials and cleanup state outside
source control. Session tokens are generated at runtime; widget signing keys
are not required by the daemon.

This source repository excludes household configuration, certificates, captures,
session history, launcher artwork and compiled widgets. Generated dependencies
and build outputs are ignored. Review files before publication; `.gitignore`
is not a secret scanner.

## Attribution and license

Built on the pinned `chris-ritsen/samsung-tv-root` integration. Adapted portions
retain source attribution. See [THIRD_PARTY.md](THIRD_PARTY.md).

Original KeyBridge code is licensed under MIT, Copyright (c) 2026 ImStillBlue.
See [LICENSE](LICENSE). Adapted upstream portions retain the Unlicense.
