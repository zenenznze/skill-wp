import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { HerdrClient } from "../herdr/client.js";
import { appendEvent, atomicWrite, readJson, redact, WriterLock } from "./state.js";
import { deterministicReview, writeReview } from "./reviewer.js";
import { judgeWithJev, type JevBoundaryResult } from "../semantic/jev-boundary.js";
import type { Receipt, ResourceRecord, Review, TaskRecord } from "./types.js";

const now=()=>new Date().toISOString(), pad=(n:number)=>String(n).padStart(2,"0");
export interface SupervisorOptions { controllerTabId?: string; waitTimeoutMs?: number; pollMs?: number }
function attemptDir(dir:string,n:number){return join(dir,"attempts",`attempt-${pad(n)}`);}
function promptFor(task:TaskRecord,dir:string,feedback?:Review):string{
 const retry=feedback?` Previous deterministic review returned RETRY: ${feedback.findings.join("; ")}. Correct those findings without exceeding scope.`:"";
 return redact(`WP task ${task.contract.task_id}. Read ${join(dir,"HANDOFF.md")} first. This is attempt ${task.attempt}.${retry} Work only within the declared contract. Write the terminal result to ${join(dir,"result.json")} and update HANDOFF.md. Do not push, deploy, or read credentials.`);
}
function archiveFile(source:string,target:string){if(existsSync(source))copyFileSync(source,target);}
export async function supervise(repo:string,dir:string,options:SupervisorOptions={}):Promise<Receipt>{
 const lock=new WriterLock(join(dir,"supervisor.lock")); lock.acquire();
 try{
  let task=readJson<TaskRecord>(join(dir,"task.json"));
  if(["accepted","failed"].includes(task.status)) throw new Error(`task is already ${task.status}`);
  const herdr=new HerdrClient(repo), timeout=options.waitTimeoutMs??Number(process.env.WP_WAIT_TIMEOUT_MS||3_600_000), poll=options.pollMs??1000;
  let previous:Review|undefined;
  while(task.attempt<=task.contract.max_attempts){
   const ad=attemptDir(dir,task.attempt); mkdirSync(ad,{recursive:true});
   let resource=existsSync(join(dir,"resources.json"))?readJson<ResourceRecord>(join(dir,"resources.json")):undefined;
   const probe=resource?await herdr.probe(resource.tab_id,resource.agent_id):{reachable:false};
   if(!resource||!probe.reachable){
    const workspaceId=options.controllerTabId?.split(":")[0];
    const tab=await herdr.createTab(`WP-${task.contract.task_id}-A${task.attempt}`,workspaceId);
    const agentName=`wp-${task.contract.repo_id.slice(-8)}-${task.contract.task_id.slice(-10)}-${pad(task.attempt)}`;
    const agent=await herdr.startPi(agentName,tab.paneId);
    resource={schema:"wp-resources-v1",task_id:task.contract.task_id,workspace_id:workspaceId,tab_id:tab.tabId,pane_id:tab.paneId,agent_id:agent.agentId,session_path:agent.sessionPath,adopted:false,created_by_wp:true,attempt:task.attempt,lifecycle:"active",bound_at:now()};
    atomicWrite(join(dir,"resources.json"),resource); atomicWrite(join(ad,"resources.json"),resource);
    const prompt=promptFor(task,dir,previous); atomicWrite(join(ad,"prompt.txt"),prompt); await herdr.prompt(resource.agent_id!,prompt);
    appendEvent(join(dir,"events.jsonl"),{event:"attempt_started",attempt:task.attempt,tab_id:resource.tab_id,agent_id:resource.agent_id});
   }else{
    atomicWrite(join(ad,"resources.json"),resource); appendEvent(join(dir,"events.jsonl"),{event:"attempt_resumed",attempt:task.attempt,tab_id:resource.tab_id,agent_id:resource.agent_id});
   }
   const started=Date.now();
   while(!existsSync(join(dir,"result.json"))&&Date.now()-started<timeout){
    try{await herdr.wait(resource.agent_id!,Math.min(poll,5000));}catch{}
    try{const output=await herdr.read(resource.agent_id!);atomicWrite(join(ad,"output.json"),output);}catch{}
   }
   try{const output=await herdr.read(resource.agent_id!);atomicWrite(join(ad,"output.json"),output);}catch(error){atomicWrite(join(ad,"output.json"),{unavailable:true,error:error instanceof Error?error.message:String(error)});}
   if(!existsSync(join(dir,"result.json"))) {
    task.status="blocked";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);
    appendEvent(join(dir,"events.jsonl"),{event:"wait_timeout",attempt:task.attempt});
    return {schema:"wp-receipt-v1",operation:"run",task_id:task.contract.task_id,ok:false,timestamp:now(),details:{status:"blocked",reason:"worker did not produce result.json before timeout",resource}};
   }
   archiveFile(join(dir,"result.json"),join(ad,"result.json"));
   appendEvent(join(dir,"events.jsonl"),{event:"result_available",attempt:task.attempt,tab_id:resource.tab_id});
   const checked=deterministicReview(repo,dir,task);writeReview(dir,checked.review);atomicWrite(join(ad,"review.json"),checked.review);
   appendEvent(join(dir,"events.jsonl"),{event:"reviewed",attempt:task.attempt,verdict:checked.review.verdict,findings:checked.review.findings});
   if(checked.review.verdict==="PASS"){
    const thresholds={
     minimumConfidence:Number(process.env.WP_JEV_MIN_CONFIDENCE||0.75),
     minimumProbability:Number(process.env.WP_JEV_MIN_PROBABILITY||0.70),
    };
    const semantic:JevBoundaryResult=await judgeWithJev({
     goal:task.contract.single_outcome,
     acceptanceCriteria:task.contract.acceptance,
     executorSummary:checked.result?.summary||"No executor summary",
     validationResults:checked.result?.validation.map(v=>`${v.command}: ${v.outcome||v.result||"unknown"}`)||[],
     blockers:checked.result?.blocker?[typeof checked.result.blocker==="string"?checked.result.blocker:JSON.stringify(checked.result.blocker)]:[],
    },true,{thresholds});
    atomicWrite(join(ad,"semantic.json"),semantic);atomicWrite(join(dir,"semantic.json"),semantic);
    appendEvent(join(dir,"events.jsonl"),{event:"semantic_judged",attempt:task.attempt,judgment:semantic.judgment,probability:semantic.probability,confidence:semantic.confidence,reason:semantic.reason});
    if(semantic.judgment!=="complete"){
     task.status="blocked";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);
     return {schema:"wp-receipt-v1",operation:"run",task_id:task.contract.task_id,ok:false,timestamp:now(),details:{status:"blocked",reason:"semantic review did not confidently establish completion",semantic,resource}};
    }
    task.status="review";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);
    return {schema:"wp-receipt-v1",operation:"run",task_id:task.contract.task_id,ok:true,timestamp:now(),details:{status:"review",verdict:"PASS",semantic,attempt:task.attempt,resource,actual_files:checked.actualFiles}};
   }
   if(checked.review.verdict==="BLOCKED"){
    task.status="blocked";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);
    return {schema:"wp-receipt-v1",operation:"run",task_id:task.contract.task_id,ok:false,timestamp:now(),details:{status:"blocked",review:checked.review,resource}};
   }
   previous=checked.review; rmSync(join(dir,"result.json"),{force:true});
   if(task.attempt>=task.contract.max_attempts){task.status="failed";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);return {schema:"wp-receipt-v1",operation:"run",task_id:task.contract.task_id,ok:false,timestamp:now(),details:{status:"failed",review:checked.review}};}
   resource.lifecycle="retired";atomicWrite(join(ad,"resources.json"),resource);
   task.attempt++;task.status="running";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);rmSync(join(dir,"resources.json"),{force:true});
  }
  throw new Error("attempt limit exhausted");
 }catch(error){
  const task=readJson<TaskRecord>(join(dir,"task.json"));task.status="blocked";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);
  appendEvent(join(dir,"events.jsonl"),{event:"supervisor_error",attempt:task.attempt,message:redact(error instanceof Error?error.message:String(error))});
  throw error;
 }finally{lock.release();}
}
