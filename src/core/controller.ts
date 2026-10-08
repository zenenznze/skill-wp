import { spawn } from "node:child_process";
import { closeSync, copyFileSync, existsSync, mkdirSync, openSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { assertContract, type AtomicWorkContract, type Receipt, type ResourceRecord, type Review, type TaskRecord } from "./types.js";
import type { JevBoundaryResult } from "../semantic/jev-boundary.js";
import { appendEvent, atomicWrite, digest, fileSummary, readJson, repoId, safeJoin, stateRoot, taskDir } from "./state.js";
import { deterministicReview, repositorySnapshot, writeReview } from "./reviewer.js";
import { supervise } from "./supervisor.js";
import { HerdrClient } from "../herdr/client.js";
const now=()=>new Date().toISOString();
const receipt=(operation:string,taskId:string|undefined,ok:boolean,details?:Record<string,unknown>,paths?:string[]):Receipt=>({schema:"wp-receipt-v1",operation,task_id:taskId,ok,timestamp:now(),details,paths});
function handoff(c:AtomicWorkContract,status="planned"){return `---\ntask_id: ${c.task_id}\ntask_protocol_version: 2\nstatus: ${status}\ncreated_at: ${now()}\nupdated_at: ${now()}\n---\n\n# Goal\n\n${c.single_outcome}\n\n# Atomic Work Contract\n\n\`\`\`json\n${JSON.stringify(c,null,2)}\n\`\`\`\n\n# Acceptance Criteria\n\n${c.acceptance.map(x=>`- [ ] ${x}`).join("\n")}\n\n# Validation Commands\n\n\`\`\`bash\n# Executor records real end-to-end commands here.\n\`\`\`\n\n# Execution Progress\n\n- Not started.\n\n# Changed Files\n\n- None.\n\n# Validation Results\n\n- Not run.\n\n# Blockers\n\n- None.\n\n# Resume Instructions\n\n${c.resume_boundary}\n\n# Final Result\n\nNot complete.\n`;}
export function createTask(input:Omit<AtomicWorkContract,"task_protocol_version"|"repo_id">&{repo:string},root=stateRoot()):Receipt{
 const repo=resolve(input.repo),contract:AtomicWorkContract={...input,task_protocol_version:2,repo_id:repoId(repo)};delete (contract as unknown as Record<string,unknown>).repo;assertContract(contract);
 const dir=taskDir(repo,contract.task_id,root);if(existsSync(join(dir,"task.json")))throw new Error("task already exists");mkdirSync(join(dir,"attempts"),{recursive:true});mkdirSync(join(dir,"reviews"),{recursive:true});mkdirSync(join(dir,"checkpoints"),{recursive:true});
 atomicWrite(join(dir,"baseline.json"),repositorySnapshot(repo));
 const task:TaskRecord={schema:"wp-task-v2",contract,status:"pending",attempt:1,created_at:now(),updated_at:now(),contract_digest:digest(contract)};atomicWrite(join(dir,"task.json"),task);atomicWrite(join(dir,"HANDOFF.md"),handoff(contract));appendEvent(join(dir,"events.jsonl"),{event:"created"});return receipt("create",contract.task_id,true,{status:"pending",state_dir:dir},[join(dir,"HANDOFF.md"),join(dir,"task.json")]);
}
function load(repo:string,id:string,root=stateRoot()){const dir=taskDir(repo,id,root);const task=readJson<TaskRecord>(join(dir,"task.json"));if(task.contract_digest!==digest(task.contract))throw new Error("contract digest mismatch");return{dir,task};}
function processAlive(pid:number):boolean{try{process.kill(pid,0);return true;}catch{return false;}}
function supervisorEntry():string{
 if(process.env.WP_SUPERVISOR_ENTRY&&existsSync(process.env.WP_SUPERVISOR_ENTRY))return resolve(process.env.WP_SUPERVISOR_ENTRY);
 const adjacent=fileURLToPath(new URL("../cli.js",import.meta.url));
 if(existsSync(adjacent))return adjacent;
 const built=fileURLToPath(new URL("../../dist/src/cli.js",import.meta.url));
 if(existsSync(built))return built;
 throw new Error("WP background supervisor entry is missing; run npm run build before reloading the extension");
}
export async function superviseTask(repo:string,id:string,controllerTabId?:string,root=stateRoot()):Promise<Receipt>{
 if(process.env.HERDR_ENV!=="1")throw new Error("HERDR_ENV=1 is required");const x=load(repo,id,root);return supervise(resolve(repo),x.dir,{controllerTabId});
}
export function runTask(repo:string,id:string,controllerTabId?:string,root=stateRoot()):Receipt{
 if(process.env.HERDR_ENV!=="1")throw new Error("HERDR_ENV=1 is required");
 const x=load(repo,id,root);if(["accepted","failed","review"].includes(x.task.status))throw new Error(`task is already ${x.task.status}`);
 const processPath=join(x.dir,"supervisor-process.json");
 if(existsSync(processPath)){try{const active=readJson<{pid:number}>(processPath);if(processAlive(active.pid))return receipt("run",id,true,{status:x.task.status,supervisor:"already_running",pid:active.pid,state_dir:x.dir});}catch{}}
 const entry=supervisorEntry(),logPath=join(x.dir,"supervisor.log"),log=openSync(logPath,"a",0o600),previousStatus=x.task.status;
 x.task.status="running";x.task.updated_at=now();atomicWrite(join(x.dir,"task.json"),x.task);
 const args=[entry,"supervise","--repo",resolve(repo),"--task-id",id];if(controllerTabId)args.push("--controller-tab-id",controllerTabId);
 let child:ReturnType<typeof spawn>;try{child=spawn(process.execPath,args,{cwd:resolve(repo),env:{...process.env,WP_STATE_DIR:root},detached:true,windowsHide:true,stdio:["ignore",log,log]});if(!child.pid)throw new Error("failed to launch WP background supervisor");}catch(error){x.task.status=previousStatus;x.task.updated_at=now();atomicWrite(join(x.dir,"task.json"),x.task);throw error;}finally{closeSync(log);}
 child.on("error",error=>appendEvent(join(x.dir,"events.jsonl"),{event:"supervisor_launch_error",message:error.message}));child.unref();
 atomicWrite(processPath,{pid:child.pid,started_at:now(),controller_tab_id:controllerTabId});appendEvent(join(x.dir,"events.jsonl"),{event:"supervisor_started",pid:child.pid});
 return receipt("run",id,true,{status:"running",supervisor:"started",pid:child.pid,state_dir:x.dir,log_path:logPath,preflight:{semantic_key_available:!!process.env.TYPESAFE_API_KEY,warning:process.env.TYPESAFE_API_KEY?undefined:"TYPESAFE_API_KEY missing in supervisor environment; execution can proceed but semantic acceptance will block"},notification:"CLI does not wake a Pi session by itself; bind with wp_task_watch in the controller plugin"});
}
export function reviewTask(repo:string,id:string,root=stateRoot()):Receipt{const{dir,task}=load(repo,id,root),x=deterministicReview(repo,dir,task);const path=writeReview(dir,x.review);task.status=x.review.verdict==="PASS"?"review":x.review.verdict==="BLOCKED"?"blocked":task.attempt>=task.contract.max_attempts?"failed":"running";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);appendEvent(join(dir,"events.jsonl"),{event:"reviewed",attempt:task.attempt,verdict:x.review.verdict});return receipt("review",id,x.review.verdict==="PASS",{...x.review,actual_files:x.actualFiles},[path]);}
async function cleanupOwnedTabs(repo:string,dir:string,task:TaskRecord):Promise<{closed:string[];preserved:string[];errors:string[]}>{
 const paths:string[]=[];const current=join(dir,"resources.json");if(existsSync(current))paths.push(current);
 const attempts=join(dir,"attempts");if(existsSync(attempts))for(const name of readdirSync(attempts)) {const path=join(attempts,name,"resources.json");if(existsSync(path))paths.push(path);}
 const records=new Map<string,{path:string;resource:ResourceRecord}>();for(const path of paths){try{const resource=readJson<ResourceRecord>(path);if(!records.has(resource.tab_id))records.set(resource.tab_id,{path,resource});}catch{}}
 const herdr=new HerdrClient(resolve(repo)),closed:string[]=[],preserved:string[]=[],errors:string[]=[];
 for(const {path,resource} of records.values()){
  if(resource.task_id!==task.contract.task_id||resource.adopted||resource.created_by_wp!==true){preserved.push(resource.tab_id);continue;}
  const probe=await herdr.probe(resource.tab_id,resource.agent_id);if(!probe.reachable){resource.lifecycle="preserved";resource.cleanup_error="resource identity/topology could not be confirmed";atomicWrite(path,resource);preserved.push(resource.tab_id);errors.push(`${resource.tab_id}: ${resource.cleanup_error}`);appendEvent(join(dir,"events.jsonl"),{event:"cleanup_deferred",tab_id:resource.tab_id,error:resource.cleanup_error});continue;}
  resource.lifecycle="close_pending";atomicWrite(path,resource);appendEvent(join(dir,"events.jsonl"),{event:"cleanup_started",tab_id:resource.tab_id,attempt:resource.attempt});
  try{await herdr.closeTab(resource.tab_id);resource.lifecycle="closed";delete resource.cleanup_error;atomicWrite(path,resource);closed.push(resource.tab_id);appendEvent(join(dir,"events.jsonl"),{event:"cleanup_succeeded",tab_id:resource.tab_id});}
  catch(error){resource.lifecycle="preserved";resource.cleanup_error=error instanceof Error?error.message:String(error);atomicWrite(path,resource);preserved.push(resource.tab_id);errors.push(`${resource.tab_id}: ${resource.cleanup_error}`);appendEvent(join(dir,"events.jsonl"),{event:"cleanup_deferred",tab_id:resource.tab_id,error:resource.cleanup_error});}
 }
 return{closed,preserved,errors};
}
export async function acceptTask(repo:string,id:string,root=stateRoot()):Promise<Receipt>{const{dir,task}=load(repo,id,root);const reviewPath=join(dir,"reviews",`attempt-${String(task.attempt).padStart(2,"0")}.json`);if(!existsSync(reviewPath)||readJson<Review>(reviewPath).verdict!=="PASS"||task.status!=="review")throw new Error("acceptance requires current PASS review");const semanticPath=join(dir,"semantic.json");if(!existsSync(semanticPath)||readJson<JevBoundaryResult>(semanticPath).judgment!=="complete")throw new Error("acceptance requires a confident semantic completion judgment");const current=deterministicReview(repo,dir,task);if(current.review.verdict!=="PASS")throw new Error(`repository changed after PASS review: ${current.review.findings.join("; ")}`);task.status="accepted";task.updated_at=now();atomicWrite(join(dir,"task.json"),task);appendEvent(join(dir,"events.jsonl"),{event:"accepted"});const cleanup=await cleanupOwnedTabs(repo,dir,task);return receipt("accept",id,true,{status:"accepted",cleanup});}
export function adoptTask(repo:string,id:string,resource:Pick<ResourceRecord,"tab_id"|"pane_id"|"agent_id"|"session_path">,confirmed:boolean,root=stateRoot()):Receipt{const{dir,task}=load(repo,id,root);if(!confirmed)return receipt("adopt",id,true,{preview:true,resource,write_scope:task.contract.write_scope});const bound:ResourceRecord={schema:"wp-resources-v1",task_id:id,...resource,adopted:true,created_by_wp:false,lifecycle:"preserved",bound_at:now()};atomicWrite(join(dir,"resources.json"),bound);appendEvent(join(dir,"events.jsonl"),{event:"resource_adopted",tab_id:bound.tab_id});return receipt("adopt",id,true,{preview:false,resource:bound});}
export function taskStatus(repo:string,id:string,root=stateRoot()):Receipt{const{dir,task}=load(repo,id,root);const resources=existsSync(join(dir,"resources.json"))?readJson<ResourceRecord>(join(dir,"resources.json")):undefined;let supervisor:Record<string,unknown>|undefined;try{const saved=readJson<{pid:number;started_at:string}>(join(dir,"supervisor-process.json"));supervisor={...saved,alive:processAlive(saved.pid)};}catch{}return receipt("status",id,true,{status:task.status,attempt:task.attempt,max_attempts:task.contract.max_attempts,resources,supervisor,state_dir:dir});}
export function listTasks(repo:string,root=stateRoot()):Array<{task_id:string;status:string;attempt:number}>{const base=safeJoin(root,"repos",repoId(repo),"tasks");if(!existsSync(base))return[];return readdirSync(base).flatMap(id=>{try{const t=readJson<TaskRecord>(join(base,id,"task.json"));return[{task_id:id,status:t.status,attempt:t.attempt}];}catch{return[];}});}
export function migrateLegacy(legacyRoot:string,targetRoot=stateRoot(),apply=false):Receipt{const source=resolve(legacyRoot),files:string[]=[];const walk=(p:string)=>{for(const n of readdirSync(p,{withFileTypes:true})){const q=join(p,n.name);n.isDirectory()?walk(q):files.push(q);}};if(existsSync(source))walk(source);if(apply)for(const file of files){const target=safeJoin(targetRoot,"legacy",relative(source,file));mkdirSync(dirname(target),{recursive:true});if(existsSync(target)&&fileSummary(target).sha256!==fileSummary(file).sha256)throw new Error(`migration conflict: ${relative(source,file)}`);copyFileSync(file,target);}return receipt("migrate",undefined,true,{preview:!apply,file_count:files.length,source});}
export function setSessionRescue(enabled:boolean,minutes=15){atomicWrite(join(stateRoot(),"session-rescue.json"),{enabled,until:new Date(Date.now()+minutes*60000).toISOString()});}
export async function herdrSnapshot(repo:string){return new HerdrClient(resolve(repo)).snapshot();}
export function printable(value:unknown){return JSON.stringify(value,null,2);}
export {stateRoot};
