#!/usr/bin/env python3
"""Check staged Git blobs or an explicit public tree for privacy.
Usage: python3 scripts/public_privacy_check.py [--tree REF] [--prefix DIR]
Only Git blobs are read; findings report paths/categories, never secret values.
"""
import argparse
import re
import subprocess
import sys

def git(*args):
    return subprocess.check_output(["git", *args])
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--tree", help="Inspect every tracked blob in this committed tree")
parser.add_argument("--prefix", default="", help="Restrict to an explicit public export directory")
args = parser.parse_args()
patterns = {
    "private-home": re.compile(r"/home/(?!demo/|test/|alice/|<user>|\[)[A-Za-z0-9_.-]+|[A-Z]:\\Users\\(?!<user>|account|demo)[A-Za-z0-9_.-]+"),
    "machine-root": re.compile("/" + r"lzcapp/"),
    "private-host": re.compile(r"heiyu" + r"\.space|host" + r"\.lzcapp|192\.168\.[0-9]+\.[0-9]+"),
    "token": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,}"),
    "credential-url": re.compile(r"https?://[^/\s]*:[^/@\s]+@"),
}
if args.tree:
    paths = git("ls-tree", "-r", "--name-only", "-z", args.tree).split(b"\0")
else:
    paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
failed = False
checked = 0
for raw in paths:
    if not raw:
        continue
    name = raw.decode("utf-8")
    if args.prefix and not name.startswith(args.prefix.rstrip("/") + "/"):
        continue
    checked += 1
    parts = name.split("/")
    forbidden = name.endswith((".key", ".pem", ".log", ".bundle")) or any(
        p in {"node_modules", "__pycache__", "wp-state", "wp-custom", ".agent"} or
        (p.startswith(".env") and p != ".env.example") for p in parts)
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", name]).returncode == 0
    if forbidden or ignored:
        print(f"FAIL {name}: ignored/local-only tracked file")
        failed = True
    blob = git("show", (args.tree + ":" if args.tree else ":") + name)
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        print(f"FAIL {name}: binary requires explicit publication review")
        failed = True
        continue
    for label, regex in patterns.items():
        if regex.search(text):
            print(f"FAIL {name}: {label}")
            failed = True
    # Headers split across test string literals are not actual private keys.
    if re.search("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----", text):
        print(f"FAIL {name}: private-key-header")
        failed = True
print(f"Privacy {'FAIL' if failed else 'PASS'}: {checked} Git blobs; targeted patterns only, manual diff review required")
sys.exit(1 if failed else 0)
