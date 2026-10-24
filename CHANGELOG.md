# Changelog

## 0.6.0

- Share the app-owned SDB server through an allowlisted bridge on port 26098,
  avoiding the TV's verified single-host-connection limit.
- Start the server for mappings or developer access; start the bridge only after
  server readiness and stop it before server shutdown.
- Add a dependency-free Bun PC helper and document shared-server commands,
  serial selection and commands that interrupt mappings.

## 0.5.0

- Add an opt-in, IPv4-allowlisted developer SDB relay through Home Assistant.
- Run developer access independently of mappings/discovery, with bounded
  connections and shared app shutdown.
- Document developer bridge setup, SDB security and the SSH tunnel alternative;
  concurrent SDB access while mappings run remains unverified on a real TV.

## 0.4.0

- Require explicit safe-key mappings to installed applications.
- Add continuous discovery logs and Samsung Bluetooth remote auto-detection.
- Support native amd64 hosts alongside aarch64, without host binfmt.
- License original KeyBridge code under MIT; retain upstream Unlicense notices.

## 0.3.2

- Restart cleanly while the previous SDB server port is in TIME_WAIT.
- Log the sockets holding the SDB port if startup still finds it occupied.

## 0.3.1

- Stop logging every routine TV status request; HTTP warnings still appear.

## 0.3.0

- Add Home Assistant options and an ARM64 app build.
- Keep new installations disabled until TV and callback addresses are configured.
- Own the Samsung SDB server explicitly, including readiness and ordered shutdown.
- Reuse the standby-safe controller reconnect loop and persistent cleanup ledger.
- Preserve the fixed Home/Theatre and TV Plus/Moonfin routes.
