# Samsung TV KeyBridge

KeyBridge restores a temporary root session and configured button mappings whenever a
compatible TV is awake. It does not wake the TV or install a persistent TV boot hook.

## Before enabling

1. Install the destination applications you want to launch on the TV.
2. Enable the TV's Developer Mode and set its host IP to your Home Assistant
   machine's LAN IPv4 address. Follow the TV's restart prompt.
3. Stop any other KeyBridge controller before enabling this app. Only one host
   may own the remote.
4. Keep app protection enabled. Host networking is used for authenticated
   callbacks; no Docker socket, host filesystem, hardware device or extra
   capability is required.

## Options

| Option | Meaning |
| --- | --- |
| `enabled` | Starts the controller only when true. Default false leaves mappings off; the developer bridge is separately opt-in. |
| `tv_host` | TV LAN IPv4 address. Use a DHCP reservation. |
| `callback_host` | Home Assistant LAN IPv4 address reachable from the TV. |
| `reconnect_delay` | Seconds between attempts while TV is off/unreachable or a session is lost; 5-60, default 15. |
| `discover` | Default false. True observes continuously and reconnects, without grabbing input or requiring mappings. |
| `developer_bridge` | Default false. Relays allowlisted developer connections on `callback_host:26098` to the app-owned SDB server at `127.0.0.1:26099`, independently of `enabled` and `discover`. Both hosts must be valid unicast IPv4 addresses. |
| `developer_clients` | Default empty. List of trusted client IPv4 addresses; at least one is required when the bridge is on. Unspecified, multicast and broadcast addresses are rejected. |
| `remote_name` | Optional exact device name to disambiguate remote selection. Omit for auto-detection. |
| `mappings` | Explicit list of safe `key` names and installed `app_id` destinations. Default empty; required when enabled outside discovery. |

## Discovery and setup

Start with your two IP addresses, `enabled: true`, `discover: true`, and
`mappings: []`. Restart the app, then read its logs:

- `remote.selected device=... product=... node=...` identifies the selected remote
  in discovery and mapping sessions.
- `discover.app id=... name=...` lists each registered application.
- Press remote buttons: each key-down logs `discover.key code=... name=...
  mappable=...`. Input is never grabbed during discovery.

Only hardware-verified names `home` (63) and `tv_plus` (560) are currently
mappable. Unknown buttons are logged but cannot be configured. Raw numeric key
codes are never accepted in user configuration. New safe names are added only
after their code is verified using discovery; no Netflix or Prime codes are assumed.

Auto-detection selects exactly one Samsung Bluetooth input device (bus 0005,
vendor 04e8) with EV_SYN, EV_KEY, and all active configured keys. Product IDs and
names may vary. The forwarded virtual device is excluded. Zero or multiple
candidates stop the session; set `remote_name` to the exact logged/device name
to resolve ambiguity. It does not bypass bus, vendor, or capability checks.

Copy your chosen registered app IDs into mappings, set `discover: false`, save,
and restart. Example only, for a TV with Theatre and Moonfin installed:

```yaml
enabled: true
tv_host: "192.0.2.10"
callback_host: "192.0.2.20"
reconnect_delay: 15
discover: false
mappings:
  - key: home
    app_id: Theatre001.Home
  - key: tv_plus
    app_id: MoonfinApp.moonfin
```

These destinations are not defaults. IDs must be at most 128 characters,
with dot-separated nonempty ASCII letters, digits, underscores or hyphens.
Duplicate keys are rejected. Missing apps log `mapping.skipped` and their keys
keep native handling; if all destinations are missing the session stops before
interception.

After saving options, restart the app. Enable **Start on boot** and **Watchdog**
in Home Assistant for unattended operation.

`sdb.ready` confirms the local SDK server. `session.ready mode=map` confirms the
authenticated TV input session. `route.launch` confirms a mapped button launched
its registered destination:

- The configured key -> its installed destination.

Other remote keys are forwarded on the TV. Reconnecting does not open an app or
replace the current picture. The app polls the TV's read-only power endpoint
before root acquisition; off/unreachable responses never authorize acquisition.

## Developer access

Samsung Developer Mode accepts SDB on TCP 26101 only from its configured host
IP, which must remain the Home Assistant/Pi LAN IP. The TV's SDB daemon accepts
only one host connection: a second connection is dropped while KeyBridge holds
its own. Developer access therefore shares the app's existing SDB server rather
than opening another connection to the TV.

To relay development traffic through the app, configure the TV and Home Assistant
LAN addresses and explicitly allow your PC's IP:

```yaml
enabled: true
tv_host: "192.0.2.10"
callback_host: "192.0.2.20"
developer_bridge: true
developer_clients:
  - "192.0.2.20"
```

Replace the example TV and Home Assistant addresses with your own. Save and restart
the app, retaining your configured mappings when `enabled: true`. The bridge
listens on `callback_host:26098` only after `sdb.ready`, forwarding to the owned
server at `127.0.0.1:26099`. It stops before that server stops. With `enabled: false`,
only the server and bridge run, without the controller or mappings. In that case,
if the TV is not listed yet, use `sdb connect <TV-IP>:26101` through the shared
server to connect this TV. The bridge allows at most four simultaneous relayed
connections and times out server connection attempts after five seconds. Binding
failures and server exits stop the app visibly.

On your PC, install Bun and run the helper from this repository:

```sh
bun scripts/dev-bridge.js <HA-IP>
# Or specify your SDK executable:
bun scripts/dev-bridge.js <HA-IP> /path/to/sdb
```

The helper stops your PC's local SDB server once, then binds `127.0.0.1:26099`
and forwards connections to Home Assistant port 26098 until Ctrl+C. It refuses
to start if that local port remains busy. Leave it running; in another terminal,
use normal SDB commands. `sdb devices` should list the TV already connected by
the Pi when the controller is running. Use the TV's serial, `<TV-IP>:26101`,
not the Home Assistant address:

```sh
sdb devices
sdb -s 192.0.2.10:26101 capability
sdb -s 192.0.2.10:26101 install /path/to/widget.wgt
sdb -s 192.0.2.10:26101 shell
```

Replace `192.0.2.10` with your TV's IP.

**While bridged, do NOT run `sdb kill-server`, `sdb disconnect`, or
`sdb connect` to another target.** These commands act on the Pi's shared server
and interrupt mappings. The app recovers automatically. The helper's initial
`kill-server` happens before bridging and affects only your local server.

**Security:** SDB is unauthenticated. Anyone allowlisted can install apps and
obtain root routes. Keep the bridge off when not developing, and allowlist only
trusted fixed IPs or DHCP reservations. The listener binds only the configured
`callback_host`; it does not use the power gate, root lease or input agent.

## Recovery and stopping

Stop the app to release interception. Its controller cleans up before its owned
SDK server stops. A lost connection or heartbeat releases TV input independently.
If the TV loses power before cleanup, only this app's owned temporary paths are
recorded under `/data/state` and cleaned on the next powered-on connection.

Do not delete app data to repair a connection: that discards the cleanup ledger.
Check the two IP settings and Developer Mode host IP first. Unexpected protocol,
missing-app and compatibility failures remain visible in the app log.

The container supports aarch64 and amd64. Python/OpenSSL are native on both.
ARM64 explicitly invokes the isolated x86-64 Samsung SDK through container-local
user-mode QEMU; amd64 uses its private loader directly. Neither installs host binfmt.
Compatibility is limited to the tested firmware/remote described in README.md.
Physical TV reboot, standby wake and unchanged-key acceptance must be observed
on your own device; unit tests do not establish those hardware results.
