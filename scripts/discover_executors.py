#!/usr/bin/env python3
"""discover_executors — probe installed coding-agent CLIs for live health.

Pattern borrowed from the agent-sop skill (MIT): the candidate list below is an
*extensible detection list*, not a roster. It defines stable capability and
transport facts; each run reports which listed CLIs exist, which models their
native catalogs advertise, and whether the transports respond.

Run by the current controller Agent at task start, before routing:

    python3 scripts/discover_executors.py [--probe] [--out wp-state/roster.json]

--probe runs a minimal non-interactive call per executor (timeout 60s) so a
transport failure shows up as "degraded" *before* work is assigned, instead of
stalling a pipeline later. Probes run through an interactive shell (`bash -ic`)
so they see the same runtime environment as a real dispatched runner.
Executors with no known non-interactive probe recipe report health
"unknown" under --probe; verify those manually before assigning.

Without --probe, health is at most "available" (present and reports a version).

Never prints credentials, tokens, or key material.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from model_resolver import EXECUTOR_MODELS, parse_model_catalog

# Detection list: executable -> static transport/model metadata + probe recipe.
# Legacy tier fields in emitted rosters are derived from these capability facts.
CANDIDATES = {
    "claude": {
        "vendor": "anthropic",
        "default_capability": "balanced",
        "models": EXECUTOR_MODELS["claude"],
        "transport": {
            "resumable": False,
            "headless": True,
            "tools": True,
            "native_goal": False,
            "model_discovery": False,
            "single_shot": True,
            "bounded_agent": True,
        },
        "version_args": ["--version"],
        "probe_args": ["-p", "reply with exactly: ok"],
        "background_recipe": "IS_SANDBOX=1 claude --dangerously-skip-permissions",
    },
    "codex": {
        "vendor": "openai",
        "default_capability": "hard",
        "models": EXECUTOR_MODELS["codex"],
        "transport": {
            "resumable": True,
            "headless": True,
            "tools": True,
            "native_goal": True,
            "model_discovery": True,
            "single_shot": False,
            "bounded_agent": False,
        },
        "version_args": ["--version"],
        "probe_args": ["exec", "--skip-git-repo-check", "reply with exactly: ok"],
        "background_recipe": "codex in a bypass/full-auto approval+sandbox mode (never interactive default)",
    },
    "pi": {
        "vendor": "multi",
        "default_capability": "balanced",
        "models": {"fast": [], "balanced": [], "hard": []},
        "transport": {
            "resumable": False,
            "headless": True,
            "tools": True,
            "native_goal": False,
            "model_discovery": False,
            "single_shot": True,
            "bounded_agent": True,
        },
        "version_args": ["--version"],
        "probe_args": ["-p", "--no-session", "reply with exactly: ok"],
        "background_recipe": "pi (auto-approves by default)",
    },
    "grok": {
        "vendor": "xai",
        "default_capability": "hard",
        "models": EXECUTOR_MODELS["grok"],
        "transport": {
            "resumable": False,
            "headless": True,
            "tools": True,
            "native_goal": False,
            "model_discovery": True,
            "single_shot": True,
            "bounded_agent": True,
        },
        "version_args": ["--version"],
        "probe_args": ["-p", "reply with exactly: ok"],
        "model_discovery_args": ["models"],
        "background_recipe": (
            "grok --prompt-file <absolute-path> --max-turns 8 --output-format plain "
            "--no-memory --no-subagents --cwd <execution-root>"
        ),
    },
    "kimi": {
        "vendor": "moonshot",
        "default_capability": "balanced",
        "models": EXECUTOR_MODELS["kimi"],
        "transport": {
            "resumable": False,
            "headless": True,
            "tools": True,
            "native_goal": False,
            "model_discovery": False,
            "single_shot": True,
            "bounded_agent": True,
        },
        "version_args": ["--version"],
        "probe_args": ["-p", "reply with exactly: ok"],
        "background_recipe": "kimi -p (non-interactive prompt mode; tool calls auto-approved)",
    },
    "agy": {
        "vendor": "google",
        "default_capability": "balanced",
        "models": EXECUTOR_MODELS["agy"],
        "transport": {
            "resumable": False,
            "headless": True,
            "tools": True,
            "native_goal": False,
            "model_discovery": False,
            "single_shot": True,
            "bounded_agent": True,
        },
        "version_args": ["--version"],
        "probe_args": ["-p", "reply with exactly: ok", "--dangerously-skip-permissions"],
        "background_recipe": "agy -p <prompt> --dangerously-skip-permissions",
    },
}

VERSION_TIMEOUT = 10
PROBE_TIMEOUT = 60
SKILL_ROOT = Path(__file__).resolve().parent.parent


# Some CLIs render plain responses with a list bullet (e.g. Kimi prints
# "• ok"). Strip common list prefixes before matching the expected answer.
_LIST_PREFIXES = ("•", "-", "*", ">")


def _strip_list_prefix(line: str) -> str:
    stripped = line.strip()
    for prefix in _LIST_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def run(cmd: list[str], timeout: int, output_limit: int = 200) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()[:output_limit]
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s"
    except OSError as exc:
        return -2, str(exc)[:200]


def discover(do_probe: bool) -> dict:
    agents = []
    for name, meta in CANDIDATES.items():
        path = shutil.which(name)
        if not path:
            continue
        entry = {
            "name": name,
            "path": path,
            "vendor": meta["vendor"],
            "tier": meta["default_capability"],
            "tiers_available": list(meta["models"]),
            "transport": dict(meta["transport"]),
            "models": {key: list(value) for key, value in meta["models"].items()},
            "models_discovered": [],
            "background_recipe": meta["background_recipe"],
            "version": None,
            "health": "available",
            "probe_ms": None,
            "notes": [],
        }
        rc, out = run([path, *meta["version_args"]], VERSION_TIMEOUT)
        if rc == 0 and out:
            entry["version"] = out.splitlines()[0][:80]
        else:
            entry["health"] = "degraded"
            entry["notes"].append(f"version check failed: {out}")

        if do_probe and meta["probe_args"] and entry["health"] == "available":
            # Probe through an interactive shell so the agent sees the same
            # profile-provided environment that a real dispatched runner gets.
            # A bare subprocess can lack that env and report false degradation.
            probe_cmd = [path, *meta["probe_args"]]
            bash = shutil.which("bash")
            if bash:
                probe_cmd = [bash, "-ic", shlex.join(probe_cmd)]
            t0 = time.monotonic()
            rc, out = run(probe_cmd, PROBE_TIMEOUT)
            entry["probe_ms"] = int((time.monotonic() - t0) * 1000)
            ok_lines = {"ok", "ok."}
            answered = rc == 0 and any(
                _strip_list_prefix(line).lower() in ok_lines for line in out.splitlines()
            )
            if not answered:
                entry["health"] = "degraded"
                entry["notes"].append(f"probe failed (rc={rc}): {out[:120]}")
        elif do_probe and not meta["probe_args"]:
            entry["health"] = "unknown"
            entry["notes"].append("no non-interactive probe known; verify manually")

        discovery_args = meta.get("model_discovery_args")
        if do_probe and discovery_args and entry["health"] != "degraded":
            # Run through the same interactive shell as transport probes so a
            # catalog that depends on profile-sourced environment (auth or
            # model routing) sees the same runtime a dispatched runner gets.
            discovery_cmd = [path, *discovery_args]
            bash = shutil.which("bash")
            if bash:
                discovery_cmd = [bash, "-ic", shlex.join(discovery_cmd)]
            rc, out = run(discovery_cmd, PROBE_TIMEOUT, output_limit=10000)
            if rc == 0:
                entry["models_discovered"] = parse_model_catalog(out)
            else:
                entry["notes"].append(
                    f"model discovery failed (rc={rc}): {out[:120]}"
                )

        agents.append(entry)

    return {
        "generated_by": "wp/scripts/discover_executors.py",
        "probed": do_probe,
        "host": "local",
        "agents": agents,
    }


def skill_output_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = SKILL_ROOT / candidate
    resolved = candidate.resolve()
    if SKILL_ROOT.resolve() not in (resolved, *resolved.parents):
        raise ValueError("--out must be inside the wp skill directory")
    return resolved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--probe",
        action="store_true",
        help="run live transport and model-discovery probes when supported",
    )
    ap.add_argument("--out", help="also write roster JSON to this path")
    args = ap.parse_args()

    roster = discover(args.probe)
    text = json.dumps(roster, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        try:
            output_path = skill_output_path(args.out)
        except ValueError as exc:
            print(f"discover_executors: {exc}", file=sys.stderr)
            sys.exit(2)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            f.write(text + "\n")
    if not roster["agents"]:
        print("WARNING: no agent CLIs found on PATH", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
