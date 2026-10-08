#!/usr/bin/env node
import { existsSync, rmSync } from "node:fs";
import { cancelTask } from "./core/cancel.js";
import { join } from "node:path";
import { acceptTask, adoptTask, createTask, listTasks, migrateLegacy, printable, reviewTask, runTask, stateRoot, superviseTask, taskStatus } from "./core/controller.js";
import { readJson, taskDir } from "./core/state.js";
function args(argv:string[]){const out:Record<string,string|boolean>={};for(let i=0;i<argv.length;i++){const x=argv[i];if(x.startsWith("--")){const k=x.slice(2);if(argv[i+1]&&!argv[i+1].startsWith("--"))out[k]=argv[++i];else out[k]=true;}}return out;}
const [command,...rest]=process.argv.slice(2), a=args(rest), need=(k:string)=>{const v=a[k];if(typeof v!=="string"||!v)throw new Error(`--${k} is required`);return v;};
async function main(){
 switch(command){
  case "init": console.log(printable(createTask({repo:need("repo"),task_id:need("task-id"),single_outcome:need("goal"),deliverables:String(a.deliverables||a["write-scope"]||"HANDOFF result").split(","),write_scope:a["read-only"]?[]:need("write-scope").split(","),read_only:!!a["read-only"],acceptance:need("acceptance").split("|"),resume_boundary:String(a["resume-boundary"]||"Resume from HANDOFF.md and the latest checkpoint."),max_attempts:Number(a["max-attempts"]||3)}))); break;
  case "run": console.log(printable(runTask(need("repo"),need("task-id"),typeof a["controller-tab-id"]==="string"?a["controller-tab-id"]:undefined))); break;
  case "supervise": {const repo=need("repo"),id=need("task-id"),processPath=join(taskDir(repo,id),"supervisor-process.json");try{console.log(printable(await superviseTask(repo,id,typeof a["controller-tab-id"]==="string"?a["controller-tab-id"]:undefined)));}finally{try{if(existsSync(processPath)&&readJson<{pid:number}>(processPath).pid===process.pid)rmSync(processPath,{force:true});}catch{}}break;}
  case "status": console.log(printable(a["task-id"]?taskStatus(need("repo"),need("task-id")):{state_root:stateRoot(),tasks:listTasks(need("repo"))})); break;
  case "resume": console.log(printable(runTask(need("repo"),need("task-id"),typeof a["controller-tab-id"]==="string"?a["controller-tab-id"]:undefined))); break;
  case "check": console.log(printable(reviewTask(need("repo"),need("task-id")))); break;
  case "cancel": console.log(printable(await cancelTask(need("repo"),need("task-id"),a.confirm===true))); break;
  case "accept": console.log(printable(await acceptTask(need("repo"),need("task-id")))); break;
  case "adopt": console.log(printable(adoptTask(need("repo"),need("task-id"),{tab_id:need("tab-id"),pane_id:typeof a["pane-id"]==="string"?a["pane-id"]:undefined,agent_id:typeof a["agent-id"]==="string"?a["agent-id"]:undefined,session_path:typeof a["session-path"]==="string"?a["session-path"]:undefined},!!a.confirm))); break;
  case "migrate": console.log(printable(migrateLegacy(need("legacy-root"),stateRoot(),!!a.apply))); break;
  default: console.log("wpctl init|run|status|resume|adopt|check|accept|cancel|migrate [options]"); process.exitCode=command?2:0;
 }
}
main().catch(e=>{console.error(printable({error:e instanceof Error?e.message:String(e)}));process.exitCode=2;});
