import { createHash } from "node:crypto";
import { appendFileSync, closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";

export function stateRoot(): string {
  return resolve(process.env.WP_STATE_DIR || join(homedir(), ".agents", "state", "wp"));
}
export function digest(value: unknown): string {
  return createHash("sha256").update(typeof value === "string" ? value : JSON.stringify(value)).digest("hex");
}
export function repoId(repo: string): string {
  return `repo-${digest(resolve(repo)).slice(0, 16)}`;
}
export function safeJoin(root: string, ...parts: string[]): string {
  const base = resolve(root), target = resolve(base, ...parts);
  if (target !== base && !target.startsWith(base + sep)) throw new Error("path escapes WP state root");
  return target;
}
export function taskDir(repo: string, taskId: string, root = stateRoot()): string {
  return safeJoin(root, "repos", repoId(repo), "tasks", taskId);
}
export function atomicWrite(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  const temp = `${path}.${process.pid}.${Date.now()}.tmp`;
  const fd = openSync(temp, "w", 0o600);
  try { writeFileSync(fd, typeof value === "string" ? value : JSON.stringify(value, null, 2) + "\n"); fsyncSync(fd); }
  finally { closeSync(fd); }
  let lastError: unknown;
for (let attempt = 0; attempt < 10; attempt++) {
  try { renameSync(temp, path); return; }
  catch (error) {
    lastError = error;
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "EPERM" && code !== "EBUSY" && code !== "EACCES") throw error;
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25 * (attempt + 1));
  }
}
throw lastError;
}
export function readJson<T>(path: string): T { return JSON.parse(readFileSync(path, "utf8")) as T; }
export function appendEvent(path: string, event: Record<string, unknown>): void {
  mkdirSync(dirname(path), { recursive: true });
  const fd = openSync(path, "a", 0o600);
  try { appendFileSync(fd, JSON.stringify({ at: new Date().toISOString(), ...event }) + "\n"); fsyncSync(fd); }
  finally { closeSync(fd); }
}
export class WriterLock {
  constructor(readonly path: string) {}
  acquire(): void {
    mkdirSync(dirname(this.path), { recursive: true });
    try { writeFileSync(this.path, JSON.stringify({ pid: process.pid, acquired_at: new Date().toISOString() }), { flag: "wx", mode: 0o600 }); }
    catch (error) {
      if (!existsSync(this.path)) throw error;
      const owner = readJson<{pid:number}>(this.path);
      try { process.kill(owner.pid, 0); throw new Error(`WP state is locked by live pid ${owner.pid}`); }
      catch (probe) { if (probe instanceof Error && probe.message.startsWith("WP state")) throw probe; rmSync(this.path, { force: true }); this.acquire(); }
    }
  }
  release(): void { rmSync(this.path, { force: true }); }
}
export function redact(value: string): string {
  return value.replace(/\b(token|password|secret|api[_-]?key)\s*[:=]\s*\S+/gi, "$1=[REDACTED]")
    .replace(/(?:sk|ghp|pat|Bearer)[-_ ][A-Za-z0-9._-]{12,}/g, "[REDACTED]");
}
export function fileSummary(path: string): { size: number; sha256: string } {
  return { size: statSync(path).size, sha256: digest(readFileSync(path)) };
}
