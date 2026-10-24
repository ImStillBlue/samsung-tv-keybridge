import { createConnection, createServer, isIPv4 } from "node:net";

const [host, sdb = "sdb", ...extra] = process.argv.slice(2);
if (!host || !isIPv4(host) || extra.length) {
  console.error("Usage: bun scripts/dev-bridge.js <home-assistant-ip> [sdb-path]");
  process.exit(1);
}

const stop = Bun.spawn([sdb, "kill-server"], { stdout: "inherit", stderr: "inherit" });
if (await stop.exited !== 0) {
  console.error("Could not stop the local SDB server; bridge not started.");
  process.exit(1);
}

const sockets = new Set();
const server = createServer((client) => {
  const upstream = createConnection({ host, port: 26098 });
  sockets.add(client);
  sockets.add(upstream);
  client.on("error", (error) => console.error(`Local SDB connection: ${error.message}`));
  upstream.on("error", (error) => console.error(`Developer bridge: ${error.message}`));
  client.on("close", () => { sockets.delete(client); upstream.destroy(); });
  upstream.on("close", () => { sockets.delete(upstream); client.destroy(); });
  client.pipe(upstream).pipe(client);
});
// kill-server returns before the old server releases its port; retry briefly.
let attempts = 0;
server.on("error", (error) => {
  if (error.code === "EADDRINUSE" && ++attempts < 20) {
    setTimeout(listen, 250);
    return;
  }
  console.error(error.code === "EADDRINUSE"
    ? "Cannot start: 127.0.0.1:26099 is busy. Stop its listener and try again."
    : `Cannot start developer bridge: ${error.message}`);
  process.exitCode = 1;
});
function listen() {
  server.listen({ host: "127.0.0.1", port: 26099, exclusive: true });
}
server.on("listening", () => {
  console.log(`Ready: 127.0.0.1:26099 -> ${host}:26098 (Ctrl+C to stop)`);
  console.log("sdb devices should list <TV-IP>:26101 already connected by the Pi; use that serial.");
});
listen();
process.on("SIGINT", () => {
  server.close();
  for (const socket of sockets) socket.destroy();
});
