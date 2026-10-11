import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { atomicWrite, readJson, redact } from "./state.js";
import type { Review, TaskRecord, TaskResult } from "./types.js";

export const attemptPath = (dir: string, attempt: number) => join(dir, "attempts", `attempt-${String(attempt).padStart(2,"0")}`);

/** Keep executor claims separate from controller decisions. Never synthesize a result. */
export function archiveAttemptEvidence(dir: string, task: TaskRecord): void {
 const ad=attemptPath(dir,task.attempt);mkdirSync(ad,{recursive:true});
 for(const name of ["result.json","HANDOFF.md"]){
  const source=join(dir,name),target=join(ad,name);
  if(existsSync(source))copyFileSync(source,target);
 }
}
function readEvidence<T>(path:string,warnings:string[]):T|undefined {
 if(!existsSync(path))return;
 try{return readJson<T>(path);}catch{warnings.push(`Unreadable evidence: ${path}`);return;}
}
export function controllerSummary(dir:string,task:TaskRecord,handoffText?:string) {
 const warnings:string[]=[];
 let result:TaskResult|undefined,review:Review|undefined,resultPath:string|undefined,reviewPath:string|undefined,resultAttempt:number|undefined;
 for(let attempt=task.attempt;attempt>=1;attempt--){
  const ad=attemptPath(dir,attempt);
  if(!result){
   const path=existsSync(join(ad,"result.json"))?join(ad,"result.json"):attempt===task.attempt&&existsSync(join(dir,"result.json"))?join(dir,"result.json"):undefined;
   const candidate=path?readEvidence<TaskResult>(path,warnings):undefined;
   if(candidate&&(candidate.task_id!==task.contract.task_id||(candidate.attempt!==undefined&&candidate.attempt!==attempt)))warnings.push("Result identity mismatch");
   else if(candidate){result=candidate;resultPath=path;resultAttempt=attempt;}
  }
  if(!review){
   const path=existsSync(join(ad,"review.json"))?join(ad,"review.json"):join(dir,"reviews",`attempt-${String(attempt).padStart(2,"0")}.json`);
   const candidate=readEvidence<Review>(path,warnings);
   if(candidate&&(candidate.task_id!==task.contract.task_id||candidate.attempt!==attempt))warnings.push("Review identity mismatch");
   else if(candidate){review=candidate;reviewPath=path;}
  }
  if(result&&review)break;
 }
 const semanticPath=join(attemptPath(dir,task.attempt),"semantic.json");
 const semantic=readEvidence<{judgment:string;reason:string}>(semanticPath,warnings);
 const handoff=handoffText??(existsSync(join(dir,"HANDOFF.md"))?readFileSync(join(dir,"HANDOFF.md"),"utf8"):"");
 const handoffStatus=handoff.match(/^---\r?\n[\s\S]*?^status: *([^\r\n]+)/m)?.[1].trim();
 if(handoffStatus&&handoffStatus!==task.status)warnings.push(`Root HANDOFF status differs from authoritative task.json: ${handoffStatus}`);
 const reason=task.status_reason||(review?[`Last reviewed attempt ${review.attempt}: ${review.summary}`,...review.findings].join("; "):"No controller decision recorded for the current attempt");
 return {
  schema:"wp-controller-summary-v1",task_id:task.contract.task_id,scope:"wp_contract",authority:join(dir,"task.json"),
  status:task.status,attempt:task.attempt,max_attempts:task.contract.max_attempts,updated_at:task.updated_at,reason,
  attempt_result:result?{attempt:resultAttempt,path:resultPath,status:result.status,summary:result.summary}:null,
  review:review?{attempt:review.attempt,path:reviewPath,verdict:review.verdict,summary:review.summary,findings:review.findings}:null,
  semantic:semantic?{attempt:task.attempt,path:semanticPath,judgment:semantic.judgment,reason:semantic.reason}:null,
  human_approval:"not_established_by_wp",business_delivery:"tracked_externally",resume_boundary:task.contract.resume_boundary,warnings,
 };
}
const START="<!-- WP:CONTROLLER:START -->",END="<!-- WP:CONTROLLER:END -->";
/** task.json is authoritative; summary and HANDOFF are derived recovery views. */
export function persistControllerState(dir:string,task:TaskRecord,reason:string):void {
 task.status_reason=redact(reason);task.updated_at=new Date().toISOString();atomicWrite(join(dir,"task.json"),task);
 const path=join(dir,"HANDOFF.md");
 let handoff=existsSync(path)?readFileSync(path,"utf8"):"";
 if(handoff.startsWith("---")){
  const end=handoff.indexOf("\n---",3);
  if(end!==-1){let front=handoff.slice(0,end);front=front.replace(/^status:.*$/m,`status: ${task.status}`).replace(/^updated_at:.*$/m,`updated_at: ${task.updated_at}`);handoff=front+handoff.slice(end);}
 }
 // Derive from authoritative state and evidence, with the synchronized header in memory.
 const summary=controllerSummary(dir,task,handoff);
 const block=[START,"# WP Controller State","", "This block is the current WP contract decision. Executor prose outside it is historical evidence.",
  "It does not establish human approval or later business delivery; follow version-bound external evidence separately.","", "```json",redact(JSON.stringify(summary,null,2)),"```",END].join("\n");
 const start=handoff.indexOf(START),end=handoff.indexOf(END,start);
 handoff=start!==-1&&end!==-1?handoff.slice(0,start)+block+handoff.slice(end+END.length):handoff.trimEnd()+"\n\n"+block+"\n";
 atomicWrite(path,handoff);atomicWrite(join(dir,"summary.json"),summary);
}
