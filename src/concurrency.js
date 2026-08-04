export const DEFAULT_CONCURRENCY = 8;

export async function runPool(items, concurrency, worker) {
  const queue = [...items];
  const workerCount = Math.min(concurrency, queue.length);
  async function run() {
    while (queue.length > 0) {
      const item = queue.shift();
      await worker(item);
    }
  }
  await Promise.all(Array.from({ length: workerCount }, run));
}
