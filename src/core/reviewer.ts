import { existsSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { assertResult, type Review, type TaskRecord, type TaskResult } from "./types.js";
import { atomicWrite, readJson } from "./state.js";

const now = () => new Date().toISOString();
const normalize = (path: string) => path.replaceAll("\\", "/").replace(/^\.\//, "");
function inScope(path: string, scopes: string[]): boolean {
  const p = normalize(path);
  return scopes.some(scope => { const s = normalize(scope).replace(/\/$/, ""); return p === s || p.startsWith(`${s}/`); });
}
export interface RepositorySnapshot { schema: "wp-repository-baseline-v1"; entries: Record<string,{status:string;sha256:string|null}> }
export function repositorySnapshot(repo: string): RepositorySnapshot {
  const root=resolve(repo),output=execFileSync("git",["status","--porcelain=v1","-z","--untracked-files=all"],{cwd:root,encoding:"utf8"});
  const parts=output.split("\0").filter(Boolean),entries:RepositorySnapshot["entries"]={};
  const record=(path:string,status:string)=>{const p=normalize(path),absolute=join(root,p);let sha256:string|null=null;try{if(statSync(absolute).isFile())sha256=createHash("sha256").update(readFileSync(absolute)).digest("hex");}catch{}entries[p]={status,sha256};};
  for(let i=0;i<parts.length;i++){const entry=parts[i],status=entry.slice(0,2),path=entry.slice(3);record(path,status);if((status[0]==="R"||status[0]==="C")&&parts[i+1])record(parts[++i],status);}
  return {schema:"wp-repository-baseline-v1",entries};
}
function taskDelta(repo:string,dir:string):string[]{
  const current=repositorySnapshot(repo).entries,baselinePath=join(dir,"baseline.json");
  const baseline=existsSync(baselinePath)?readJson<RepositorySnapshot>(baselinePath).entries:{};
  return [...new Set([...Object.keys(current),...Object.keys(baseline)].filter(path=>JSON.stringify(current[path]??null)!==JSON.stringify(baseline[path]??null)))].sort();
}
export function deterministicReview(repo: string, dir: string, task: TaskRecord): { review: Review; result?: TaskResult; actualFiles: string[] } {
  const findings: string[] = [], resultPath = join(dir, "result.json");
  let result: TaskResult | undefined;
  if (!existsSync(resultPath)) findings.push("result.json does not exist");
  else try { result = readJson<TaskResult>(resultPath); assertResult(result, task); } catch (error) { findings.push(error instanceof Error ? error.message : String(error)); }
  const actualFiles = taskDelta(resolve(repo),dir);
  const outOfScope = actualFiles.filter(path => !inScope(path, task.contract.write_scope));
  if (outOfScope.length) findings.push(`out-of-scope repository changes: ${outOfScope.join(", ")}`);
  if (result) {
    const claimed = [...new Set(result.changed_files.map(normalize))].sort();
    const unreported = actualFiles.filter(path => !claimed.includes(path));
    const nonexistentClaims = claimed.filter(path => !actualFiles.includes(path));
    if (unreported.length) findings.push(`unreported repository changes: ${unreported.join(", ")}`);
    if (nonexistentClaims.length) findings.push(`claimed files not present in Git changes: ${nonexistentClaims.join(", ")}`);
    const badClaims = claimed.filter(path => !inScope(path, task.contract.write_scope));
    if (badClaims.length) findings.push(`result claims out-of-scope files: ${badClaims.join(", ")}`);
    if (result.status === "failed") findings.push(`executor failed: ${result.summary}`);
    if (result.status === "success" && result.validation.some(v => (v.outcome || v.result) !== "passed")) findings.push("successful result contains non-passing validation evidence");
  }
  let verdict: Review["verdict"] = findings.length ? "RETRY" : "PASS";
  if (result?.status === "blocked") verdict = "BLOCKED";
  if (task.contract.read_only && actualFiles.length) { verdict = "RETRY"; findings.push("read-only task changed repository files"); }
  const summary = verdict === "PASS" ? "Result and actual Git changes passed deterministic repository review" : verdict === "BLOCKED" ? (typeof result?.blocker === "string" ? result.blocker : "Executor reported an external blocker") : "Deterministic review found retryable discrepancies";
  return { review: { schema:"wp-review-v1", task_id:task.contract.task_id, attempt:task.attempt, verdict, summary, findings, reviewed_at:now() }, result, actualFiles };
}
export function writeReview(dir: string, review: Review): string {
  const path = join(dir, "reviews", `attempt-${String(review.attempt).padStart(2,"0")}.json`);
  atomicWrite(path, review); return path;
}
