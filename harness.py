"""Host gates and explicit candidate-diff policy, not an execution sandbox."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent


def run(*args):
    subprocess.run(args, cwd=ROOT, check=True)


def candidate_gate(base):
    policy = json.loads((ROOT / "policy.json").read_text())
    names = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", base, "--"],
        cwd=ROOT, text=True).splitlines()
    patch = subprocess.check_output(["git", "diff", base, "--"], cwd=ROOT)
    if len(names) > policy["max_changed_files"]:
        raise ValueError("candidate file-count limit")
    if len(patch) > policy["max_patch_bytes"]:
        raise ValueError("candidate patch-size limit")
    if any(name not in policy["candidate_files"] for name in names):
        raise ValueError("candidate touches protected or unapproved paths")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"check", "candidate"}:
        raise SystemExit("usage: python3 harness.py check | candidate BASE_SHA")
    tree = ast.parse((ROOT / "transfer.py").read_text())
    allowed = {"struct", "typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(n.name in allowed for n in node.names), "layer violation"
        if isinstance(node, ast.ImportFrom):
            assert node.module in allowed, "layer violation"
    run(sys.executable, "-m", "compileall", "-q", "transfer.py", "quick_mode.py")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    if suite.countTestCases() < 8:
        raise ValueError("test discovery found fewer than eight baseline tests")
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    if sys.argv[1] == "candidate":
        if len(sys.argv) != 3 or not sys.argv[2] or sys.argv[2].startswith("-"):
            raise SystemExit("candidate requires a base commit")
        candidate_gate(sys.argv[2])
    print("Host gates passed. Hardware and human review not verified.")


if __name__ == "__main__":
    main()
