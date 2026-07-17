from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
INTAKE: Final = ROOT / "scripts" / "repo_issue_intake.sh"
DISPATCHER: Final = ROOT / "scripts" / "repo_issue_to_pr_dispatch.sh"
TRIAGE: Final = ROOT / "scripts" / "repo_pr_triage.sh"


class IssueToPrToMergeContractTests(unittest.TestCase):
    """End-to-end local issue -> OMP -> PR -> guarded merge lifecycle."""

    def test_issue_42_merges_closes_releases_then_issue_43(self) -> None:
        with self._fixture() as fx:
            self._intake(fx)
            self._dispatch(fx)
            self._dispatch(fx, run_omp=True)
            self._triage(fx)
            self._assert_completed(fx, 42)
            self.assertEqual(1, fx.state["merge_count"])
            self.assertEqual(1, fx.state["close_count"])

            # Every rerun is a no-op after the terminal receipt and release.
            self._triage(fx)
            self._intake(fx)
            self.assertEqual(1, fx.state["merge_count"])
            self.assertEqual(1, fx.state["close_count"])
            self.assertEqual(43, fx.claim()["issue"])
            self.assertTrue(any("#43" in task["title"] for task in fx.state["tasks"] if task["status"] != "done"))

    def test_recovery_after_merge_before_receipt(self) -> None:
        with self._fixture(fault="after-merge") as fx:
            self._intake(fx)
            self._dispatch(fx)
            self._dispatch(fx, run_omp=True)
            first = self._triage(fx)
            self.assertNotEqual(0, first.returncode)
            self.assertEqual(1, fx.state["merge_count"])
            self.assertEqual("OPEN", fx.state["issues"]["42"]["state"])
            self.assertEqual(42, fx.claim()["issue"])
            second = self._triage(fx)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self._assert_completed(fx, 42)
            self.assertEqual(1, fx.state["merge_count"])
            self.assertEqual(1, fx.state["close_count"])

    def test_recovery_after_close_before_terminal_receipt(self) -> None:
        with self._fixture(fault="after-close") as fx:
            self._intake(fx)
            self._dispatch(fx)
            self._dispatch(fx, run_omp=True)
            first = self._triage(fx)
            self.assertNotEqual(0, first.returncode)
            self.assertEqual("CLOSED", fx.state["issues"]["42"]["state"])
            self.assertEqual(1, fx.state["merge_count"])
            self.assertEqual(1, fx.state["close_count"])
            self.assertEqual(42, fx.claim()["issue"])
            second = self._triage(fx)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self._assert_completed(fx, 42)
            self.assertEqual(1, fx.state["merge_count"])
            self.assertEqual(1, fx.state["close_count"])

    def test_recovery_after_terminal_receipt_before_release(self) -> None:
        with self._fixture() as fx:
            self._intake(fx)
            self._dispatch(fx)
            self._dispatch(fx, run_omp=True)
            self._triage(fx)
            self._assert_completed(fx, 42)
            receipt = next(fx.receipts.glob("*.json"))
            payload = json.loads(receipt.read_text())
            payload["phase"] = "ISSUE_CLOSED_CONFIRMED"
            receipt.write_text(json.dumps(payload) + "\n")
            fx.active.parent.mkdir(parents=True, exist_ok=True)
            fx.active.mkdir()
            claim = {"version": 1, "repo": "owner/repo", "issue": 42, "board": "board", "claimedAt": "2024-01-01T00:00:00Z"}
            (fx.active / "claim.json").write_text(json.dumps(claim) + "\n")
            result = self._triage(fx)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse(fx.active.exists())
            self.assertEqual(1, fx.state["merge_count"])
            self.assertEqual(1, fx.state["close_count"])

    def _fixture(self, fault: str = "") -> "Fixture":
        return Fixture(fault)

    def _run(self, script: Path, fx: "Fixture", *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(script), *args], cwd=ROOT, env=fx.env, text=True, capture_output=True, check=False)

    def _intake(self, fx: "Fixture") -> None:
        result = self._run(INTAKE, fx, "--live", "--limit", "10")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        fx.refresh()

    def _dispatch(self, fx: "Fixture", run_omp: bool = False) -> None:
        args = ["--live", "--max", "1"]
        if run_omp:
            args.append("--run-opencode")
        for _ in range(120):
            result = self._run(DISPATCHER, fx, *args)
            fx.refresh()
            fix_tasks = [t for t in fx.state.get("tasks", []) if t.get("id", "").startswith("task-fix-")]
            if not run_omp or any(t.get("status") == "done" for t in fix_tasks):
                if run_omp:
                    for _ in range(120):
                        fx.refresh()
                        if fx.state.get("prs") and not (fx.root / "worktrees" / "board" / ".agent.lock").exists():
                            break
                        time.sleep(0.1)
                break
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        fx.refresh()

    def _triage(self, fx: "Fixture") -> subprocess.CompletedProcess[str]:
        for _ in range(120):
            fx.refresh()
            if fx.state.get("prs") and not (fx.root / "worktrees" / "board" / ".agent.lock").exists():
                break
            time.sleep(0.1)
        result = self._run(TRIAGE, fx, "--live")
        fx.refresh()
        return result

    def _refresh(self, fx: "Fixture") -> None:
        fx.refresh()

    def _assert_completed(self, fx: "Fixture", issue: int) -> None:
        self._refresh(fx)
        self.assertEqual("CLOSED", fx.state["issues"][str(issue)]["state"])
        self.assertFalse(fx.active.exists())
        receipt = next(fx.receipts.glob("*.json"))
        payload = json.loads(receipt.read_text())
        self.assertEqual("ISSUE_CLOSED_CONFIRMED", payload["phase"])
        merge_sha = payload["mergeSha"]
        origin_main = subprocess.check_output(["git", "--git-dir", str(fx.origin), "rev-parse", "refs/heads/main"], text=True).strip()
        self.assertEqual(origin_main, payload["originMainSha"])
        subprocess.run(["git", "--git-dir", str(fx.origin), "merge-base", "--is-ancestor", merge_sha, origin_main], check=True)
        self.assertEqual(merge_sha, fx.state["prs"]["17"]["merge_sha"])
        worktrees = subprocess.check_output(["git", "-C", str(fx.clone), "worktree", "list", "--porcelain"], text=True)
        self.assertNotIn("task-fix-42", worktrees)


