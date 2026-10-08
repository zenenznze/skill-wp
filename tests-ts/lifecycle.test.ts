import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import extension from "../extensions/wp.js";
import { createTask } from "../src/core/controller.js";
import { taskDir, appendEvent } from "../src/core/state.js";
import { taskNotifications } from "../src/core/notifications.js";
import { cancelTask } from "../src/core/cancel.js";
import { HerdrClient } from "../src/herdr/client.js";

function fixture() {
 const root=mkdtempSync(join(tmpdir(),"wp-life-")),repo=join(root,"repo");
 mkdirSync(repo);execFileSync("git",["init","-q"],{cwd:repo});
 const id="20261004-lifecycle";
 createTask({repo,task_id:id,single_outcome:"Observe lifecycle",deliverables:["report"],write_scope:[],read_only:true,acceptance:["report collected"],resume_boundary:"Read state",max_attempts:1},root);
 return {root,repo,id,dir:taskDir(repo,id,root)};
}
test("result existence cannot emit success; partial logs and delayed stages have stable IDs",()=>{
 const f=fixture();try {
  writeFileSync(join(f.dir,"result.json"),JSON.stringify({task_id:f.id,status:"failed"}));
  assert.deepEqual(taskNotifications(f.dir),[]);
  appendEvent(join(f.dir,"events.jsonl"),{event:"reviewed",attempt:1,verdict:"PASS"});
  const first=taskNotifications(f.dir);assert.equal(first[0].stage,"reviewed");
  appendEvent(join(f.dir,"events.jsonl"),{event:"semantic_judged",attempt:1,judgment:"manual_review",reason:"missing_api_key"});
  assert.equal(taskNotifications(f.dir)[0].id,first[0].id);
  assert.equal(taskNotifications(f.dir)[1].event.reason,"missing_api_key");
  const log=join(f.dir,"events.jsonl");
  const complete=readFileSync(log,"utf8");
  writeFileSync(log,complete+'{"event":"wait_timeout"');
  assert.equal(taskNotifications(f.dir).length,2);
  writeFileSync(log,complete+'{"event":"wait_timeout","attempt":2}\n');
  assert.equal(taskNotifications(f.dir).length,3);
 }finally{rmSync(f.root,{recursive:true,force:true});}
});

test("plugin exposes watch/cancel, wakes natively, and deduplicates across reload",async()=>{
 const f=fixture(),old=process.env.WP_STATE_DIR;process.env.WP_STATE_DIR=f.root;
 // Minimal Pi runtime adapter: sends are persisted into the active session branch.
 const branch:any[]=[],handlers=new Map<string,Function>(),tools=new Map<string,any>(),messages:any[]=[];
 const pi={on:(name:string,fn:Function)=>handlers.set(name,fn),registerTool:(t:any)=>tools.set(t.name,t),
  registerCommand:()=>{},appendEntry:(customType:string,data:unknown)=>branch.push({type:"custom",customType,data}),
  sendMessage:(message:any,options:any)=>{messages.push({message,options});branch.push({type:"message",message:{role:"custom",...message}});}};
 const ctx={hasUI:false,cwd:f.repo,sessionManager:{getBranch:()=>branch}};
 try {
  extension(pi as unknown as ExtensionAPI);
  await handlers.get("session_start")!({},ctx);
  await tools.get("wp_task_watch").execute("x",{repo:f.repo,task_id:f.id});
  for(const n of [2,3]){
   const id=`20261004-lifecycle-${n}`;
   createTask({repo:f.repo,task_id:id,single_outcome:"Observe lifecycle",deliverables:["report"],write_scope:[],read_only:true,acceptance:["report collected"],resume_boundary:"Read state",max_attempts:1},f.root);
   await tools.get("wp_task_watch").execute("x",{repo:f.repo,task_id:id});
   appendEvent(join(taskDir(f.repo,id,f.root),"events.jsonl"),{event:n===2?"supervisor_error":"reviewed",attempt:1,verdict:n===3?"PASS":undefined,message:n===2?"worker unavailable":undefined});
  }
  appendEvent(join(f.dir,"events.jsonl"),{event:"reviewed",attempt:1,verdict:"PASS"});
  appendEvent(join(f.dir,"events.jsonl"),{event:"semantic_judged",attempt:1,judgment:"manual_review",reason:"missing_api_key"});
  await new Promise(resolve=>setTimeout(resolve,1100));
  assert.equal(messages.length,1);assert.equal(messages[0].options.triggerTurn,true);
  assert.match(messages[0].message.content,/missing_api_key/);
  assert.match(messages[0].message.content,/worker unavailable/);
  assert.equal(messages[0].message.details.ids.length,4);
  await tools.get("wp_task_ack").execute("x",{ids:messages[0].message.details.ids});
  await handlers.get("session_shutdown")!({});
  extension(pi as unknown as ExtensionAPI);
  await handlers.get("session_start")!({},ctx);
  assert.equal(messages.length,1);
  appendEvent(join(f.dir,"events.jsonl"),{event:"wait_timeout",attempt:2});
  await handlers.get("session_start")!({},ctx);
  assert.equal(messages.length,2);
  assert.ok(tools.has("wp_task_cancel"));
 }finally{await handlers.get("session_shutdown")!({});if(old===undefined)delete process.env.WP_STATE_DIR;else process.env.WP_STATE_DIR=old;rmSync(f.root,{recursive:true,force:true});}
});

