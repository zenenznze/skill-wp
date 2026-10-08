import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { HerdrClient } from "../herdr/client.js";
import { appendEvent, atomicWrite, digest, readJson, taskDir, WriterLock } from "./state.js";
import { deterministicReview } from "./reviewer.js";
import { assertResult, type ResourceRecord, type TaskRecord } from "./types.js";

/** Cancellation is not acceptance. Only settled, collected, scope-verified resources can be reclaimed. */
export async function cancelTask(repo:string,id:string,confirmed:boolean,root?:string) {
 if(process.env.HERDR_ENV!=="1")throw new Error("HERDR_ENV=1 is required");
 const dir=taskDir(repo,id,root),lock=new WriterLock(join(dir,"supervisor.lock"));
 lock.acquire();
 try {
  const task=readJson<TaskRecord>(join(dir,"task.json"));
  if(task.contract_digest!==digest(task.contract))throw new Error("contract digest mismatch");
  if(task.status==="accepted")throw new Error("accepted tasks use acceptance cleanup");
  const processPath=join(dir,"supervisor-process.json");
  if(existsSync(processPath)){const {pid}=readJson<{pid:number}>(processPath);try{process.kill(pid,0);throw new Error("supervisor is still running");}catch(error){if((error as NodeJS.ErrnoException).code!=="ESRCH")throw error;}}
  const resource=readJson<ResourceRecord>(join(dir,"resources.json"));
  if(resource.task_id!==id||resource.created_by_wp!==true||resource.adopted||!resource.pane_id||!resource.agent_id||resource.tab_id===process.env.HERDR_TAB_ID)
   throw new Error("resource ownership cannot be safely established");
  if(!existsSync(join(dir,"result.json"))||!existsSync(join(dir,"attempts",`attempt-${String(task.attempt).padStart(2,"0")}`,"output.json")))
   throw new Error("result and output must be collected before cancellation");
  const result=readJson<unknown>(join(dir,"result.json"));assertResult(result,task);
  const output=readJson<{unavailable?:boolean}>(join(dir,"attempts",`attempt-${String(task.attempt).padStart(2,"0")}`,"output.json"));
  if(output.unavailable)throw new Error("worker output has not been collected");
  const review=deterministicReview(resolve(repo),dir,task);
  if(review.actualFiles.length||result.changed_files.length)throw new Error("cancellation cleanup requires no undelivered repository changes");
  const herdr=new HerdrClient(resolve(repo));
  const snapshot=await herdr.snapshot();
  if(resource.lifecycle==="closed"){
   const tabs=(snapshot.result as {snapshot?:{tabs?:Array<Record<string,unknown>>}})?.snapshot?.tabs;
   if(tabs&&!tabs.some(t=>t.tab_id===resource.tab_id))return {preview:!confirmed,task_id:id,closed:[resource.tab_id],already_closed:true,acceptance:false};
   throw new Error("previously closed resource topology is inconsistent");
  }
  const topology=(snapshot.result as {snapshot?:{panes?:Array<Record<string,unknown>>}})?.snapshot;
  const panes=topology?.panes?.filter(p=>p.tab_id===resource.tab_id);
  if(panes?.length!==1||panes[0].pane_id!==resource.pane_id||panes[0].cwd!==resolve(repo)||!["idle","done"].includes(String(panes[0].agent_status)))
   throw new Error("resource topology or settled lifecycle could not be confirmed");
  const agent=await herdr.call(["agent","get",resource.agent_id]);
  const info=(agent.result as {agent?:Record<string,unknown>})?.agent;
  if(info?.tab_id!==resource.tab_id||info?.pane_id!==resource.pane_id)throw new Error("agent identity mismatch");
  if(!confirmed)return {preview:true,task_id:id,tab_id:resource.tab_id,acceptance:false};
  await herdr.read(resource.agent_id);
  await herdr.renameTab(resource.tab_id,`结束-${id}`);
  await herdr.closeTab(resource.tab_id);
  const after=await herdr.snapshot();
  const remaining=(after.result as {snapshot?:{tabs?:Array<Record<string,unknown>>}})?.snapshot?.tabs;
  if(!remaining||remaining.some(t=>t.tab_id===resource.tab_id))throw new Error("tab closure could not be verified");
  resource.lifecycle="closed";atomicWrite(join(dir,"resources.json"),resource);
  task.status="failed";task.updated_at=new Date().toISOString();atomicWrite(join(dir,"task.json"),task);
  appendEvent(join(dir,"events.jsonl"),{event:"cancelled",attempt:task.attempt,tab_id:resource.tab_id,acceptance:false});
  return {preview:false,task_id:id,closed:[resource.tab_id],acceptance:false};
 } finally {lock.release();}
}
