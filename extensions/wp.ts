import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { taskNotifications } from "../src/core/notifications.js";
import { cancelTask } from "../src/core/cancel.js";
import { taskDir } from "../src/core/state.js";
import { acceptTask, adoptTask, createTask, herdrSnapshot, listTasks, printable, reviewTask, runTask, setSessionRescue, stateRoot, taskStatus } from "../src/core/controller.js";

const Base=Type.Object({repo:Type.String(),task_id:Type.String()});
const text=(details:unknown)=>({content:[{type:"text" as const,text:printable(details)}],details});
const directDispatch=/\bherdr\s+(?:tab\s+create|agent\s+(?:start|prompt))\b/i;
function refreshWidget(ctx:ExtensionContext){if(!ctx.hasUI)return;const tasks=listTasks(ctx.cwd);const active=tasks.filter(t=>!["accepted","failed"].includes(t.status));ctx.ui.setWidget("wp-control",active.length?[`WP 控制面：${active.length} 个活跃任务`,...active.slice(0,4).map(t=>`• ${t.task_id}  ${t.status}  attempt ${t.attempt}`)]:undefined);}
export default function(pi:ExtensionAPI){
 let timer:ReturnType<typeof setInterval>|undefined;
 let bindings:Array<{repo:string;task_id:string}>=[],seen=new Set<string>();
 const stop=()=>{if(timer)clearInterval(timer);timer=undefined;};
 const restore=(ctx:ExtensionContext)=>{
  bindings=[];seen=new Set();
  for(const entry of ctx.sessionManager.getBranch()){
   if(entry.type==="custom"&&entry.customType==="wp-bind"){
    const data=entry.data as {repo:string;task_id:string};
    if(!bindings.some(b=>b.repo===data.repo&&b.task_id===data.task_id))bindings.push(data);
   }
   if(entry.type==="custom"&&entry.customType==="wp-notification-ack")for(const id of (entry.data as {ids:string[]}).ids)seen.add(id);
   if(entry.type==="message"&&entry.message.role==="custom"&&entry.message.customType==="wp-update"){
    const data=entry.message.details as {ids?:string[]}|undefined;
    for(const id of data?.ids||[])seen.add(id);
   }
  }
 };
 const pump=()=>{
  const updates=bindings.flatMap(b=>{try{return taskNotifications(taskDir(b.repo,b.task_id)).filter(e=>!seen.has(e.id));}catch{return [];}});
  if(!updates.length)return;
  try{pi.sendMessage({customType:"wp-update",content:"WP durable stage updates (not task acceptance). Independently inspect evidence and report results/blockers to the user.\n"+printable(updates),
   display:true,details:{ids:updates.map(e=>e.id)}},{triggerTurn:true,deliverAs:"followUp"});
  for(const update of updates)seen.add(update.id);
  }catch{/* Keep unconsumed IDs pending for a later session wake. */}
 };
 const bind=(repo:string,task_id:string)=>{
  if(bindings.some(b=>b.repo===repo&&b.task_id===task_id))return;
  bindings.push({repo,task_id});pi.appendEntry("wp-bind",{repo,task_id});
 };
 pi.on("session_start",async(_e,ctx)=>{stop();restore(ctx);refreshWidget(ctx);pump();timer=setInterval(pump,1000);timer.unref();});
 pi.on("session_shutdown",async()=>stop());
 pi.registerTool({name:"wp_task_watch",label:"WP Watch",description:"Bind a CLI-created task to this controller session for durable native stage notifications; no external until required.",parameters:Base,async execute(_id,p){taskStatus(p.repo,p.task_id);bind(p.repo,p.task_id);return text({watching:true,task_id:p.task_id});}});
 pi.registerTool({name:"wp_task_ack",label:"WP Ack",description:"Persist consumption of delivered notification IDs in this controller session; does not accept a task.",parameters:Type.Object({ids:Type.Array(Type.String())}),async execute(_id,p){pi.appendEntry("wp-notification-ack",{ids:p.ids});for(const id of p.ids)seen.add(id);return text({acknowledged:p.ids,acceptance:false});}});
 pi.registerTool({name:"wp_task_cancel",label:"WP Cancel",description:"Preview or confirm cancellation cleanup independently of acceptance. Only owned settled resources with collected results and no repository changes may close.",parameters:Type.Intersect([Base,Type.Object({confirmed:Type.Boolean()})]),async execute(_id,p){return text(await cancelTask(p.repo,p.task_id,p.confirmed));}});
 pi.on("tool_call",async(event)=>{if(event.toolName!=="bash")return;const command=String((event.input as Record<string,unknown>).command||"");if(directDispatch.test(command)){const rescuePath=await import("node:fs").then(fs=>`${stateRoot()}/session-rescue.json`);let active=false;try{const fs=await import("node:fs");const x=JSON.parse(fs.readFileSync(rescuePath,"utf8"));active=x.enabled&&Date.parse(x.until)>Date.now();}catch{}if(!active)return{block:true,reason:"WP 已纳管 Agent 调度。请使用 wp_task_run；诊断或紧急操作先在 /wp 开启限时救援模式。"};}return;});
 pi.registerTool({name:"wp_task_create",label:"WP Create",description:"Create a durable WP task contract and HANDOFF before delegation.",parameters:Type.Object({repo:Type.String(),task_id:Type.String(),single_outcome:Type.String(),deliverables:Type.Array(Type.String()),write_scope:Type.Array(Type.String()),read_only:Type.Boolean(),acceptance:Type.Array(Type.String()),resume_boundary:Type.String(),max_attempts:Type.Optional(Type.Integer({minimum:1,maximum:10}))}),async execute(_id,p){const r=createTask({...p,max_attempts:p.max_attempts??3});return text(r);}});
 pi.registerTool({name:"wp_task_status",label:"WP Status",description:"Read durable WP task state. Herdr lifecycle is evidence only.",parameters:Base,async execute(_id,p){return text(taskStatus(p.repo,p.task_id));}});
 pi.registerTool({name:"wp_task_run",label:"WP Run",description:"Start the durable supervisor in a detached process and return immediately; use wp_task_status for progress while the controller remains interactive.",parameters:Type.Intersect([Base,Type.Object({controller_tab_id:Type.Optional(Type.String())})]),async execute(_id,p){const r=runTask(p.repo,p.task_id,p.controller_tab_id??process.env.HERDR_TAB_ID);bind(p.repo,p.task_id);return text(r);}});
 pi.registerTool({name:"wp_task_resume",label:"WP Resume",description:"Resume supervision in a detached process and return immediately, reconnecting to the original live Herdr resource when possible.",parameters:Type.Intersect([Base,Type.Object({controller_tab_id:Type.Optional(Type.String())})]),async execute(_id,p){const r=runTask(p.repo,p.task_id,p.controller_tab_id??process.env.HERDR_TAB_ID);bind(p.repo,p.task_id);return text(r);}});
 pi.registerTool({name:"wp_task_review",label:"WP Review",description:"Validate result.json and write an independent PASS, RETRY, or BLOCKED review.",parameters:Base,async execute(_id,p){return text(reviewTask(p.repo,p.task_id));}});
 pi.registerTool({name:"wp_task_adopt",label:"WP Adopt",description:"Preview or confirm binding an existing Herdr resource to a WP task.",parameters:Type.Intersect([Base,Type.Object({tab_id:Type.String(),pane_id:Type.Optional(Type.String()),agent_id:Type.Optional(Type.String()),session_path:Type.Optional(Type.String()),confirmed:Type.Boolean()})]),async execute(_id,p){return text(adoptTask(p.repo,p.task_id,{tab_id:p.tab_id,pane_id:p.pane_id,agent_id:p.agent_id,session_path:p.session_path},p.confirmed));}});
 pi.registerTool({name:"wp_task_accept",label:"WP Accept",description:"Accept a task only after a PASS review and mark its visible tab complete.",parameters:Base,async execute(_id,p){return text(await acceptTask(p.repo,p.task_id));}});
 pi.registerCommand("wp",{description:"WP 统一任务调度、状态、恢复、接管和救援入口",handler:async(_args,ctx)=>{if(!ctx.hasUI){ctx.ui.notify("/wp requires TUI mode","error");return;}const action=await ctx.ui.select("WP 控制面",["查看当前仓库任务","查看 Herdr 前台映射","开启 15 分钟救援模式","退出救援模式"]);if(action==="查看当前仓库任务"){const tasks=listTasks(ctx.cwd);await ctx.ui.custom((_tui,theme,_kb,done)=>({render:(w:number)=>[theme.fg("accent","WP Tasks"),...(tasks.length?tasks.map(t=>`${t.task_id}  ${t.status}  attempt ${t.attempt}`):["No tasks"]),theme.fg("dim","Press any key to close")].map(x=>x.slice(0,w)),handleInput:()=>done(undefined),invalidate:()=>{}}));}else if(action==="查看 Herdr 前台映射"){try{ctx.ui.notify(printable(await herdrSnapshot(ctx.cwd)).slice(0,2000),"info");}catch(e){ctx.ui.notify(e instanceof Error?e.message:String(e),"error");}}else if(action==="开启 15 分钟救援模式"){setSessionRescue(true,15);ctx.ui.notify("WP 救援模式已开启 15 分钟；所有绕行仍应写入任务事件日志。","warning");}else if(action==="退出救援模式"){setSessionRescue(false);ctx.ui.notify("WP 救援模式已关闭。","info");}refreshWidget(ctx);}});
}
