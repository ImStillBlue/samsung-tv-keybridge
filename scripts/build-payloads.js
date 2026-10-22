import { createHash } from "node:crypto";
import { cp, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const cache = join(root, "controller", ".cache");
const upstream = join(cache, "upstream");
const output = join(root, "controller", "build", "payloads");
const lock = JSON.parse(await readFile(join(root, "upstream.lock.json"), "utf8"));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const localArchive = process.env.UPSTREAM_ARCHIVE;
let archiveBytes;
if (localArchive) {
  archiveBytes = await readFile(localArchive);
} else {
  const response = await fetch(lock.archiveUrl);
  if (!response.ok) throw new Error(`Upstream download failed: ${response.status}`);
  archiveBytes = new Uint8Array(await response.arrayBuffer());
}
if (sha256(archiveBytes) !== lock.archiveSha256) {
  throw new Error("Pinned upstream archive checksum mismatch");
}
const files = await new Bun.Archive(archiveBytes).files();
await mkdir(upstream, { recursive: true });
// Extract only the verified integration/build subset, not arbitrary archive paths.
for (const [path, digest] of Object.entries(lock.files)) {
  const entry = [...files.entries()].find(([name]) => name.endsWith(`/${path}`));
  if (!entry) throw new Error(`Missing pinned source: ${path}`);
  const bytes = new Uint8Array(await entry[1].arrayBuffer());
  if (sha256(bytes) !== digest) throw new Error(`Pinned source checksum mismatch: ${path}`);
  const destination = join(upstream, path);
  await mkdir(dirname(destination), { recursive: true });
  await writeFile(destination, bytes);
}
console.log(`Verified upstream ${lock.commit} (${lock.archiveSha256})`);
if (process.argv.includes("--source-only")) process.exit(0);

const dotnet = process.env.DOTNET ?? "dotnet";
const environment = { ...process.env, DOTNET_SYSTEM_GLOBALIZATION_INVARIANT: "1" };
await mkdir(output, { recursive: true });
async function build(project) {
  const restore = Bun.spawn([
    dotnet, "restore", project, "-p:TargetFramework=netcoreapp3.1",
  ], { cwd: dirname(project), env: environment, stdout: "inherit", stderr: "inherit" });
  const restoreCode = await restore.exited;
  if (restoreCode !== 0) throw new Error(`Payload restore failed (${restoreCode}): ${project}`);
  const child = Bun.spawn([
    dotnet, "build", project, "--configuration", "Release", "--no-restore",
    "-p:TargetFramework=netcoreapp3.1", "--output", output,
  ], { cwd: dirname(project), env: environment, stdout: "inherit", stderr: "inherit" });
  const code = await child.exited;
  if (code !== 0) throw new Error(`Payload build failed (${code}): ${project}`);
}
for (const name of ["SamsungTvArchiveRoot", "SamsungTvRootAgent"]) {
  await build(join(upstream, "payloads", "common", `${name}.csproj`));
}
// Build a snapshot so the payload owner's obj/bin and source remain untouched.
const remote = join(cache, "remote");
await mkdir(remote, { recursive: true });
for (const file of await readdir(join(root, "payloads", "remote"))) {
  if (file.endsWith(".cs") || file.endsWith(".csproj")) {
    await cp(join(root, "payloads", "remote", file), join(remote, file));
  }
}
await build(join(remote, "TheatreInputAgent.csproj"));
for (const name of ["SamsungTvArchiveRoot", "SamsungTvRootAgent", "TheatreInputAgent"]) {
  await writeFile(join(output, `${name}.runtimeconfig.json`), JSON.stringify({
    runtimeOptions: {
      tfm: "netcoreapp3.1", rollForward: "LatestPatch",
      framework: { name: "Microsoft.NETCore.App", version: "3.1.0" },
    },
  }) + "\n");
}
console.log(`Built .NET 3.1 archive, root and input payloads: ${output}`);
