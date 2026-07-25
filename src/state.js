import fs from "node:fs/promises";

export async function loadState(stateFilePath) {
  try {
    const raw = await fs.readFile(stateFilePath, "utf-8");
    const parsed = JSON.parse(raw);
    return parsed.entries ?? {};
  } catch (err) {
    if (err.code === "ENOENT") return {};
    throw err;
  }
}

export async function saveState(stateFilePath, entries) {
  const data = { version: 1, updatedAt: new Date().toISOString(), entries };
  await fs.writeFile(stateFilePath, JSON.stringify(data, null, 2), "utf-8");
}
