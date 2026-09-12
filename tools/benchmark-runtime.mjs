import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";

import { createCipher } from "../src/crypto.js";
import { createIgnoreMatcher } from "../src/ignore.js";
import { listLocalFiles } from "../src/localFiles.js";
import { planSync } from "../src/sync.js";

const args = new Map();
for (let index = 2; index < process.argv.length; index += 1) {
  if (process.argv[index].startsWith("--")) args.set(process.argv[index], process.argv[index + 1]);
}

const profile = args.get("--profile") ?? "windows";
const defaults = profile === "android-pseudo"
  ? { files: 1000, bytes: 8192, repeats: 5 }
  : { files: 2000, bytes: 16384, repeats: 5 };
const fileCount = Number(args.get("--files") ?? defaults.files);
const contentBytes = Number(args.get("--bytes") ?? defaults.bytes);
const repeats = Number(args.get("--repeats") ?? defaults.repeats);
const root = await fs.mkdtemp(path.join(os.tmpdir(), "r2-sync-bench-node-"));

function median(values) {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.floor(sorted.length / 2)];
}

async function timed(fn) {
  const started = performance.now();
  const value = await fn();
  return { value, ms: performance.now() - started };
}

async function makeDataset() {
  const payload = Buffer.alloc(contentBytes);
  for (let index = 0; index < payload.length; index += 1) payload[index] = (index * 31 + 17) & 0xff;
  for (let index = 0; index < fileCount; index += 1) {
    const rel = `dir-${String(index % 50).padStart(2, "0")}/note-${String(index).padStart(5, "0")}.md`;
    const target = path.join(root, ...rel.split("/"));
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, payload);
  }
}

async function main() {
  await fs.rm(root, { recursive: true, force: true });
  await fs.mkdir(root, { recursive: true });
  await makeDataset();

  const cipher = await createCipher("benchmark-password");
  const matcher = createIgnoreMatcher([], { vaultPath: root });
  const scanSamples = [];
  let localFiles;
  for (let index = 0; index < repeats; index += 1) {
    const sample = await timed(() => listLocalFiles(root, matcher));
    scanSamples.push(sample.ms);
    localFiles = sample.value;
  }

  const paths = [...localFiles.keys()];
  const encryptedPaths = [];
  for (const relPath of paths) encryptedPaths.push(await cipher.encryptPath(relPath));
  const remoteItems = encryptedPaths.map((key, index) => {
    const fileIndex = Number(paths[index].match(/note-(\d+)\.md$/)[1]);
    return { key, etag: `etag-${fileIndex}` };
  });
  const prevEntries = {};
  for (const [relPath, info] of localFiles) {
    const index = Number(relPath.match(/note-(\d+)\.md$/)[1]);
    prevEntries[relPath] = {
      localMtimeMs: info.mtimeMs,
      localSize: info.size,
      remoteETag: `etag-${index}`,
      localContentHash: "benchmark-hash",
    };
  }

  const planSamples = [];
  let planned;
  for (let index = 0; index < repeats; index += 1) {
    const sample = await timed(() => planSync({
      vaultPath: root,
      localFiles,
      remoteItems,
      prevEntries,
      ignoreMatcher: matcher,
      cipher,
      r2Prefix: "",
      r2: { getObject: async () => { throw new Error("not expected in noop benchmark"); } },
    }));
    planSamples.push(sample.ms);
    planned = sample.value;
  }

  const content = Buffer.alloc(contentBytes);
  const cryptoSamples = [];
  let checksum = 0;
  for (let index = 0; index < repeats; index += 1) {
    const sample = await timed(async () => {
      let total = 0;
      for (let item = 0; item < Math.min(fileCount, 256); item += 1) {
        const encrypted = await cipher.encryptContent(content);
        const clear = await cipher.decryptContent(encrypted);
        total += clear.length + encrypted.length;
      }
      return total;
    });
    cryptoSamples.push(sample.ms);
    checksum += sample.value;
  }

  console.log(JSON.stringify({
    runtime: "node",
    profile,
    node: process.version,
    platform: `${process.platform}-${process.arch}`,
    dataset: { files: fileCount, bytesPerFile: contentBytes, repeats },
    benchmarks: {
      scanLocalFiles: { medianMs: median(scanSamples), samplesMs: scanSamples, items: fileCount },
      decodeAndPlanNoop: { medianMs: median(planSamples), samplesMs: planSamples, items: fileCount, noop: planned.filter((item) => item.type === "NOOP").length },
      encryptDecryptContent: { medianMs: median(cryptoSamples), samplesMs: cryptoSamples, items: Math.min(fileCount, 256), bytesPerSample: contentBytes * Math.min(fileCount, 256), checksum },
    },
  }));
}

try {
  await main();
} finally {
  await fs.rm(root, { recursive: true, force: true });
}
