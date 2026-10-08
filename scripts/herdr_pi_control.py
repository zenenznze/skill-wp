#!/usr/bin/env python3
"""Fail-closed, lifecycle-only Herdr bridge for Pi execution terminals."""
from __future__ import annotations

import argparse, hashlib, hmac, json, os, re, secrets, stat, subprocess, sys, tempfile
try:
    import fcntl
except ImportError:  # Windows native Python has no fcntl; msvcrt is used below.
    fcntl = None
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "wp-state" / "herdr-receipts"
KEY_NAME = ".bridge-key"
SCHEMA = "herdr-pi-lifecycle-v3"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
BAD_WORDS = {"worker", "reviewer", "agent", "codex", "claude"}

class BridgeError(ValueError): pass
class CommandError(RuntimeError): pass
class HerdrTimeout(CommandError): pass
class ProtocolError(RuntimeError): pass

def require_object(v: Any, field: str) -> dict[str, Any]:
    if not isinstance(v, dict): raise ProtocolError(f"{field} must be an object")
    return v

def require_string(v: Any, field: str) -> str:
    if not isinstance(v, str) or not v.strip(): raise BridgeError(f"{field} must be a non-empty string")
    return v.strip()

def require_int(v: Any, field: str, minimum=0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum: raise BridgeError(f"{field} must be an integer >= {minimum}")
    return v

def semantic_name(v: Any) -> str:
    name=require_string(v,"agent_name"); parts=name.split("-")
    if not NAME_RE.fullmatch(name) or len(parts)<2 or any(x in BAD_WORDS for x in parts): raise BridgeError("agent_name must contain task and role semantics")
    return name

def semantic_label(v: Any, field: str) -> str:
    value=require_string(v,field)
    if value.isdigit() or value.lower() in BAD_WORDS: raise BridgeError(f"{field} must be semantic")
    return value

def canonical_cwd(v: Any) -> str:
    raw=require_string(v,"cwd"); p=Path(raw)
    if not p.is_absolute() or ".." in p.parts: raise BridgeError("cwd must be an absolute path without ..")
    try: p=p.resolve(strict=True)
    except OSError as exc: raise BridgeError(f"cwd does not exist: {raw}") from exc
    if not p.is_dir(): raise BridgeError("cwd must be a directory")
    return str(p)

def validate_plan(v: Any) -> dict[str, Any]:
    p=require_object(v,"plan"); mode=p.get("space_mode","cwd")
    if mode not in {"cwd","topic"}: raise BridgeError("space_mode must be cwd or topic")
    if p.get("kind")!="pi": raise BridgeError("kind must be pi")
    if p.get("split",False) is not False or p.get("no_focus",True) is not True: raise BridgeError("split is forbidden and no_focus must be true")
    return {"cwd":canonical_cwd(p.get("cwd")),"space_mode":mode,"topic":semantic_label(p.get("topic"),"topic") if mode=="topic" else None,"agent_name":semantic_name(p.get("agent_name")),"tab_label":semantic_label(p.get("tab_label"),"tab_label"),"controller_tab_id":require_string(p.get("controller_tab_id"),"controller_tab_id"),"controller_tab_label":semantic_label(p.get("controller_tab_label"),"controller_tab_label"),"kind":"pi","split":False,"no_focus":True,"prompt":require_string(p.get("prompt"),"prompt"),"timeout_seconds":require_int(p.get("timeout_seconds",120),"timeout_seconds",1)}

def receipt_path(v: Any) -> Path:
    raw=require_string(v,"receipt"); rel=Path(raw)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or any(part.startswith(".") for part in rel.parts): raise BridgeError("receipt must be a non-hidden relative path under wp-state")
    root=STATE.resolve(); candidate=Path(os.path.abspath(STATE / rel))
    if root not in candidate.parents: raise BridgeError("receipt escapes wp-state")
    parent=candidate.parent
    if parent.resolve() != parent or root not in parent.resolve().parents and parent.resolve() != root:
        raise BridgeError("receipt parent escapes wp-state")
    return candidate

def _secure_flags(base: int) -> int:
    return base | getattr(os,"O_NOFOLLOW",0) | getattr(os,"O_CLOEXEC",0)

def _check_regular(fd: int, *, private=False) -> os.stat_result:
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise BridgeError("file must be a single-link regular file")
    current_uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if info.st_uid != current_uid: raise BridgeError("file owner mismatch")
    if private and os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600: raise BridgeError("private file mode must be 0600")
    return info

def bridge_key(create=True) -> bytes:
    STATE.mkdir(parents=True,exist_ok=True); path=STATE/KEY_NAME
    flags=_secure_flags(os.O_RDONLY)
    try: fd=os.open(path,flags)
    except FileNotFoundError:
        if not create: raise BridgeError("bridge key is missing")
        try:
            fd=os.open(path,_secure_flags(os.O_WRONLY|os.O_CREAT|os.O_EXCL),0o600)
            key=secrets.token_bytes(32); os.write(fd,key); os.fsync(fd); os.close(fd)
            fd=os.open(path,flags)
        except FileExistsError: fd=os.open(path,flags)
    try:
        _check_regular(fd,private=True); key=os.read(fd,64)
        if len(key)!=32: raise BridgeError("bridge key is invalid")
        return key
    finally: os.close(fd)

def _mac_payload(r: dict[str,Any]) -> bytes:
    stable={k:v for k,v in r.items() if k!="receipt_mac"}
    return json.dumps(stable,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()

def sign_receipt(r: dict[str,Any], key: bytes|None=None) -> dict[str,Any]:
    out=dict(r); out["receipt_mac"]=hmac.new(key or bridge_key(),_mac_payload(out),hashlib.sha256).hexdigest(); return out

def validate_receipt(v: Any, key: bytes|None=None) -> dict[str,Any]:
    r=require_object(v,"receipt")
    required={"schema","receipt_id","receipt_mac","lifecycle_only","partial","closed","command","status","inconclusive","timeout","error_class","error_message","workspace_id","workspace_created","controller_tab_id","controller_tab_label","tab_id","tab_label","pane_id","agent_id","created"}
    if set(r)!=required or r.get("schema")!=SCHEMA or r.get("lifecycle_only") is not True or r.get("command")!="run": raise BridgeError("receipt schema mismatch")
    for f in ("receipt_id","receipt_mac","status","controller_tab_id","controller_tab_label","tab_label"): require_string(r.get(f),f)
    for f in ("partial","closed","inconclusive","timeout","workspace_created"):
        if not isinstance(r.get(f),bool): raise BridgeError(f"{f} must be boolean")
    for f in ("workspace_id","tab_id","pane_id","agent_id","error_class","error_message"):
        if r.get(f) is not None and not isinstance(r.get(f),str): raise BridgeError(f"{f} must be string or null")
    expected=hmac.new(key or bridge_key(create=False),_mac_payload(r),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(r["receipt_mac"],expected): raise BridgeError("receipt provenance check failed")
    if r["closed"] and r["partial"]: raise BridgeError("partial receipt cannot be closed")
    if not r["partial"]:
        for f in ("workspace_id","tab_id","pane_id","agent_id"): require_string(r.get(f),f)
        if r["tab_id"]==r["controller_tab_id"]: raise BridgeError("execution tab cannot equal controller tab")
        expected_ids={"workspace":r["workspace_id"],"tab":r["tab_id"],"pane":r["pane_id"]}
        if not isinstance(r["created"],list) or len(r["created"])!=3: raise BridgeError("complete receipt requires workspace, tab, and pane")
        actual={}
        for item in r["created"]:
            item=require_object(item,"created resource")
            if set(item)!={"kind","id"} or item.get("kind") not in expected_ids or item["kind"] in actual: raise BridgeError("invalid created resource")
            actual[item["kind"]]=require_string(item.get("id"),"created.id")
        if actual!=expected_ids: raise BridgeError("created resources do not match receipt")
    elif not isinstance(r["created"],list): raise BridgeError("partial created must be an array")
    return r

def _payload(r: dict[str,Any]) -> bytes: return (json.dumps(r,ensure_ascii=False,indent=2)+"\n").encode()

def write_new_receipt(path: Path, r: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); bridge_key()
    try: fd=os.open(path,_secure_flags(os.O_WRONLY|os.O_CREAT|os.O_EXCL),0o600)
    except FileExistsError as exc: raise BridgeError("receipt already exists") from exc
    try: os.write(fd,_payload(r)); os.fsync(fd); _check_regular(fd,private=True)
    finally: os.close(fd)

def replace_receipt(path: Path, r: dict[str,Any], expected_inode: tuple[int,int]|None=None) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(prefix=".receipt-",dir=path.parent)
    try:
        os.fchmod(fd,0o600); os.write(fd,_payload(r)); os.fsync(fd); os.close(fd); fd=-1
        if expected_inode:
            current=os.stat(path,follow_symlinks=False)
            if (current.st_dev,current.st_ino)!=expected_inode or not stat.S_ISREG(current.st_mode): raise BridgeError("receipt changed during update")
        os.replace(tmp,path); tmp=""
        if os.name != "nt":
            d=os.open(path.parent,os.O_RDONLY); os.fsync(d); os.close(d)
    finally:
        if fd>=0: os.close(fd)
        if tmp and os.path.exists(tmp): os.unlink(tmp)

def read_receipt(path: Path) -> dict[str,Any]:
    if path.is_symlink(): raise BridgeError("receipt symlink is not allowed")
    fd=os.open(path,_secure_flags(os.O_RDONLY))
    try:
        _check_regular(fd,private=True); chunks=[]
        while True:
            block=os.read(fd,65536)
            if not block: break
            chunks.append(block)
        return validate_receipt(json.loads(b"".join(chunks)))
    except (json.JSONDecodeError,UnicodeDecodeError) as exc: raise BridgeError(f"invalid receipt JSON: {exc}") from exc
    finally: os.close(fd)

def herdr_result(v: Any) -> dict[str,Any]:
    envelope=require_object(v,"Herdr response")
    if "result" not in envelope: raise ProtocolError("Herdr response is missing result")
    return require_object(envelope["result"],"Herdr result")

class HerdrClient:
    def __init__(self,binary=None): self.binary=binary or os.environ.get("HERDR_BIN","herdr")
    def call(self,*args: str)->dict[str,Any]:
        command=[self.binary,*args]
        if os.name == "nt":
            try:
                candidate=Path(self.binary)
                if candidate.is_file() and candidate.read_bytes()[:2] == b"#!":
                    command=[sys.executable,str(candidate),*args]
            except OSError:
                pass
        try: cp=subprocess.run(command,text=True,capture_output=True,check=False)
        except subprocess.TimeoutExpired as exc: raise HerdrTimeout(str(exc)) from exc
        except OSError as exc: raise CommandError(str(exc)) from exc
        if cp.returncode:
            message=cp.stderr.strip() or cp.stdout.strip() or "Herdr command failed"
            try: structured=json.loads(cp.stdout)
            except json.JSONDecodeError: structured=None
            if isinstance(structured,dict) and structured.get("error",{}).get("code") in {"timeout","deadline_exceeded"}: raise HerdrTimeout(message)
            raise CommandError(f"Herdr exited {cp.returncode}: {message}")
        try: return require_object(json.loads(cp.stdout),"Herdr response")
        except json.JSONDecodeError as exc: raise ProtocolError(f"Herdr returned invalid JSON: {exc}") from exc
    def result(self,*args: str)->dict[str,Any]: return herdr_result(self.call(*args))

def nested_id(v: Any,*keys: str)->str:
    cur=v
    for key in keys:
        if not isinstance(cur,dict) or key not in cur: raise ProtocolError(f"missing Herdr field {'.'.join(keys)}")
        cur=cur[key]
    try: return require_string(cur,".".join(keys))
    except BridgeError as exc: raise ProtocolError(str(exc)) from exc

def require_array(result:dict[str,Any],field:str)->list[Any]:
    value=result.get(field)
    if not isinstance(value,list): raise ProtocolError(f"Herdr result.{field} must be an array")
    return value

def discover_workspace(plan:dict[str,Any],client:HerdrClient)->str|None:
    if plan["space_mode"]=="topic": return None
    matches=[]
    for raw in require_array(client.result("workspace","list"),"workspaces"):
        wid=nested_id(require_object(raw,"workspace item"),"workspace_id")
        require_array(client.result("tab","list","--workspace",wid),"tabs")
        panes=require_array(client.result("pane","list","--workspace",wid),"panes")
        if not panes: raise ProtocolError(f"workspace {wid} has no pane cwd")
        cwds=set()
        for raw_pane in panes:
            pane=require_object(raw_pane,"pane item"); cwd=Path(require_string(pane.get("cwd"),"pane.cwd"))
            if not cwd.is_absolute(): raise ProtocolError("pane cwd must be absolute")
            try: cwds.add(str(cwd.resolve(strict=True)))
            except OSError as exc: raise ProtocolError("cannot verify pane cwd") from exc
        if plan["cwd"] in cwds: matches.append(wid)
    if len(matches)>1: raise BridgeError("multiple workspaces match cwd")
    return matches[0] if matches else None

def best_effort_readback(client:HerdrClient,agent_id:str)->None:
    for args in (("agent","get",agent_id),("agent","read",agent_id,"--source","recent-unwrapped")):
        try: client.call(*args)
        except (CommandError,ProtocolError): pass

def new_partial(plan:dict[str,Any])->dict[str,Any]:
    return {"schema":SCHEMA,"receipt_id":"hpc-"+secrets.token_hex(16),"receipt_mac":"pending","lifecycle_only":True,"partial":True,"closed":False,"command":"run","status":"partial","inconclusive":True,"timeout":False,"error_class":None,"error_message":None,"workspace_id":None,"workspace_created":False,"controller_tab_id":plan["controller_tab_id"],"controller_tab_label":plan["controller_tab_label"],"tab_id":None,"tab_label":plan["tab_label"],"pane_id":None,"agent_id":None,"created":[]}

def journal(path:Path,r:dict[str,Any],exists:bool)->dict[str,Any]:
    signed=sign_receipt(r)
    if exists: replace_receipt(path,signed)
    else: write_new_receipt(path,signed)
    return signed

def run_plan(plan:dict[str,Any],client:HerdrClient,receipt_name:str|None=None)->dict[str,Any]:
    if os.environ.get("HERDR_ENV")!="1": raise BridgeError("HERDR_ENV must be exactly 1")
    receipt=new_partial(plan); path=receipt_path(receipt_name or f"runs/{receipt['receipt_id']}.json"); exists=False
    try:
        client.call("tab","rename",plan["controller_tab_id"],plan["controller_tab_label"])
        wid=discover_workspace(plan,client); receipt["workspace_created"]=wid is None
        if wid is None:
            creation=client.result("workspace","create","--cwd",plan["cwd"],"--label",plan["topic"] if plan["space_mode"]=="topic" else Path(plan["cwd"]).name,"--no-focus")
            wid=nested_id(creation,"workspace","workspace_id")
        else:
            creation=client.result("tab","create","--workspace",wid,"--cwd",plan["cwd"],"--label",plan["tab_label"],"--no-focus")
        receipt.update(workspace_id=wid,created=[{"kind":"workspace","id":wid}])
        receipt=journal(path,receipt,exists); exists=True
        tab=nested_id(creation,"tab","tab_id")
        if tab==plan["controller_tab_id"]: raise ProtocolError("Herdr returned controller tab")
        receipt.update(tab_id=tab,created=[*receipt["created"],{"kind":"tab","id":tab}])
        receipt=journal(path,receipt,exists)
        pane=nested_id(creation,"root_pane","pane_id")
        receipt.update(pane_id=pane,created=[*receipt["created"],{"kind":"pane","id":pane}])
        receipt=journal(path,receipt,exists)
        started=client.result("agent","start",plan["agent_name"],"--kind","pi","--pane",pane)
        agent=nested_id(started,"agent","name") if isinstance(started.get("agent"),dict) else nested_id(started,"agent_name")
        if agent!=plan["agent_name"]: raise ProtocolError("agent start returned inconsistent ID")
        receipt["agent_id"]=agent; receipt=journal(path,receipt,exists)
        client.call("agent","prompt",agent,plan["prompt"])
        status= nested_id(client.result("agent","wait",agent,"--timeout",str(plan["timeout_seconds"]*1000)),"status")
        if status not in {"done","idle","blocked","unknown"}: raise ProtocolError(f"unsupported wait status: {status}")
        receipt.update(partial=False,status=status,inconclusive=status in {"blocked","unknown"})
        if receipt["inconclusive"]: best_effort_readback(client,agent)
        return journal(path,receipt,exists)
    except HerdrTimeout as exc:
        receipt.update(status="timeout",inconclusive=True,timeout=True,error_class=type(exc).__name__,error_message=str(exc))
        if receipt.get("agent_id"): best_effort_readback(client,receipt["agent_id"])
        receipt["partial"]=False
        receipt=journal(path,receipt,exists); return receipt
    except (CommandError,ProtocolError,BridgeError,OSError) as exc:
        receipt.update(status="protocol_error" if isinstance(exc,ProtocolError) else "command_failed",inconclusive=True,error_class=type(exc).__name__,error_message=str(exc),partial=True)
        if receipt.get("agent_id"): best_effort_readback(client,receipt["agent_id"])
        if receipt.get("workspace_id"): receipt=journal(path,receipt,exists)
        raise

def verify_close_topology(r:dict[str,Any],client:HerdrClient)->None:
    tabs=require_array(client.result("tab","list","--workspace",r["workspace_id"]),"tabs")
    matches=[require_object(x,"tab item") for x in tabs if isinstance(x,dict) and x.get("tab_id")==r["tab_id"]]
    if len(matches)!=1 or matches[0].get("label")!=r["tab_label"]: raise BridgeError("execution tab topology mismatch")
    panes=require_array(client.result("pane","list","--workspace",r["workspace_id"]),"panes")
    if len([x for x in panes if isinstance(x,dict) and x.get("pane_id")==r["pane_id"] and x.get("tab_id")==r["tab_id"]])!=1: raise BridgeError("execution pane topology mismatch")
    agent=client.result("agent","get",r["agent_id"]); name=nested_id(agent,"agent","name"); pane=nested_id(agent,"agent","pane_id")
    if name!=r["agent_id"] or pane!=r["pane_id"]: raise BridgeError("agent topology mismatch")

def close_file(value:str,client:HerdrClient,accepted:Any)->dict[str,Any]:
    if accepted is not True: raise BridgeError("close requires accepted=true")
    path=receipt_path(value)
    if path.is_symlink(): raise BridgeError("receipt symlink is not allowed")
    fd=os.open(path,_secure_flags(os.O_RDWR))
    if fcntl is not None:
        fcntl.flock(fd,fcntl.LOCK_EX)
    else:
        import msvcrt
        os.lseek(fd,0,os.SEEK_SET); msvcrt.locking(fd,msvcrt.LK_LOCK,1)
    try:
        info=_check_regular(fd,private=True); current=os.stat(path,follow_symlinks=False)
        if (info.st_dev,info.st_ino)!=(current.st_dev,current.st_ino): raise BridgeError("receipt changed while locking")
        data=b""
        while True:
            chunk=os.read(fd,65536)
            if not chunk: break
            data+=chunk
        r=validate_receipt(json.loads(data))
        if r["partial"]: raise BridgeError("partial receipt requires manual recovery")
        if r["closed"]: raise BridgeError("receipt is already closed")
        verify_close_topology(r,client); client.call("tab","rename",r["tab_id"],"完成-"+r["tab_label"]); client.call("tab","close",r["tab_id"])
        updated=dict(r); updated["closed"]=True; updated=sign_receipt(updated); validate_receipt(updated)
        if os.name == "nt":
            os.close(fd); fd=-1
        replace_receipt(path,updated,(info.st_dev,info.st_ino)); return updated
    except (json.JSONDecodeError,UnicodeDecodeError) as exc: raise BridgeError(f"invalid receipt JSON: {exc}") from exc
    finally:
        if fd >= 0: os.close(fd)

def main(argv=None)->int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("command",choices=["plan","run","close"]); p.add_argument("--input"); p.add_argument("--receipt"); p.add_argument("--accepted",action="store_true"); a=p.parse_args(argv)
    try:
        if a.command=="close":
            if not a.receipt: raise BridgeError("close requires --receipt")
            out=close_file(a.receipt,HerdrClient(),a.accepted)
        else:
            if not a.input: raise BridgeError("--input is required")
            raw=Path(a.input[1:]).read_text() if a.input.startswith("@") else a.input; value=json.loads(raw); out=validate_plan(value) if a.command=="plan" else run_plan(validate_plan(value),HerdrClient(),a.receipt)
        print(json.dumps(out,ensure_ascii=False,indent=2)); return 0
    except (BridgeError,CommandError,ProtocolError,OSError,TypeError,AttributeError,KeyError,json.JSONDecodeError) as exc:
        print(json.dumps({"error":type(exc).__name__,"message":str(exc)},ensure_ascii=False),file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
