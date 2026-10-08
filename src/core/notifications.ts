import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { digest, readJson, redact } from "./state.js";
import type { ResourceRecord, TaskRecord } from "./types.js";

/** Durable event IDs are task + attempt + append-only log sequence, not file existence. */
export function taskNotifications(dir: string) {
 const task=readJson<TaskRecord>(join(dir,"task.json"));
 const path=join(dir,"events.jsonl");
 if(!existsSync(path))return [];
 const resources=existsSync(join(dir,"resources.json"))?readJson<ResourceRecord>(join(dir,"resources.json")):undefined;
 const stages=new Set(["attempt_started","result_available","reviewed","semantic_judged","wait_timeout","accepted","cancelled","supervisor_error","cleanup_deferred","cleanup_succeeded"]);
 return readFileSync(path,"utf8").split("\n").flatMap((line,index)=>{
  if(!line.trim())return [];
  let event:Record<string,unknown>;try{event=JSON.parse(line);}catch{return [];}
  if(!stages.has(String(event.event)))return [];
  const attempt=event.attempt??task.attempt;
  const attemptPath=join(dir,"attempts",`attempt-${String(attempt).padStart(2,"0")}`,"resources.json");
  const bound=existsSync(attemptPath)?readJson<ResourceRecord>(attemptPath):Number(attempt)===task.attempt?resources:undefined;
  return [{id:`${digest(dir).slice(0,16)}:${task.contract.task_id}:${attempt}:${index+1}`,task_id:task.contract.task_id,attempt,sequence:index+1,
   stage:event.event,event:JSON.parse(redact(JSON.stringify(event))),resources:bound?{tab_id:bound.tab_id,pane_id:bound.pane_id,agent_id:bound.agent_id}:undefined,
   status:task.status,acceptance:"Not inferred from result files or Herdr lifecycle; explicit accept is required."}];
 });
}
