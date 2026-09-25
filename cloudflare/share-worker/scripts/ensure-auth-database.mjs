import { spawnSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const databaseName = "cheatvision-share-auth";
const workerDir = fileURLToPath(new URL("..", import.meta.url));
const configPath = fileURLToPath(new URL("../wrangler.jsonc", import.meta.url));

function runWrangler(args) {
  const result = spawnSync("wrangler", ["d1", ...args], {
    cwd: workerDir,
    encoding: "utf8",
    shell: process.platform === "win32",
  });
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || "Wrangler D1 command failed.").trim());
  }
  return result.stdout;
}

function findDatabase(value) {
  if (Array.isArray(value)) {
    for (const entry of value) {
      const found = findDatabase(entry);
      if (found) return found;
    }
    return null;
  }
  if (!value || typeof value !== "object") return null;
  const id = value.uuid || value.database_id || value.databaseId || value.id;
  const name = value.name || value.database_name || value.databaseName;
  if (name === databaseName && typeof id === "string") return id;
  for (const child of Object.values(value)) {
    const found = findDatabase(child);
    if (found) return found;
  }
  return null;
}

function listDatabaseId() {
  const output = runWrangler(["list", "--json"]);
  return findDatabase(JSON.parse(output));
}

let databaseId = listDatabaseId();
if (!databaseId) {
  runWrangler(["create", databaseName, "--location", "enam"]);
  databaseId = listDatabaseId();
}
if (!databaseId) throw new Error(`Could not find D1 database ${databaseName} after provisioning.`);

const config = JSON.parse(await readFile(configPath, "utf8"));
config.d1_databases ||= [];
let binding = config.d1_databases.find(item => item.binding === "AUTH_DB");
if (!binding) {
  binding = { binding: "AUTH_DB", database_name: databaseName, database_id: databaseId, migrations_dir: "migrations" };
  config.d1_databases.push(binding);
} else {
  binding.database_name = databaseName;
  binding.database_id = databaseId;
  binding.migrations_dir = "migrations";
}
await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`, "utf8");
process.stdout.write(`Configured AUTH_DB binding for ${databaseName}.\n`);
