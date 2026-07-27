"""Durability contracts for canonical receipt lifecycle atoms."""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from lokay.steps import cleanup, issue_to_pr, triage
PROVENANCE = {"source":"github_pr_readback","state":"MERGED","repo":"owner/repo","number":7,"head_oid":"head-7","head_ref":"ai/fix/7","merge_oid":"merge-7","merged_at":"2026-01-01T00:00:00Z"}
def request(data: dict, conduction: dict | None = None, *, config: dict | None = None) -> dict:
    return {"input": {**data, "conduction": conduction or {}}, "config": config or {}}
class ReceiptDurabilityTests(unittest.TestCase):
    def dispatch(self, path, payload, dry_run=False):
        b=issue_to_pr.build_dispatch_receipt(request({"receipt_path":str(path),"payload":payload,"dry_run":dry_run})); p=issue_to_pr.publish_dispatch_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"build_dispatch_receipt":b})); v=issue_to_pr.verify_dispatch_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"build_dispatch_receipt":b,"publish_dispatch_receipt":p})); return b,p,v
    def merge(self, path, payload, dry_run=False):
        d={"ok":True,"status":"decided","action":"merge"}; b=triage.build_merge_receipt(request({"receipt_path":str(path),"payload":payload,"dry_run":dry_run},{"verify_merge_provenance":{"ok":True,"status":"verified","verified_provenance":PROVENANCE},"decide_triage_action":d})); r=triage.read_receipt_merge_provenance(request({"receipt_path":str(path),"dry_run":dry_run},{"build_merge_receipt":b})); p=triage.publish_merge_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"read_receipt_merge_provenance":r,"decide_triage_action":d})); v=triage.verify_merge_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"publish_merge_receipt":p})); return b,r,p,v
    @staticmethod
    def evidence(no_branch=False, mutated=False):
        n=("parse_cleanup_issue_number","check_issue_closed","check_no_open_pr_for_branch","remove_worktree","delete_local_branch","release_claim_file")
        if no_branch:return {x:{"ok":True,"status":"noop","reason":"no_branch","mutated":False} for x in n}
        return {"parse_cleanup_issue_number":{"ok":True,"status":"parsed","issue":7,"mutated":False},"check_issue_closed":{"ok":True,"status":"checked","closed":True,"mutated":False},"check_no_open_pr_for_branch":{"ok":True,"status":"checked","safe_to_cleanup":True,"open_count":0,"mutated":False},"remove_worktree":{"ok":True,"status":"already_absent","mutated":mutated},"delete_local_branch":{"ok":True,"status":"already_absent","mutated":False},"release_claim_file":{"ok":True,"status":"already_absent","mutated":False}}
    def cleanup_build(self,path,evidence=None,entity=None,dry_run=False):
        e=self.evidence() if evidence is None else evidence; c=cleanup.collect_cleanup_receipt_evidence(request({"dry_run":dry_run},e)); d=cleanup.decide_cleanup_outcome(request({"dry_run":dry_run},{"collect_cleanup_receipt_evidence":c})); return cleanup.build_cleanup_receipt(request({"receipt_path":str(path),"entity":entity or {},"dry_run":dry_run},{"collect_cleanup_receipt_evidence":c,"decide_cleanup_outcome":d}))
    def cleanup_lifecycle(self,path,entity=None,dry_run=False):
        b=self.cleanup_build(path,entity=entity,dry_run=dry_run); p=cleanup.publish_cleanup_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"build_cleanup_receipt":b})); v=cleanup.verify_cleanup_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"build_cleanup_receipt":b,"publish_cleanup_receipt":p})); return b,p,v
    def test_dispatch_build_publish_verify_lifecycle(self):
        with tempfile.TemporaryDirectory() as t:
            b,p,v=self.dispatch(Path(t)/"d.json",{"phase":"DISPATCHED","issue":1}); self.assertEqual((b["status"],p["status"],v["status"]),("built","published","verified"))
    def test_merge_build_publish_verify_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            b,r,p,v=self.merge(Path(t)/"m.json",{"candidate":"pr-7"}); self.assertEqual((b["status"],r["status"],p["status"],v["status"]),("merge_receipt_built","receipt_provenance_read","written","merge_receipt_verified")); self.assertEqual(json.loads((Path(t)/"m.json").read_text())["verified_provenance"],PROVENANCE)
    def test_cleanup_build_publish_verify_preserves_identity(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"c.json"; e={"task":"task-7","repo":"owner/repo","issue":7,"receipt":str(Path(t)/"d.json")}; b,p,v=self.cleanup_lifecycle(path,entity=e); self.assertEqual((b["status"],p["status"],v["status"]),("built","written","verified")); self.assertEqual(json.loads(path.read_text())["entity"],e)
    def test_identical_idempotent_conflict_no_clobber(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"c.json"; e={"task":"task-7"}; b,p,v=self.cleanup_lifecycle(path,entity=e); original=path.read_bytes(); same=self.cleanup_build(path,entity=e); self.assertEqual(cleanup.publish_cleanup_receipt(request({"receipt_path":str(path),"dry_run":False},{"build_cleanup_receipt":same}))["status"],"exists"); conflict=self.cleanup_build(path,entity={"task":"other"}); r=cleanup.publish_cleanup_receipt(request({"receipt_path":str(path),"dry_run":False},{"build_cleanup_receipt":conflict})); self.assertEqual((p["status"],r["reason"],path.read_bytes()),("written","receipt_conflict",original))
    def test_dry_run_planned(self):
        with tempfile.TemporaryDirectory() as t:
            d,m=Path(t)/"d.json",Path(t)/"m.json"; self.assertEqual(self.dispatch(d,{"phase":"x"},True)[1]["status"],"planned"); self.assertEqual(self.merge(m,{"candidate":"x"},True)[2]["status"],"planned"); self.assertFalse(d.exists()); self.assertFalse(m.exists())
    def test_no_branch_exact_build_skip(self):
        with tempfile.TemporaryDirectory() as t:
            b=self.cleanup_build(Path(t)/"c.json",self.evidence(no_branch=True)); self.assertEqual((b["status"],b["reason"]),("noop","no_branch")); self.assertFalse(b["mutated"])
    def test_cleanup_evidence_failure_attribution(self):
        c=cleanup.collect_cleanup_receipt_evidence(request({}, {"parse_cleanup_issue_number":{"ok":True}})); d=cleanup.decide_cleanup_outcome(request({}, {"collect_cleanup_receipt_evidence":c})); self.assertEqual((c["reason"],d["operation"],d["upstream_effector"]),("cleanup_evidence_missing","decide_cleanup_outcome","collect_cleanup_receipt_evidence"))
    def test_merge_failure_metadata(self):
        with tempfile.TemporaryDirectory() as t:
            b=triage.build_merge_receipt(request({"receipt_path":str(Path(t)/"m.json")},{"verify_merge_provenance":{"ok":False,"status":"failed"}})); self.assertEqual((b["operation"],b["upstream_effector"],b["failure_class"],b["retry_safe"]),("build_merge_receipt","verify_merge_provenance","terminal",False))
    def test_cleanup_fsync_failure_rollback_retry(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"c.json"; b=self.cleanup_build(path)
            with mock.patch("lokay.steps.cleanup.os.fsync",side_effect=[None,OSError("directory fsync failed"),None]): r=cleanup.publish_cleanup_receipt(request({"receipt_path":str(path),"dry_run":False},{"build_cleanup_receipt":b}))
            self.assertEqual(r["reason"],"receipt_write_failed"); self.assertFalse(path.exists()); self.assertEqual(cleanup.publish_cleanup_receipt(request({"receipt_path":str(path),"dry_run":False},{"build_cleanup_receipt":b}))["status"],"written")
    def test_cleanup_failed_rollback_durability(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"c.json"; b=self.cleanup_build(path); real=os.unlink
            def bad(x):
                if Path(x)==path: raise OSError("rollback unlink failed")
                real(x)
            with mock.patch("lokay.steps.cleanup.os.fsync",side_effect=[None,OSError("directory fsync failed"),OSError("rollback fsync failed")]),mock.patch("lokay.steps.cleanup.os.unlink",side_effect=bad): r=cleanup.publish_cleanup_receipt(request({"receipt_path":str(path),"dry_run":False},{"build_cleanup_receipt":b}))
            self.assertEqual(r["reason"],"receipt_write_failed"); self.assertTrue(path.is_file())
    def test_cleanup_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as t:
            target=Path(t)/"x"; target.write_text("{}"); link=Path(t)/"l"; link.symlink_to(target); b=self.cleanup_build(link); self.assertEqual(cleanup.publish_cleanup_receipt(request({"receipt_path":str(link),"dry_run":False},{"build_cleanup_receipt":b}))["reason"],"receipt_conflict")
    def test_verify_readback_mismatch(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"c.json"; b,p,v=self.cleanup_lifecycle(path,{"task":"x"}); path.write_text("{}"); r=cleanup.verify_cleanup_receipt(request({"receipt_path":str(path),"dry_run":False},{"build_cleanup_receipt":b,"publish_cleanup_receipt":p})); self.assertEqual((r["reason"],r["failure_class"],r["retry_safe"]),("receipt_readback_mismatch","terminal",False))
if __name__ == "__main__": unittest.main()