test("cancel previews, refuses working/adopted resources and verifies closure without acceptance",async()=>{
 const f=fixture(),original=HerdrClient.prototype.call,env=process.env.HERDR_ENV;process.env.HERDR_ENV="1";
 let closed=false,status="working",adopted=false;
 const resource=()=>writeFileSync(join(f.dir,"resources.json"),JSON.stringify({schema:"wp-resources-v1",task_id:f.id,tab_id:"test:t1",pane_id:"test:p1",agent_id:"worker",created_by_wp:true,adopted}));
 resource();
 writeFileSync(join(f.dir,"result.json"),JSON.stringify({schema:"wp-result-v2",task_id:f.id,attempt:1,status:"success",summary:"report collected",changed_files:[],validation:[{command:"read report",outcome:"passed"}],handoff_path:join(f.dir,"HANDOFF.md")}));
 mkdirSync(join(f.dir,"attempts/attempt-01"));writeFileSync(join(f.dir,"attempts/attempt-01/output.json"),"{}");
 HerdrClient.prototype.call=async function(args){
  if(args[0]==="api")return {result:{snapshot:{panes:closed?[]:[{tab_id:"test:t1",pane_id:"test:p1",cwd:f.repo,agent_status:status}],tabs:closed?[]:[{tab_id:"test:t1"}]}}};
  if(args[1]==="get")return {result:{agent:{tab_id:"test:t1",pane_id:"test:p1"}}};
  if(args[1]==="close")closed=true;
  return {};
 };
 try {
  await assert.rejects(cancelTask(f.repo,f.id,true,f.root),/settled lifecycle/);
  status="done";assert.equal((await cancelTask(f.repo,f.id,false,f.root)).preview,true);assert.equal(closed,false);
  adopted=true;resource();await assert.rejects(cancelTask(f.repo,f.id,true,f.root),/ownership/);
  adopted=false;resource();
  writeFileSync(join(f.dir,"supervisor.lock"),JSON.stringify({pid:process.pid}));
  await assert.rejects(cancelTask(f.repo,f.id,true,f.root),/locked by live pid/);
  rmSync(join(f.dir,"supervisor.lock"));
  writeFileSync(join(f.repo,"unshipped.txt"),"preserve");
  await assert.rejects(cancelTask(f.repo,f.id,true,f.root),/undelivered/);
  rmSync(join(f.repo,"unshipped.txt"));
  writeFileSync(join(f.dir,"result.json"),JSON.stringify({schema:"wp-result-v2",task_id:f.id,attempt:1,status:"blocked",summary:"external blocker",blocker:"missing dependency",changed_files:[],validation:[],handoff_path:join(f.dir,"HANDOFF.md")}));
  const result=await cancelTask(f.repo,f.id,true,f.root);
  assert.equal(result.acceptance,false);assert.equal(closed,true);
  assert.equal((await cancelTask(f.repo,f.id,true,f.root)).already_closed,true);
 }finally{HerdrClient.prototype.call=original;if(env===undefined)delete process.env.HERDR_ENV;else process.env.HERDR_ENV=env;rmSync(f.root,{recursive:true,force:true});}
});