class Fixture:
    def __init__(self, fault: str = "") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.origin = self.root / "origin.git"
        self.clone = self.root / "clone"
        self._make_git()
        self.active = self.root / "active"
        self.receipts = self.root / "triage-receipts"
        self.dispatch_receipts = self.root / "dispatch-receipts"
        self.state_path = self.root / "state.json"
        self.calls = self.root / "calls.log"
        self.repos = self.root / "repos.txt"
        self.repos.write_text(f"owner/repo|board|{self.clone}|42\n")
        self.state = {
            "fault": fault,
            "fault_used": False,
            "issues": {
                "42": {"state": "OPEN", "title": "Broken issue", "assignees": ["owner"], "labels": ["ai:ready"]},
                "43": {"state": "OPEN", "title": "Next issue", "assignees": ["owner"], "labels": ["ai:ready"]},
            },
            "tasks": [],
            "prs": {},
            "merge_count": 0,
            "close_count": 0,
        }
        self._save()
        self._write_fake_hermes()
        self._write_fake_gh()
        self._write_fake_omp()
        self.bash_env = self.root / "bash_env"
        self.bash_env.write_text(f'gh() {{ "{self.bin / "gh"}" "$@"; }}\nhermes() {{ "{self.bin / "hermes"}" "$@"; }}\n')
        base = os.environ.copy()
        base.update({
            "BASH_ENV": str(self.bash_env),
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HERMES_REPO_AGENT_REPOS_FILE": str(self.repos),
            "HERMES_REPO_AGENT_TEST_FIXTURE": "1",
            "HERMES_REPO_AGENT_ACTIVE_ISSUE_DIR": str(self.active),
            "HERMES_REPO_AGENT_SOURCE": "kanban",
            "HERMES_REPO_AGENT_LABEL_READY": "ai:ready",
            "HERMES_REPO_AGENT_LABEL_IN_PROGRESS": "ai:in-progress",
            "HERMES_REPO_AGENT_LABEL_BLOCKED": "ai:blocked",
            "HERMES_REPO_AGENT_LABEL_PR_OPENED": "ai:pr-opened",
            "HERMES_REPO_AGENT_LABEL_GENERATED": "ai:generated",
            "HERMES_REPO_AGENT_ASSIGNEE": "owner",
            "HERMES_KANBAN_INTAKE_ASSIGNEE": "owner",
            "HERMES_INTAKE_ASSIGNEE": "owner",
            "HERMES_KANBAN_FIXER_ASSIGNEE": "owner",
            "HERMES_INTAKE_LOG": str(self.root / "intake.log"),
            "HERMES_INTAKE_LOCK_DIR": str(self.root / "intake.lock"),
            "HERMES_ISSUE_TO_PR_LOG": str(self.root / "dispatch.log"),
            "HERMES_ISSUE_TO_PR_LOCK_DIR": str(self.root / "dispatch.lock"),
            "HERMES_WORKTREE_ROOT": str(self.root / "worktrees"),
            "HERMES_REPO_AGENT_RECEIPT_DIR": str(self.dispatch_receipts),
            "HERMES_OMP_TIMEOUT_SECONDS": "10",
            "HERMES_PR_TRIAGE_LOG": str(self.root / "triage.log"),
            "HERMES_PR_TRIAGE_LOCK_DIR": str(self.root / "triage.lock"),
            "HERMES_PR_TRIAGE_MERGE_RECEIPT_DIR": str(self.receipts),
            "HERMES_PR_AUTOMERGE": "1",
            "HERMES_PR_ALLOW_NO_CHECKS": "1",
            "HERMES_PR_REQUIRE_APPROVED": "0",
            "HERMES_PR_REQUIRE_TEST_EVIDENCE": "0",
            "STATE_FILE": str(self.state_path),
            "CALLS_FILE": str(self.calls),
            "ORIGIN_DIR": str(self.origin),
            "CLONE_DIR": str(self.clone),
        })
        self.env = base

    def __enter__(self) -> "Fixture":
        return self

    def __exit__(self, *_: object) -> None:
        self._tmp.cleanup()

    def refresh(self) -> None:
        self.state = json.loads(self.state_path.read_text())

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, sort_keys=True) + "\n")

    def _make_git(self) -> None:
        subprocess.run(["git", "init", "--bare", str(self.origin)], check=True, capture_output=True)
        seed = self.root / "seed"
        subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
        (seed / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "base"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(self.origin)], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.clone), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.clone), "config", "user.name", "Test"], check=True)

    def _write_fake_hermes(self) -> None:
        self._write_exec(self.bin / "hermes", '''#!/usr/bin/env python3
import json, os, sys
p=os.environ["STATE_FILE"]
s=json.load(open(p)); a=sys.argv
if "list" in a and "--json" in a:
    print(json.dumps(s["tasks"])); raise SystemExit
if "create" in a:
    i=a.index("create"); title=a[i+1]; key=a[a.index("--idempotency-key")+1] if "--idempotency-key" in a else title
    if not any(t.get("key")==key for t in s["tasks"]):
        task={"id":("gh-issue-" if title.startswith("[issue]") else "task-fix-")+title.split("#",1)[1].split(":",1)[0],"title":title,"status":"ready","key":key,"body":"","assignee":"owner","source":"kanban"}
        if "--branch" in a: task["branch_name"]=a[a.index("--branch")+1]
        if "--workspace" in a: task["workspace"]={"path":a[a.index("--workspace")+1].split(":",1)[-1]}
        s["tasks"].append(task)
    json.dump(s,open(p,"w")); raise SystemExit
if "complete" in a:
    tid=a[a.index("complete")+1]
    for t in s["tasks"]:
        if t["id"]==tid: t["status"]="done"
    json.dump(s,open(p,"w")); raise SystemExit
print("[]")
''')

    def _write_fake_omp(self) -> None:
        self._write_exec(self.bin / "omp", '''#!/usr/bin/env python3
import os, subprocess, sys
args=sys.argv; wt=args[args.index("--cwd")+1]; branch=subprocess.check_output(["git","-C",wt,"branch","--show-current"],text=True).strip()
subprocess.run(["git","-C",wt,"config","user.email","test@example.invalid"],check=True); subprocess.run(["git","-C",wt,"config","user.name","Test"],check=True)
with open(os.path.join(wt,"README.md"),"a") as f: f.write("fix\\n")
subprocess.run(["git","-C",wt,"add","README.md"],check=True); subprocess.run(["git","-C",wt,"commit","-m","fix"],check=True,stdout=subprocess.DEVNULL)
subprocess.run(["git","-C",wt,"push","-u","origin",branch],check=True,stdout=subprocess.DEVNULL)
subprocess.run(["gh","pr","create","--repo","owner/repo","--head",branch,"--base","main","--title","Broken issue","--body","Fixes #42"],check=True,stdout=subprocess.DEVNULL)
''')

    def _write_fake_gh(self) -> None:
        self._write_exec(self.bin / "gh", '''#!/usr/bin/env python3
import json, os, subprocess, tempfile, datetime
p=os.environ["STATE_FILE"]; s=json.load(open(p)); a=os.sys.argv[1:]
def save(): json.dump(s,open(p,"w"))
def arg(name, default=""):
    return a[a.index(name)+1] if name in a else default
def positional(command):
    try: return a[a.index(command)+1]
    except (ValueError, IndexError): return ""
def pr_obj(pr):
    return s["prs"][str(pr)]
def emit(value, jq=""):
    if isinstance(value,str): print(value)
    elif jq==".state": print(value.get("state",""))
    elif jq==".object.sha": print(value.get("sha",""))
    elif "assignees" in jq: print(",".join(x.get("login","") if isinstance(x,dict) else str(x) for x in value.get("assignees",[])))
    elif jq==".[].name":
        for x in value: print(x.get("name","") if isinstance(x,dict) else x)
    elif jq: print(json.dumps(value))
    else: print(json.dumps(value))
if a[:2]==["label","list"]: emit(["ai:ready","ai:generated","ai:pr-opened"]); raise SystemExit
if a[:2]==["issue","list"]:
    for n,v in s["issues"].items():
        if v["state"]=="OPEN": print(f"{n}\\t{v['title']}\\thttps://example.invalid/issues/{n}\\t{','.join(v['labels'])}\\t{','.join(v['assignees'])}\\ttrue")
    raise SystemExit
if a[:2]==["issue","view"]:
    n=positional("view"); v=s["issues"].get(n,{})
    if any("assignees" in x for x in a):
        jq=arg("--jq"); vals=v.get("assignees",[]); emit({"assignees":[{"login":x} for x in vals]} if "join" not in jq else ",".join(vals),jq)
    else: emit({"state":v.get("state","UNKNOWN")},arg("--jq"))
    raise SystemExit
if a[:2]==["issue","edit"]: raise SystemExit
if a[:2]==["issue","close"]:
    n=positional("close"); v=s["issues"][n]
    if v["state"]=="OPEN":
        v["state"]="CLOSED"; s["close_count"]+=1; save()
        if s.get("fault")=="after-close" and not s.get("fault_used"): s["fault_used"]=True; save(); raise SystemExit(91)
    raise SystemExit
if a[:2]==["api"]:
    ref=a[1].split("refs/heads/")[-1] if "refs/heads/" in a[1] else ""
    out=subprocess.check_output(["git","--git-dir",os.environ["ORIGIN_DIR"],"rev-parse","refs/heads/"+ref],text=True).strip(); emit({"sha":out},arg("--jq")); raise SystemExit
if a[:2]==["pr","create"]:
    b=arg("--head"); head=subprocess.check_output(["git","--git-dir",os.environ["ORIGIN_DIR"],"rev-parse","refs/heads/"+b],text=True).strip(); base=subprocess.check_output(["git","--git-dir",os.environ["ORIGIN_DIR"],"rev-parse","refs/heads/main"],text=True).strip()
    s["prs"]["17"]={"number":17,"title":"Broken issue","url":"https://example.invalid/pr/17","headRefName":b,"baseRefName":"main","headRefOid":head,"baseRefOid":base,"isDraft":False,"mergeStateStatus":"CLEAN","reviewDecision":None,"labels":[{"name":"ai:generated"},{"name":"ai:pr-opened"}],"author":{"login":"owner"},"state":"OPEN","closingIssuesReferences":[{"number":42}],"merge_sha":""}; save(); print("https://example.invalid/pr/17"); raise SystemExit
if a[:2]==["pr","list"]:
    if "--head" in a:
        b=arg("--head"); out=[{"number":17,"url":"https://example.invalid/pr/17"}] if s["prs"].get("17",{}).get("headRefName")==b and s["prs"]["17"].get("state")=="OPEN" else []
    else: out=[pr_obj(17)] if s["prs"].get("17",{}).get("state")=="OPEN" else []
    print(json.dumps(out)); raise SystemExit
if a[:2]==["pr","view"]:
    n=positional("view"); v=dict(pr_obj(n));
    if v.get("state")=="MERGED": v.update({"mergeCommit":{"oid":v["merge_sha"]},"mergedAt":v["merged_at"]})
    emit(v,arg("--jq")); raise SystemExit
if a[:2]==["pr","checks"]: print("[]"); raise SystemExit
if a[:2]==["pr","edit"]: raise SystemExit
if a[:2]==["pr","merge"]:
    n=positional("merge"); v=pr_obj(n); expected=arg("--match-head-commit");
    if expected!=v["headRefOid"]: raise SystemExit(2)
    temp=tempfile.mkdtemp(); subprocess.run(["git","clone",os.environ["ORIGIN_DIR"],temp],check=True,stdout=subprocess.DEVNULL); subprocess.run(["git","-C",temp,"config","user.email","test@example.invalid"],check=True); subprocess.run(["git","-C",temp,"config","user.name","Test"],check=True); subprocess.run(["git","-C",temp,"checkout","main"],check=True,stdout=subprocess.DEVNULL); subprocess.run(["git","-C",temp,"merge","--no-ff","origin/"+v["headRefName"],"-m","Merge PR #17"],check=True,stdout=subprocess.DEVNULL); subprocess.run(["git","-C",temp,"push","origin","main"],check=True,stdout=subprocess.DEVNULL); sha=subprocess.check_output(["git","-C",temp,"rev-parse","HEAD"],text=True).strip(); v.update({"state":"MERGED","merge_sha":sha,"merged_at":datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z")}); s["merge_count"]+=1; save()
    if s.get("fault")=="after-merge" and not s.get("fault_used"): s["fault_used"]=True; save(); raise SystemExit(91)
    raise SystemExit
raise SystemExit
''')

    def _write_exec(self, path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o700)

    def claim(self) -> dict[str, object]:
        return json.loads((self.active / "claim.json").read_text())


if __name__ == "__main__":
    unittest.main()
