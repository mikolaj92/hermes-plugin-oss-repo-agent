"""Durability contracts for canonical receipt lifecycle atoms."""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from lokay.process_runtime import payload_digest
from lokay.steps import cleanup, issue_to_pr, triage
from lokay.steps.orchestration import aggregate_lane_results
PROVENANCE = {"source":"github_pr_readback","state":"MERGED","repo":"owner/repo","number":7,"head_oid":"head-7","head_ref":"ai/fix/7","merge_oid":"merge-7","merged_at":"2026-01-01T00:00:00Z"}
def request(data: dict, conduction: dict | None = None, *, config: dict | None = None) -> dict:
    return {"input": {**data, "conduction": conduction or {}}, "config": config or {}}
class ReceiptDurabilityTests(unittest.TestCase):
    def dispatch(self, path, payload, dry_run=False):
        b=issue_to_pr.build_dispatch_receipt(request({"receipt_path":str(path),"payload":payload,"dry_run":dry_run})); p=issue_to_pr.publish_dispatch_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"build_dispatch_receipt":b})); v=issue_to_pr.verify_dispatch_receipt(request({"receipt_path":str(path),"dry_run":dry_run},{"build_dispatch_receipt":b,"publish_dispatch_receipt":p})); return b,p,v
    def merge(self, path, payload, dry_run=False, *, include_closure=True, closure=None, provenance_status="merge_provenance_verified", postcondition_provenance=None, include_postcondition=True):
        subject={"repo":PROVENANCE["repo"],"number":PROVENANCE["number"],"head_oid":PROVENANCE["head_oid"]}
        decision={"status":"decided","ok":True,"action":"merge",**subject}
        identity={"schema_version":1,"process_id":"pr_triage","candidate_id":"candidate-7","config_sha256":"config-7","generation":"generation-7","correlation_id":payload_digest({"process_id":"pr_triage","kind":"pr_decision","subject":payload_digest(subject)}),"subject":subject,"predecessor_digests":[],"operation":"publish","source":"lokay.process_runtime","mutation_status":"mutated","payload":decision}
        digest=payload_digest(identity)
        body={**identity,"content_digest":digest,"verified_readback_state":"verified"}
        durable={"path_id":"pr_merge","repo":subject["repo"],"number":subject["number"],"head_oid":subject["head_oid"],"pr_decision":digest,"generation":identity["generation"],"candidate_id":identity["candidate_id"],"config_sha256":identity["config_sha256"],"predecessor_evidence":{"groups":[["pr_decision"]],"required_inputs":["pr_decision"],"subject":subject,"receipts":{"pr_decision":{"process_id":"pr_triage","receipt_kind":"pr_decision","digest":digest,"payload":body}}}}
        postcondition_provenance = PROVENANCE if postcondition_provenance is None else postcondition_provenance
        postcondition_status = "planned" if dry_run else "merge_postcondition_read"
        conduction={"verify_merge_provenance":{"ok":True,"status":provenance_status,"verified_provenance":PROVENANCE}}
        if include_postcondition:
            conduction["read_merge_postcondition"]={"ok":True,"status":postcondition_status,"verified_provenance":postcondition_provenance}
        if include_closure:
            conduction["verify_linked_issue_closed"] = closure or {"ok":True,"status":"issue_close_verified","issue_close_verified":True,"repo":PROVENANCE["repo"],"number":7,"issue":7,"pr_number":PROVENANCE["number"],"head_oid":PROVENANCE["head_oid"],"head_ref":PROVENANCE["head_ref"],"verified_provenance":PROVENANCE,"mutated":False}
        b=triage.build_merge_receipt(request({"receipt_path":str(path),"payload":payload,"dry_run":dry_run,**durable},conduction)); r=triage.read_receipt_merge_provenance(request({"receipt_path":str(path),"dry_run":dry_run,**durable},{"build_merge_receipt":b})); p=triage.publish_merge_receipt(request({"receipt_path":str(path),"dry_run":dry_run,**durable},{"read_receipt_merge_provenance":r})); v=triage.verify_merge_receipt(request({"receipt_path":str(path),"dry_run":dry_run,**durable},{"publish_merge_receipt":p})); return b,r,p,v
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
    def test_dispatch_receipt_is_private(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/"d.json"; self.dispatch(path,{"phase":"DISPATCHED","issue":1}); self.assertEqual(path.stat().st_mode & 0o777,0o600)
    def test_merge_receipt_requires_verified_issue_closure(self):
        with tempfile.TemporaryDirectory() as t:
            built, _read, _publish, _verify = self.merge(Path(t) / "missing.json", {"candidate": "pr-7"}, include_closure=False)
            self.assertEqual((built["status"], built["reason"]), ("failed", "merge_issue_close_verification_required"))
            self.assertFalse(built["mutated"])
    def test_merge_receipt_rejects_wrong_provenance_status(self):
        with tempfile.TemporaryDirectory() as t:
            built, _read, _publish, _verify = self.merge(Path(t) / "wrong-status.json", {"candidate": "pr-7"}, provenance_status="verified")
            self.assertEqual((built["status"], built["reason"]), ("failed", "merge_provenance_unverified"))
            self.assertFalse(built["mutated"])
    def test_merge_receipt_rejects_mutated_postcondition_provenance(self):
        mutated = {**PROVENANCE, "head_oid": "forged-head"}
        with tempfile.TemporaryDirectory() as t:
            built, _read, _publish, _verify = self.merge(Path(t) / "forged.json", {"candidate": "pr-7"}, postcondition_provenance=mutated)
            self.assertEqual((built["status"], built["reason"]), ("failed", "merge_provenance_identity_mismatch"))
            self.assertFalse(built["mutated"])
    def test_merge_receipt_requires_postcondition(self):
        with tempfile.TemporaryDirectory() as t:
            built, _read, _publish, _verify = self.merge(Path(t) / "missing-postcondition.json", {"candidate": "pr-7"}, include_postcondition=False)
            self.assertEqual((built["status"], built["reason"]), ("failed", "merge_postcondition_required"))
            self.assertFalse(built["mutated"])

    def test_dry_run_linked_issue_closure_chain_preserves_provenance(self):
        subject = {"repo": PROVENANCE["repo"], "number": PROVENANCE["number"], "head_oid": PROVENANCE["head_oid"]}
        decision = {"status": "decided", "ok": True, "action": "merge", **subject}
        identity = {"schema_version": 1, "process_id": "pr_triage", "candidate_id": "candidate-7", "config_sha256": "config-7", "generation": "generation-7", "correlation_id": payload_digest({"process_id": "pr_triage", "kind": "pr_decision", "subject": payload_digest(subject)}), "subject": subject, "predecessor_digests": [], "operation": "publish", "source": "lokay.process_runtime", "mutation_status": "mutated", "payload": decision}
        digest = payload_digest(identity)
        body = {**identity, "content_digest": digest, "verified_readback_state": "verified"}
        durable = {"path_id": "pr_merge", "repo": subject["repo"], "number": subject["number"], "head_oid": subject["head_oid"], "pr_decision": digest, "generation": identity["generation"], "candidate_id": identity["candidate_id"], "config_sha256": identity["config_sha256"], "predecessor_evidence": {"groups": [["pr_decision"]], "required_inputs": ["pr_decision"], "subject": subject, "receipts": {"pr_decision": {"process_id": "pr_triage", "receipt_kind": "pr_decision", "digest": digest, "payload": body}}}}
        linked = {"ok": True, "status": "linked_merge_provenance_verified", "repo": PROVENANCE["repo"], "issue": 7, "verified_provenance": PROVENANCE}
        read = triage.read_linked_issue_state(request({**durable, "dry_run": True}, {"verify_linked_merge_provenance": linked}))
        close = triage.close_linked_issue(request({**durable, "dry_run": True}, {"read_linked_issue_state": read}))
        verify = triage.verify_linked_issue_closed(request({**durable, "dry_run": True}, {"close_linked_issue": close}))
        built = triage.build_merge_receipt(request({**durable, "dry_run": True, "receipt_path": "dry-run.json"}, {"verify_merge_provenance": {"ok": True, "status": "planned", "verified_provenance": PROVENANCE}, "read_merge_postcondition": {"ok": True, "status": "planned", "verified_provenance": PROVENANCE}, "verify_linked_issue_closed": verify}))
        self.assertEqual((read["status"], close["status"], verify["status"], built["status"]), ("planned", "planned", "planned", "merge_receipt_built"))
        self.assertEqual(verify["verified_provenance"], PROVENANCE)
        self.assertEqual(built["payload"]["issue"], 7)

    def test_open_issue_closure_cannot_build_merge_receipt(self):
        close_source = {
            "ok": True,
            "status": "issue_closed",
            "repo": PROVENANCE["repo"],
            "number": 7,
            "issue": 7,
            "pr_number": PROVENANCE["number"],
            "head_oid": PROVENANCE["head_oid"],
            "head_ref": PROVENANCE["head_ref"],
            "verified_provenance": PROVENANCE,
            "mutated": True,
        }
        with mock.patch("lokay.steps.triage.run_cmd", return_value=mock.Mock(stdout=json.dumps({"state": "OPEN"}), stderr="", returncode=0)):
            closure = triage.verify_linked_issue_closed(
                request(
                    {
                        "path_id": "pr_merge",
                        "repo": PROVENANCE["repo"],
                        "number": 7,
                        "dry_run": False,
                    },
                    {
                        "close_linked_issue": close_source,
                        "verify_merge_provenance": {
                            "ok": True,
                            "status": "merge_provenance_verified",
                            "verified_provenance": PROVENANCE,
                        },
                    },
                )
            )
        self.assertEqual((closure["status"], closure["reason"]), ("failed", "close_readback_mismatch"))
        with tempfile.TemporaryDirectory() as t:
            built, _read, _publish, _verify = self.merge(Path(t) / "open.json", {"candidate": "pr-7"}, closure=closure)
            self.assertEqual((built["status"], built["reason"]), ("failed", "upstream_failed"))
            self.assertFalse(built["mutated"])

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
    def test_verified_merge_finalization_authorizes_cleanup_with_exact_head(self):
        with tempfile.TemporaryDirectory() as t:
            merge_path=Path(t)/"merge.json"
            cleanup_path=Path(t)/"cleanup.json"
            _b,_r,_p,verified=self.merge(merge_path,{"candidate":"pr-7"})
            self.assertEqual(verified["status"],"merge_receipt_verified")
            payload=json.loads(merge_path.read_text())
            provenance=payload["verified_provenance"]
            self.assertEqual(provenance,PROVENANCE)
            self.assertEqual((payload["repo"],payload["pr"],payload["headSha"]),(PROVENANCE["repo"],PROVENANCE["number"],PROVENANCE["head_oid"]))
            identity={"repo":PROVENANCE["repo"],"issue":7,"pr_number":PROVENANCE["number"],"branch":PROVENANCE["head_ref"],"head_oid":PROVENANCE["head_oid"]}
            lifecycle={"status":"decided","ok":True,"mutated":False,"outcome":"finalize_merged","identity":identity}
            triage_decision={"status":"verified","ok":True,"mutated":False,"repo":PROVENANCE["repo"],"board":"board-7","clone_path":"/repo","priority":3,"issue":7,"pr_number":PROVENANCE["number"],"branch":PROVENANCE["head_ref"],"head_oid":PROVENANCE["head_oid"]}
            verified_receipt={"status":"merge_receipt_verified","ok":True,"mutated":False,"receipt_path":str(merge_path),"payload":payload,"verified_provenance":provenance}
            authorized=aggregate_lane_results(request({},{"auto_worker_triage_verify_merge_receipt":verified_receipt,"auto_worker_triage_decide_triage_action":triage_decision,"auto_worker_lifecycle_decide_lifecycle_transition":lifecycle}))
            self.assertTrue(authorized["cleanup_authorized"],authorized)
            self.assertEqual(authorized["cleanup_identity"],{"repo":PROVENANCE["repo"],"board":"board-7","clone_path":"/repo","priority":3,"issue":7,"pr_number":PROVENANCE["number"],"branch":PROVENANCE["head_ref"],"head_oid":PROVENANCE["head_oid"]})
            mismatched=aggregate_lane_results(request({},{"auto_worker_triage_verify_merge_receipt":verified_receipt,"auto_worker_triage_decide_triage_action":triage_decision,"auto_worker_lifecycle_decide_lifecycle_transition":{**lifecycle,"identity":{**identity,"head_oid":"forged-head"}}}))
            self.assertFalse(mismatched["cleanup_authorized"])
            self.assertNotIn("cleanup_identity",mismatched)
            missing_finalization=aggregate_lane_results(request({},{"auto_worker_triage_verify_merge_receipt":verified_receipt,"auto_worker_triage_decide_triage_action":triage_decision,"auto_worker_lifecycle_decide_lifecycle_transition":{"status":"decided","ok":True,"mutated":False,"outcome":"wait_pending_checks","identity":identity}}))
            self.assertFalse(missing_finalization["cleanup_authorized"])
            self.assertNotIn("cleanup_identity",missing_finalization)
            unauthorized=cleanup.check_issue_closed(request({"repo":PROVENANCE["repo"],"issue":7},{"aggregate_lane_results":{"ok":True,"status":"aggregated","cleanup_authorized":False}}))
            self.assertFalse(unauthorized["ok"])
            self.assertEqual(unauthorized["reason"],"cleanup_not_authorized")
            entity={"task":"task-7","repo":PROVENANCE["repo"],"issue":7,"pr_number":PROVENANCE["number"],"branch":PROVENANCE["head_ref"],"head_oid":PROVENANCE["head_oid"],"receipt":str(merge_path)}
            b,p,v=self.cleanup_lifecycle(cleanup_path,entity=entity)
            self.assertEqual((b["status"],p["status"],v["status"]),("built","written","verified"))
            stored=json.loads(cleanup_path.read_text())
            self.assertEqual(stored["entity"],entity)
            self.assertEqual(stored["entity"]["head_oid"],PROVENANCE["head_oid"])
            self.assertEqual(stored["entity"]["pr_number"],PROVENANCE["number"])
            self.assertEqual(stored["phase"],"CLEANUP_TERMINAL")
            with mock.patch("lokay.steps.cleanup.run_cmd",return_value=mock.Mock(stdout=json.dumps({"state":"CLOSED"}),stderr="",returncode=0)) as run_cmd:
                allowed=cleanup.check_issue_closed(request({"repo":PROVENANCE["repo"],"issue":7},{"aggregate_lane_results":authorized}))
            self.assertTrue(allowed["ok"],allowed)
            self.assertTrue(allowed["closed"])
            self.assertEqual((allowed["repo"],allowed["issue"]),(PROVENANCE["repo"],7))
            run_cmd.assert_called_once()
if __name__ == "__main__": unittest.main()
