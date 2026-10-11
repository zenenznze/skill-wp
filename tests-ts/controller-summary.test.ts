import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { acceptTask, createTask, reviewTask, taskStatus } from "../src/core/controller.js";
import { atomicWrite, readJson, taskDir } from "../src/core/state.js";
import { supervise } from "../src/core/supervisor.js";
import { HerdrClient } from "../src/herdr/client.js";
import type { TaskRecord } from "../src/core/types.js";

function fixture(max_attempts=1) {
 const root=mkdtempSync(join(tmpdir(),"wp-summary-")),repo=join(root,"repo"),id="20261010-summary";
 execFileSync("git",["init","-q",repo]);
 createTask({repo,task_id:id,single_outcome:"Prepare review package r2",deliverables:["review package"],write_scope:[],read_only:true,acceptance:["local check passes; approval remains pending"],resume_boundary:"Read version-bound business evidence",max_attempts},root);
 const dir=taskDir(repo,id,root),task=readJson<TaskRecord>(join(dir,"task.json"));
 const result={task_id:id,status:"success",summary:"r2 review package; human approval pending",changed_files:[],validation:[{command:"local check",exit_code:0,result:"PASS local checks"}],handoff_path:join(dir,"HANDOFF.md")};
 const handoff=readFileSync(join(dir,"HANDOFF.md"),"utf8").split("<!-- WP:CONTROLLER:START -->")[0].trimEnd().replace("status: planned","status: success").replace("status: pending","status: success")+"\nExecutor reports bounded success r2, not final approval.\n";
 atomicWrite(join(dir,"HANDOFF.md"),handoff);
 return {root,repo,id,dir,task,result,handoff};
}
function cli(f:ReturnType<typeof fixture>,command:string) {
 return spawnSync(process.execPath,[resolve("bin/wpctl.mjs"),command,"--repo",f.repo,"--task-id",f.id],{env:{...process.env,WP_STATE_DIR:f.root},encoding:"utf8"});
}
test("legacy failed state recovers exact attempt/review evidence through CLI without writes",()=>{
 const f=fixture();try {
  const ad=join(f.dir,"attempts/attempt-01");
  atomicWrite(join(ad,"result.json"),f.result);
  atomicWrite(join(ad,"review.json"),{schema:"wp-review-v1",task_id:f.id,attempt:1,verdict:"RETRY",summary:"Deterministic review found retryable discrepancies",findings:["successful result contains non-passing validation evidence"],reviewed_at:"2026-10-10T00:00:00Z"});
  f.task.status="failed";delete f.task.status_reason;atomicWrite(join(f.dir,"task.json"),f.task);rmSync(join(f.dir,"summary.json"));
  const before=readFileSync(join(f.dir,"HANDOFF.md"),"utf8"),r=cli(f,"status");
  assert.equal(r.status,0,r.stderr);
  const d=JSON.parse(r.stdout).details;
  assert.equal(d.summary.status,"failed");assert.equal(d.summary.scope,"wp_contract");
  assert.equal(d.summary.attempt_result.status,"success");assert.equal(d.summary.attempt_result.path,join(ad,"result.json"));
  assert.equal(d.summary.review.verdict,"RETRY");assert.equal(d.summary.review.path,join(ad,"review.json"));
  assert.match(d.summary.reason,/non-passing validation/);assert.match(d.summary.warnings.join(" "),/HANDOFF/);
  assert.equal(d.summary.human_approval,"not_established_by_wp");assert.equal(d.summary.business_delivery,"tracked_externally");
  assert.equal(readFileSync(join(f.dir,"HANDOFF.md"),"utf8"),before);assert.equal(existsSync(join(f.dir,"summary.json")),false);
  const checked=cli(f,"check");assert.equal(checked.status,0,checked.stderr);assert.equal(JSON.parse(checked.stdout).details.summary.review.verdict,"RETRY");
  const resumed=cli(f,"resume");assert.equal(resumed.status,0,resumed.stderr);
  assert.equal(JSON.parse(resumed.stdout).ok,false);assert.equal(JSON.parse(resumed.stdout).details.summary.status,"failed");
  assert.equal(existsSync(join(f.dir,"supervisor-process.json")),false);
  assert.equal(readFileSync(join(f.dir,"HANDOFF.md"),"utf8"),before);assert.equal(existsSync(join(f.dir,"summary.json")),false);
 } finally {rmSync(f.root,{recursive:true,force:true});}
});
test("review synchronizes root state while preserving executor evidence and validation rejection",()=>{
 const f=fixture();try {
  atomicWrite(join(f.dir,"result.json"),f.result);
  const r=reviewTask(f.repo,f.id,f.root);assert.equal(r.details?.verdict,"RETRY");
  assert.ok((r.details?.findings as string[]).includes("invalid validation outcome"));
  const summary=readJson<any>(join(f.dir,"summary.json"));
  assert.equal(summary.status,"failed");assert.equal(summary.attempt_result.status,"success");
  const text=readFileSync(join(f.dir,"HANDOFF.md"),"utf8");assert.match(text,/status: failed/);assert.match(text,/WP Controller State/);assert.match(text,/invalid validation outcome/);
  assert.equal(readFileSync(join(f.dir,"attempts/attempt-01/HANDOFF.md"),"utf8"),f.handoff);
  assert.deepEqual(readJson(join(f.dir,"attempts/attempt-01/result.json")),f.result);
  atomicWrite(join(f.dir,"result.json"),{...f.result,validation:[{command:"local check",outcome:"passed"}]});
  const again=reviewTask(f.repo,f.id,f.root);assert.equal(again.ok,false);assert.equal(taskStatus(f.repo,f.id,f.root).details?.status,"failed");
  assert.deepEqual(readJson(join(f.dir,"attempts/attempt-01/result.json")),f.result);
  assert.equal(readJson<any>(join(f.dir,"summary.json")).review.verdict,"RETRY");
 } finally {rmSync(f.root,{recursive:true,force:true});}
});
test("supervisor failure, external blocker and timeout persist consistent summaries without agents",async()=>{
 const probe=HerdrClient.prototype.probe,read=HerdrClient.prototype.read,wait=HerdrClient.prototype.wait;
 HerdrClient.prototype.probe=async()=>({reachable:true});HerdrClient.prototype.read=async()=>({});HerdrClient.prototype.wait=async()=>({});
 try {for(const mode of ["retry","blocked","timeout"]){
  const f=fixture();try {
   atomicWrite(join(f.dir,"resources.json"),{schema:"wp-resources-v1",task_id:f.id,tab_id:"fixture:t1",agent_id:"fixture",adopted:true,created_by_wp:false});
   if(mode!=="timeout")atomicWrite(join(f.dir,"result.json"),mode==="retry"?f.result:{...f.result,status:"blocked",blocker:"External input unavailable",validation:[]});
   const r=await supervise(f.repo,f.dir,{waitTimeoutMs:0});
   assert.equal(r.details?.status,mode==="retry"?"failed":"blocked");
   const summary=readJson<any>(join(f.dir,"summary.json"));assert.equal(summary.status,r.details?.status);
   assert.match(readFileSync(join(f.dir,"HANDOFF.md"),"utf8"),new RegExp("status: "+summary.status));
   if(mode==="retry"){assert.equal(existsSync(join(f.dir,"result.json")),false);assert.equal(summary.attempt_result.status,"success");assert.equal(summary.review.verdict,"RETRY");}
   if(mode==="blocked")assert.equal(summary.review.verdict,"BLOCKED");
   if(mode==="timeout")assert.match(summary.reason,/timeout/);
  }finally{rmSync(f.root,{recursive:true,force:true});}
 }}finally{HerdrClient.prototype.probe=probe;HerdrClient.prototype.read=read;HerdrClient.prototype.wait=wait;}
});
test("retry checkpoint keeps previous review separate from current attempt and acceptance stays external",async()=>{
 const f=fixture(2);try {
  atomicWrite(join(f.dir,"result.json"),f.result);
  const reviewed=reviewTask(f.repo,f.id,f.root);assert.equal(reviewed.details?.status,"running");
  const task=readJson<TaskRecord>(join(f.dir,"task.json"));task.attempt=2;task.status="running";delete task.status_reason;
  atomicWrite(join(f.dir,"task.json"),task);rmSync(join(f.dir,"result.json"));
  let summary=(taskStatus(f.repo,f.id,f.root).details?.summary as any);
  assert.equal(summary.attempt,2);assert.equal(summary.review.attempt,1);assert.equal(summary.attempt_result.attempt,1);assert.match(summary.reason,/Last reviewed attempt 1/);
  atomicWrite(join(f.dir,"result.json"),{...f.result,attempt:2,validation:[{command:"local check",outcome:"passed"}]});
  summary=(taskStatus(f.repo,f.id,f.root).details?.summary as any);assert.equal(summary.attempt_result.attempt,2);assert.equal(summary.review.attempt,1);
  assert.equal(reviewTask(f.repo,f.id,f.root).details?.verdict,"PASS");
  atomicWrite(join(f.dir,"semantic.json"),{judgment:"complete",reason:"accepted"});
  await acceptTask(f.repo,f.id,f.root);
  summary=(taskStatus(f.repo,f.id,f.root).details?.summary as any);
  assert.equal(summary.status,"accepted");assert.equal(summary.review.attempt,2);assert.equal(summary.human_approval,"not_established_by_wp");assert.equal(summary.business_delivery,"tracked_externally");
  assert.match(readFileSync(join(f.dir,"HANDOFF.md"),"utf8"),/status: accepted/);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});
test("terminal supervisor recovery cannot change historical failed to blocked",async()=>{
 const f=fixture();try {f.task.status="failed";atomicWrite(join(f.dir,"task.json"),f.task);
  await assert.rejects(supervise(f.repo,f.dir),/already failed/);
  assert.equal(readJson<TaskRecord>(join(f.dir,"task.json")).status,"failed");
 }finally{rmSync(f.root,{recursive:true,force:true});}
});
