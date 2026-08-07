from __future__ import annotations

import sys

import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from lokay.registry import PROCESS_IDS, MIGRATION_DEFAULTS, canonical_toml, process_defaults
from lokay.process_runtime import LeaseError
from lokay import supervisor as sup


def _document(*, mode: str = "dry-run", enabled: dict[str, bool] | None = None) -> dict:
    enabled = enabled or {}
    processes = process_defaults()
    for row in processes:
        if row["id"] in enabled:
            row["enabled"] = enabled[row["id"]]
    return {
        "version": 1,
        "mode": mode,
        "branch_prefix": "ai/fix",
        "base_branch": "main",
        "github": {"cli": "gh", "default_limit": 10, "assignee": ""},
        "labels": dict(MIGRATION_DEFAULTS["labels"]),
        "automation": dict(MIGRATION_DEFAULTS["automation"]),
        "direction": dict(MIGRATION_DEFAULTS["direction"]),
        "triage": dict(MIGRATION_DEFAULTS["triage"]),
        "executor": dict(MIGRATION_DEFAULTS["executor"]),
        "paths": {
            "worktree_root": "~/.hermes/worktrees/lokay",
            "dispatch_receipts": "~/.hermes/state/lokay-dispatch",
            "task_receipts": "~/.hermes/state/lokay-receipts",
            "merge_receipts": "~/.hermes/state/lokay-merge",
            "active_issue": "~/.hermes/state/lokay-active",
            "triage_receipts": "~/.hermes/state/lokay-triage",
        },
        "repos": [
            {
                "repo": "owner/one",
                "board": "owner-one",
                "clone_path": "~/Developer/one",
                "priority": 10,
            }
        ],
        "processes": processes,
    }


def _write_config(directory: Path, document: dict) -> Path:
    path = directory / "config.toml"
    path.write_bytes(canonical_toml(document))
    return path


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += float(seconds)


class SupervisorSingletonTests(unittest.TestCase):
    def test_two_contender_singleton_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            clock = _Clock()
            owner_a = sup.SupervisorOwner(
                owner_token="a" * 32,
                owner_pid=os.getpid(),
                start_identity="a-start",
                candidate_id="c" * 64,
                generation="gen-a",
                config_sha256="d" * 64,
            )
            owner_b = sup.SupervisorOwner(
                owner_token="b" * 32,
                owner_pid=os.getpid(),
                start_identity="b-start",
                candidate_id="c" * 64,
                generation="gen-a",
                config_sha256="d" * 64,
            )
            store_a = sup.SupervisorStore(root, owner=owner_a, clock=clock)
            store_b = sup.SupervisorStore(root, owner=owner_b, clock=clock)

            barrier = threading.Barrier(2)
            results: dict[str, Any] = {}
            errors: dict[str, BaseException] = {}

            def contender(name: str, store: sup.SupervisorStore) -> None:
                try:
                    barrier.wait(timeout=2)
                    results[name] = store.acquire_singleton()
                except BaseException as exc:  # noqa: BLE001 - capture exact contender outcome
                    errors[name] = exc

            threads = [
                threading.Thread(target=contender, args=("a", store_a)),
                threading.Thread(target=contender, args=("b", store_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            winners = [name for name in ("a", "b") if name in results]
            losers = [name for name in ("a", "b") if name in errors]
            self.assertEqual(len(winners), 1, f"results={results} errors={errors}")
            self.assertEqual(len(losers), 1, f"results={results} errors={errors}")
            self.assertIsInstance(errors[losers[0]], LeaseError)
            winner_record = results[winners[0]]
            readback = store_a.read_singleton()
            self.assertIsNotNone(readback)
            assert readback is not None
            self.assertEqual(readback.owner_token, winner_record.owner_token)
            self.assertEqual(readback.lease_key, sup.SINGLETON_KEY)

    def test_renewal_and_lease_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            clock = _Clock()
            owner = sup.SupervisorOwner(
                owner_token="tok",
                owner_pid=os.getpid(),
                start_identity="start",
                candidate_id="c" * 64,
                generation="gen",
                config_sha256="f" * 64,
            )
            store = sup.SupervisorStore(root, owner=owner, clock=clock)
            first = store.acquire_singleton()
            self.assertEqual(first.ttl_seconds, 90)
            self.assertEqual(first.renew_seconds, 30)
            self.assertEqual(first.stale_after - first.expires_at, 90.0)

            clock.advance(10)
            renewed = store.renew_singleton()
            self.assertGreater(renewed.expires_at, first.expires_at)
            self.assertEqual(renewed.owner_token, owner.owner_token)

            # Foreign owner token cannot renew; original expiry stays owned.
            foreign = sup.SupervisorStore(
                root,
                owner=sup.SupervisorOwner(
                    owner_token="other",
                    owner_pid=os.getpid(),
                    start_identity="other-start",
                    candidate_id=owner.candidate_id,
                    generation=owner.generation,
                    config_sha256=owner.config_sha256,
                ),
                clock=clock,
            )
            with self.assertRaises(LeaseError):
                foreign.renew_singleton()
            still = store.read_singleton()
            self.assertIsNotNone(still)
            assert still is not None
            self.assertEqual(still.owner_token, owner.owner_token)

            # Overwrite row to simulate lost ownership, then renew fails closed.
            connection = store._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE singleton_lease SET owner_token = ? WHERE lease_key = ?",
                    ("stolen", sup.SINGLETON_KEY),
                )
                connection.execute("COMMIT")
            finally:
                connection.close()
            with self.assertRaises(LeaseError):
                store.renew_singleton()


class SupervisorDispatchTests(unittest.TestCase):
    def test_exact_child_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document())
            db = root / "state.sqlite"
            python = root / "python"
            inventory = sup.build_dispatch_commands(
                processes=_document()["processes"],
                python=python,
                config_path=config,
                db_path=db,
                dry_run=True,
            )
            self.assertEqual(len(inventory), 12)
            self.assertEqual([item["process_id"] for item in inventory], list(PROCESS_IDS))
            for item in inventory:
                process_id = item["process_id"]
                self.assertEqual(
                    item["command"],
                    [
                        str(python),
                        "-m",
                        "lokay.process",
                        f"lokay-process-{process_id}",
                        "--config",
                        str(config),
                        "--db",
                        str(db),
                        "--dry-run",
                        "--json",
                    ],
                )
                self.assertEqual(item["command_digest"], sup.command_digest(item["command"]))

            live = sup.build_child_command(
                process_id="repo_issue_poll",
                python=python,
                config_path=config,
                db_path=db,
                dry_run=False,
            )
            self.assertIn("--live", live)
            self.assertNotIn("--dry-run", live)

    def test_main_passes_exact_runtime_interpreter(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run_supervisor(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True, "status": "ok", "reason": "once", "dispatches": 0, "lease_lost": False}

        with mock.patch.object(sup, "run_supervisor", side_effect=fake_run_supervisor):
            with mock.patch.object(sup.sys, "executable", "/exact/runtime/python"):
                code = sup.main(["--config", "/config.toml", "--db", "/state.sqlite", "--once"])

        self.assertEqual(code, 0)
        self.assertEqual(captured["python"], "/exact/runtime/python")

    def test_starting_reservation_blocks_duplicate_after_spawn_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            db = root / "state.sqlite"
            state_root = root / "supervisor"
            clock = _Clock(start=1_000.0)
            candidate_id = "a" * 64
            generation = "generation-test"
            config_sha256 = sup._config_sha256(config)
            spawned: list[sup.ChildHandle] = []

            first = sup.Supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                python=root / "python",
                state_root=state_root,
                clock=clock,
                candidate_id=candidate_id,
                generation=generation,
                config_sha256=config_sha256,
            )
            first.store.acquire_singleton()

            def crash_after_spawn(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del command, stdout_path, stderr_path, env
                child = sup.ChildHandle(pid=50_001, start_identity="spawned-start", _poll_after=10_000)
                spawned.append(child)
                raise RuntimeError("simulated supervisor crash after child creation")

            first.process_factory = crash_after_spawn
            with self.assertRaisesRegex(RuntimeError, "after child creation"):
                first._dispatch_due()

            reservation = first.store.list_slots()[0]
            self.assertEqual(reservation.status, "starting")
            self.assertIsNone(reservation.pid)
            self.assertEqual(
                reservation.details["start_reservation"],
                {
                    "dispatch_id": reservation.dispatch_id,
                    "command": list(reservation.command),
                    "command_digest": reservation.command_digest,
                    "fencing_identity": {
                        "candidate_id": candidate_id,
                        "generation": generation,
                        "config_sha256": config_sha256,
                        "owner_token": first.owner.owner_token,
                        "supervisor_pid": first.owner.owner_pid,
                        "supervisor_start_identity": first.owner.start_identity,
                    },
                },
            )
            self.assertEqual(len(spawned), 1)

            # Expiry alone cannot prove that a child created after the
            # reservation died; retain the fence across supervisor restart.
            clock.advance(300.0)
            launches_after_restart: list[str] = []

            def duplicate_factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del stdout_path, stderr_path, env
                launches_after_restart.append(str(command[3]))
                return sup.ChildHandle(pid=50_002, start_identity="duplicate-start")

            second = sup.Supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                python=root / "python",
                state_root=state_root,
                clock=clock,
                process_factory=duplicate_factory,
                candidate_id=candidate_id,
                generation=generation,
                config_sha256=config_sha256,
                owner_token="restart-owner",
                start_identity="restart-start",
            )
            with mock.patch.object(sup, "_pid_alive", return_value=False):
                second.store.acquire_singleton()
            second._reconcile_slots()
            recovered = second.store.list_slots()[0]
            self.assertEqual(recovered.status, "orphaned")
            self.assertTrue(recovered.details["recovery_required"])
            self.assertEqual(recovered.details["orphan_resolution"], "unknown")
            self.assertEqual(second._dispatch_due(), [])
            self.assertEqual(launches_after_restart, [])
    def test_no_duplicate_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(
                    enabled={
                        process_id: process_id in {"repo_issue_poll", "issue_triage"}
                        for process_id in PROCESS_IDS
                    }
                ),
            )
            db = root / "state.sqlite"
            state_root = root / "supervisor"
            clock = _Clock()
            launches: list[str] = []
            handles: dict[str, sup.ChildHandle] = {}

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del env
                process_id = command[3].removeprefix("lokay-process-")
                launches.append(process_id)
                Path(stdout_path).write_text("ok\n", encoding="utf-8")
                Path(stderr_path).write_text("", encoding="utf-8")
                # Stay running until explicitly reaped many polls: prevents redispatch.
                handle = sup.ChildHandle(
                    pid=10_000 + len(launches),
                    start_identity=f"child-{process_id}",
                    _poll_after=10_000,
                )
                handles[process_id] = handle
                return handle

            result = sup.run_supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                once=True,
                python=root / "python",
                state_root=state_root,
                clock=clock,
                process_factory=factory,
                sleep=lambda _s: None,
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            self.assertEqual(result["reason"], "once")
            self.assertEqual(sorted(launches), ["issue_triage", "repo_issue_poll"])
            self.assertEqual(launches.count("repo_issue_poll"), 1)
            self.assertEqual(launches.count("issue_triage"), 1)
            self.assertEqual(result["dispatches"], 2)

            # After once-exit, drain force-stops children as orphaned/due-now
            # (not retryable failure). Restart redispatches each due process once.
            result2 = sup.run_supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                once=True,
                python=root / "python",
                state_root=state_root,
                clock=clock,
                process_factory=factory,
                sleep=lambda _s: None,
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
                owner_token=uuid.uuid4().hex,
                start_identity="second-supervisor",
            )
            self.assertEqual(result2["dispatches"], 2)
            self.assertEqual(launches.count("repo_issue_poll"), 2)
            self.assertEqual(launches.count("issue_triage"), 2)
            # Within the second pass alone, no process is double-launched.
            second_only = launches[2:]
            self.assertEqual(sorted(second_only), ["issue_triage", "repo_issue_poll"])
            self.assertEqual(len(second_only), len(set(second_only)))

    def test_no_duplicate_within_pass_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            db = root / "state.sqlite"
            state_root = root / "supervisor"
            clock = _Clock()
            launches: list[str] = []

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del env
                process_id = command[3].removeprefix("lokay-process-")
                launches.append(process_id)
                Path(stdout_path).write_text("ok\n", encoding="utf-8")
                Path(stderr_path).write_bytes(b"")
                return sup.ChildHandle(
                    pid=42,
                    start_identity="child",
                    _poll_after=10_000,
                )

            stop = threading.Event()
            # max_loops drives multiple wake cycles with child still running.
            result = sup.run_supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                once=False,
                max_loops=5,
                python=root / "python",
                state_root=state_root,
                clock=clock,
                process_factory=factory,
                sleep=lambda _s: clock.advance(1),
                stop_event=stop,
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            self.assertEqual(launches.count("repo_issue_poll"), 1)
            self.assertEqual(result["dispatches"], 1)
    def test_restart_does_not_redispatch_live_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            state_root = root / "supervisor"
            candidate_id = "b" * 64
            generation = "gen-test"
            config_sha256 = sup._config_sha256(config)
            supervisor = sup.Supervisor(
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                python=root / "python",
                state_root=state_root,
                candidate_id=candidate_id,
                generation=generation,
                config_sha256=config_sha256,
            )
            item = supervisor._inventory_by_id["repo_issue_poll"]
            supervisor.store.upsert_slot(
                sup.DispatchSlot(
                    process_id="repo_issue_poll",
                    dispatch_id="prior-dispatch",
                    command=tuple(item["command"]),
                    command_digest=str(item["command_digest"]),
                    candidate_id=candidate_id,
                    generation=generation,
                    config_sha256=config_sha256,
                    due_at=1_000.0,
                    status="running",
                    pid=42_424,
                    start_identity="persisted-child-start",
                    started_at=999.0,
                    deadline_at=2_000.0,
                )
            )
            launches: list[str] = []

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del command, stdout_path, stderr_path, env
                launches.append("unexpected")
                raise AssertionError("live orphan must remain fenced")

            with mock.patch.object(sup, "_pid_alive", return_value=True):
                result = sup.run_supervisor(
                    config_path=config,
                    db_path=root / "state.sqlite",
                    dry_run=True,
                    once=True,
                    python=root / "python",
                    state_root=state_root,
                    process_factory=factory,
                    clock=_Clock(),
                    sleep=lambda _seconds: None,
                    candidate_id=candidate_id,
                    generation=generation,
                    config_sha256=config_sha256,
                )

            self.assertEqual(result["dispatches"], 0)
            self.assertEqual(launches, [])
            slot = supervisor.store.list_slots()[0]
            self.assertEqual(slot.status, "orphaned")
            self.assertEqual(slot.pid, 42_424)

    def test_restart_redispatches_pid_reuse_after_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            state_root = root / "supervisor"
            candidate_id = "c" * 64
            generation = "gen-test"
            config_sha256 = sup._config_sha256(config)
            supervisor = sup.Supervisor(
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                python=root / "python",
                state_root=state_root,
                candidate_id=candidate_id,
                generation=generation,
                config_sha256=config_sha256,
            )
            item = supervisor._inventory_by_id["repo_issue_poll"]
            supervisor.store.upsert_slot(
                sup.DispatchSlot(
                    process_id="repo_issue_poll",
                    dispatch_id="prior-dispatch",
                    command=tuple(item["command"]),
                    command_digest=str(item["command_digest"]),
                    candidate_id=candidate_id,
                    generation=generation,
                    config_sha256=config_sha256,
                    due_at=1_000.0,
                    status="running",
                    pid=42_425,
                    start_identity="42_425:boot:ps:old-start",
                    started_at=999.0,
                    deadline_at=2_000.0,
                )
            )
            launches: list[str] = []

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del env
                process_id = command[3].removeprefix("lokay-process-")
                launches.append(process_id)
                Path(stdout_path).write_text("ok\n", encoding="utf-8")
                Path(stderr_path).write_bytes(b"")
                return sup.ChildHandle(pid=50_000, start_identity="new-child", _poll_after=0)

            with (
                mock.patch.object(sup, "_pid_alive", return_value=True),
                mock.patch.object(sup, "_start_identity", return_value="42_425:boot:ps:new-start"),
            ):
                result = sup.run_supervisor(
                    config_path=config,
                    db_path=root / "state.sqlite",
                    dry_run=True,
                    once=True,
                    python=root / "python",
                    state_root=state_root,
                    process_factory=factory,
                    clock=_Clock(),
                    sleep=lambda _seconds: None,
                    candidate_id=candidate_id,
                    generation=generation,
                    config_sha256=config_sha256,
                )

            self.assertEqual(result["dispatches"], 1)
            self.assertEqual(launches, ["repo_issue_poll"])


    def test_restart_keeps_live_orphan_when_identity_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            state_root = root / "supervisor"
            candidate_id = "d" * 64
            generation = "gen-test"
            config_sha256 = sup._config_sha256(config)
            supervisor = sup.Supervisor(
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                python=root / "python",
                state_root=state_root,
                candidate_id=candidate_id,
                generation=generation,
                config_sha256=config_sha256,
            )
            item = supervisor._inventory_by_id["repo_issue_poll"]
            supervisor.store.upsert_slot(
                sup.DispatchSlot(
                    process_id="repo_issue_poll",
                    dispatch_id="prior-dispatch",
                    command=tuple(item["command"]),
                    command_digest=str(item["command_digest"]),
                    candidate_id=candidate_id,
                    generation=generation,
                    config_sha256=config_sha256,
                    due_at=1_000.0,
                    status="running",
                    pid=42_426,
                    start_identity="42_426:boot:ps:old-start",
                    started_at=999.0,
                    deadline_at=2_000.0,
                )
            )
            launches: list[str] = []

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del command, stdout_path, stderr_path, env
                launches.append("unexpected")
                raise AssertionError("unknown identity must retain orphan fence")

            with (
                mock.patch.object(sup, "_pid_alive", return_value=True),
                mock.patch.object(sup, "_start_identity", return_value="42_426:boot:unverified"),
            ):
                result = sup.run_supervisor(
                    config_path=config,
                    db_path=root / "state.sqlite",
                    dry_run=True,
                    once=True,
                    python=root / "python",
                    state_root=state_root,
                    process_factory=factory,
                    clock=_Clock(),
                    sleep=lambda _seconds: None,
                    candidate_id=candidate_id,
                    generation=generation,
                    config_sha256=config_sha256,
                )

            self.assertEqual(result["dispatches"], 0)
            self.assertEqual(launches, [])
            slot = supervisor.store.list_slots()[0]
            self.assertEqual(slot.status, "orphaned")
            self.assertEqual(slot.pid, 42_426)

    def test_lease_loss_stops_dispatch_without_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            db = root / "state.sqlite"
            state_root = root / "supervisor"
            clock = _Clock()
            launches: list[str] = []

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del env
                process_id = command[3].removeprefix("lokay-process-")
                launches.append(process_id)
                Path(stdout_path).write_text("ok\n", encoding="utf-8")
                Path(stderr_path).write_bytes(b"")
                return sup.ChildHandle(pid=7, start_identity="child", _poll_after=1)

            owner_token = "owner-token"
            # Pre-create store owner that will acquire, then force renew failure.
            real_renew = sup.SupervisorStore.renew_singleton
            calls = {"n": 0}

            def flaky_renew(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise LeaseError("forced lease loss")
                return real_renew(self, *args, **kwargs)

            with mock.patch.object(sup.SupervisorStore, "renew_singleton", flaky_renew):
                result = sup.run_supervisor(
                    config_path=config,
                    db_path=db,
                    dry_run=True,
                    once=False,
                    max_loops=5,
                    python=root / "python",
                    state_root=state_root,
                    clock=clock,
                    process_factory=factory,
                    sleep=lambda _s: clock.advance(1),
                    candidate_id="a" * 64,
                    generation="gen-test",
                    config_sha256=sup._config_sha256(config),
                    owner_token=owner_token,
                    start_identity="lease-loss-start",
                )

            self.assertTrue(result["lease_lost"] or result["reason"] == "singleton_lease_lost")
            self.assertFalse(result["ok"])
            # Singleton must remain present after loss (not released/overwritten).
            store = sup.SupervisorStore(
                state_root,
                owner=sup.SupervisorOwner(
                    owner_token=owner_token,
                    owner_pid=os.getpid(),
                    start_identity="lease-loss-start",
                    candidate_id="a" * 64,
                    generation="gen-test",
                    config_sha256=sup._config_sha256(config),
                ),
                clock=clock,
            )
            remaining = store.read_singleton()
            self.assertIsNotNone(remaining)
            assert remaining is not None
            self.assertEqual(remaining.owner_token, owner_token)

    def test_request_kill_persists_send_attempt_after_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(enabled={pid: False for pid in PROCESS_IDS}))
            supervisor = sup.Supervisor(
                python=root / "python",
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                state_root=root / "supervisor",
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            item = supervisor._inventory_by_id["repo_issue_poll"]
            slot = sup.DispatchSlot(
                process_id="repo_issue_poll",
                dispatch_id="dispatch",
                command=tuple(item["command"]),
                command_digest=str(item["command_digest"]),
                candidate_id=supervisor.candidate_id,
                generation=supervisor.generation,
                config_sha256=supervisor.config_sha256,
                due_at=0.0,
                status="terminating",
                pid=123,
                start_identity="child-start",
            )

            class KillRaises(sup.ChildHandle):
                def kill(self) -> None:
                    raise OSError("kill denied")

            child = KillRaises(pid=123, start_identity="child-start", _poll_after=10_000)
            supervisor.slots[slot.process_id] = slot
            with mock.patch.object(supervisor, "_persist_slot") as persist:
                supervisor._request_kill(child, slot, now=10.0)

            self.assertEqual(slot.status, "kill_requested")
            self.assertEqual(slot.details["kill_sent_at"], 10.0)
            self.assertEqual(slot.details["kill_error"], "kill denied")
            self.assertGreaterEqual(persist.call_count, 2)

    def test_reap_all_visits_every_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(enabled={pid: False for pid in PROCESS_IDS}))
            supervisor = sup.Supervisor(
                python=root / "python",
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                state_root=root / "supervisor",
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            item = supervisor._inventory_by_id["repo_issue_poll"]
            slots = {
                process_id: sup.DispatchSlot(
                    process_id=process_id,
                    dispatch_id=f"{process_id}-dispatch",
                    command=tuple(item["command"]),
                    command_digest=str(item["command_digest"]),
                    candidate_id=supervisor.candidate_id,
                    generation=supervisor.generation,
                    config_sha256=supervisor.config_sha256,
                    due_at=0.0,
                    status="running",
                )
                for process_id in ("child-a", "child-b")
            }
            children = {
                process_id: sup.ChildHandle(pid=index, start_identity=process_id)
                for index, process_id in enumerate(slots, start=1)
            }
            supervisor.slots.update(slots)
            supervisor._children.update(children)
            with mock.patch.object(supervisor, "_reap_one") as reap_one:
                supervisor._reap_all()

            self.assertEqual(
                {call.args[0] for call in reap_one.call_args_list},
                set(children),
            )
            self.assertEqual(reap_one.call_count, len(children))

    def test_bounded_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            db = root / "state.sqlite"
            state_root = root / "supervisor"
            clock = _Clock()
            stop = threading.Event()
            child = sup.ChildHandle(pid=99, start_identity="child", _poll_after=10_000)

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del command, env
                Path(stdout_path).write_text("running\n", encoding="utf-8")
                Path(stderr_path).write_bytes(b"")
                return child

            def trip() -> None:
                # Let first loop dispatch, then request shutdown.
                time.sleep(0.05)
                stop.set()

            thread = threading.Thread(target=trip)
            thread.start()
            started = time.time()
            result = sup.run_supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                once=False,
                max_loops=100,
                python=root / "python",
                state_root=state_root,
                clock=clock,
                process_factory=factory,
                sleep=lambda s: time.sleep(min(s, 0.01)),
                stop_event=stop,
                shutdown_drain_seconds=0.2,
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            thread.join(timeout=2)
            elapsed = time.time() - started
            self.assertLess(elapsed, 3.0)
            self.assertIn(result["reason"], {"signal", "max_loops", "completed", "once"})
            self.assertTrue(child._terminate_called or result["dispatches"] >= 0)

    def test_default_factory_returns_real_child_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout_path = root / "child.out"
            stderr_path = root / "child.err"
            child = sup._subprocess_factory(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            try:
                self.assertIsInstance(child, sup.ChildHandle)
                self.assertIsNone(child.poll())
                child.terminate()
                self.assertIsNotNone(child.wait(timeout=2))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)

    def test_bounded_drain_persists_orphan_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            db = root / "state.sqlite"
            state_root = root / "supervisor"

            class ResistantChild(sup.ChildHandle):
                def terminate(self) -> None:
                    self._terminate_called = True

                def kill(self) -> None:
                    self._terminate_called = True

            def factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del command, env
                Path(stdout_path).write_text("running\n", encoding="utf-8")
                Path(stderr_path).write_bytes(b"")
                return ResistantChild(pid=91_001, start_identity="orphan-start", _poll_after=10_000)

            candidate_id = "e" * 64
            result = sup.run_supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                once=True,
                python=root / "python",
                state_root=state_root,
                process_factory=factory,
                sleep=lambda _seconds: None,
                shutdown_drain_seconds=0.0,
                candidate_id=candidate_id,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            self.assertEqual(result["reason"], "once")

            reader = sup.Supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                python=root / "python",
                state_root=state_root,
                candidate_id=candidate_id,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            slots = reader.store.list_slots()
            self.assertEqual(len(slots), 1)
            slot = slots[0]
            self.assertEqual(slot.status, "orphaned")
            self.assertEqual(slot.pid, 91_001)
            self.assertEqual(slot.start_identity, "orphan-start")
            self.assertTrue(slot.details["fence_retained"])
    def test_drain_redispatch_nonzero_exit_is_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: pid == "repo_issue_poll" for pid in PROCESS_IDS}),
            )
            db = root / "state.sqlite"
            state_root = root / "supervisor"
            candidate_id = "f" * 64

            class ResistantChild(sup.ChildHandle):
                def terminate(self) -> None:
                    self._terminate_called = True

                def kill(self) -> None:
                    self._terminate_called = True

            def resistant_factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del command, env
                Path(stdout_path).write_text("running\n", encoding="utf-8")
                Path(stderr_path).write_bytes(b"")
                return ResistantChild(
                    pid=92_001,
                    start_identity="first-start",
                    _poll_after=10_000,
                )

            first = sup.run_supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                once=True,
                python=root / "python",
                state_root=state_root,
                process_factory=resistant_factory,
                sleep=lambda _seconds: None,
                shutdown_drain_seconds=0.0,
                candidate_id=candidate_id,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            self.assertEqual(first["reason"], "once")

            def failing_factory(command, *, stdout_path, stderr_path, env=None):  # type: ignore[no-untyped-def]
                del command, env
                Path(stdout_path).write_text("failed\n", encoding="utf-8")
                Path(stderr_path).write_bytes(b"")
                return sup.ChildHandle(
                    pid=92_002,
                    start_identity="second-start",
                    _exit_code=-7,
                )

            with mock.patch.object(sup, "_pid_alive", return_value=False):
                second = sup.run_supervisor(
                    config_path=config,
                    db_path=db,
                    dry_run=True,
                    max_loops=2,
                    python=root / "python",
                    state_root=state_root,
                    process_factory=failing_factory,
                    sleep=lambda _seconds: None,
                    shutdown_drain_seconds=0.0,
                    candidate_id=candidate_id,
                    generation="gen-test",
                    config_sha256=sup._config_sha256(config),
                )

            self.assertEqual(second["reason"], "max_loops")
            slot = sup.Supervisor(
                config_path=config,
                db_path=db,
                dry_run=True,
                python=root / "python",
                state_root=state_root,
                candidate_id=candidate_id,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            ).store.list_slots()[0]
            self.assertEqual(slot.status, "failed")
            self.assertEqual(slot.attempt, 1)
            self.assertEqual(slot.details["last_exit"], -7)
            self.assertNotIn("forced_stop", slot.details)

    def test_drain_deadline_expired_successful_exit_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(enabled={pid: False for pid in PROCESS_IDS}))
            clock = _Clock(start=2_000.0)
            supervisor = sup.Supervisor(
                python=root / "python",
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                state_root=root / "supervisor",
                clock=clock,
                sleep=lambda _seconds: None,
                shutdown_drain_seconds=1.0,
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            item = supervisor._inventory_by_id["repo_issue_poll"]
            item["max_attempts"] = 1
            slot = sup.DispatchSlot(
                process_id="repo_issue_poll",
                dispatch_id="dispatch",
                command=tuple(item["command"]),
                command_digest=str(item["command_digest"]),
                candidate_id=supervisor.candidate_id,
                generation=supervisor.generation,
                config_sha256=supervisor.config_sha256,
                due_at=1_000.0,
                status="running",
                pid=77_001,
                start_identity="deadline-child",
                started_at=1_000.0,
                deadline_at=1_500.0,
                attempt=0,
            )
            child = sup.ChildHandle(
                pid=77_001,
                start_identity="deadline-child",
                _exit_code=0,
            )
            supervisor.slots[slot.process_id] = slot
            supervisor._children[slot.process_id] = child

            supervisor._drain()

            self.assertEqual(slot.status, "idle")
            self.assertEqual(slot.attempt, 0)
            self.assertEqual(slot.exit_code, 0)
            self.assertIsNone(slot.pid)
            self.assertIsNone(slot.start_identity)
            self.assertIsNone(slot.deadline_at)
            self.assertTrue(slot.details.get("exit_confirmed"))
            self.assertNotIn("retry_exhausted", slot.details)
            self.assertNotEqual(slot.details.get("failure_class"), "timeout")
            self.assertNotIn(slot.process_id, supervisor._children)

    def test_deadline_timeout_term_kill_orphan_and_retry_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(enabled={pid: False for pid in PROCESS_IDS}))
            clock = _Clock(start=2_000.0)
            supervisor = sup.Supervisor(
                python=root / "python",
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                state_root=root / "supervisor",
                clock=clock,
                sleep=lambda _seconds: None,
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            item = supervisor._inventory_by_id["repo_issue_poll"]
            item["max_attempts"] = 1
            item["backoff_seconds"] = [30]

            class TermRaises(sup.ChildHandle):
                def terminate(self) -> None:
                    self._terminate_called = True
                    raise OSError("term denied")

                def kill(self) -> None:
                    self._terminate_called = True
                    # Keep the child live so kill grace can orphan the fence.

            slot = sup.DispatchSlot(
                process_id="repo_issue_poll",
                dispatch_id="dispatch",
                command=tuple(item["command"]),
                command_digest=str(item["command_digest"]),
                candidate_id=supervisor.candidate_id,
                generation=supervisor.generation,
                config_sha256=supervisor.config_sha256,
                due_at=1_000.0,
                status="running",
                pid=88_001,
                start_identity="timeout-child",
                started_at=1_000.0,
                deadline_at=1_500.0,
                attempt=0,
            )
            child = TermRaises(pid=88_001, start_identity="timeout-child", _poll_after=10_000)
            supervisor.slots[slot.process_id] = slot
            supervisor._children[slot.process_id] = child

            self.assertFalse(supervisor._reap_one(slot.process_id, child, slot))
            self.assertEqual(slot.status, "terminating")
            self.assertEqual(slot.details["reason"], "deadline_exceeded")
            self.assertEqual(slot.details["failure_class"], "timeout")
            self.assertEqual(slot.details["terminate_sent_at"], 2_000.0)
            self.assertEqual(slot.details["terminate_error"], "term denied")
            self.assertTrue(child._terminate_called)

            clock.advance(sup.TIMEOUT_TERMINATE_GRACE_SECONDS)
            self.assertFalse(supervisor._reap_one(slot.process_id, child, slot))
            self.assertEqual(slot.status, "kill_requested")
            self.assertEqual(slot.details["kill_sent_at"], clock())

            clock.advance(sup.TIMEOUT_KILL_GRACE_SECONDS)
            self.assertFalse(supervisor._reap_one(slot.process_id, child, slot))
            self.assertEqual(slot.status, "orphaned")
            self.assertEqual(slot.details["reason"], "timeout_kill_grace_expired")
            self.assertTrue(slot.details["fence_retained"])
            self.assertFalse(slot.details["exit_confirmed"])
            self.assertEqual(slot.pid, 88_001)
            self.assertEqual(slot.start_identity, "timeout-child")

            child._exit_code = -9
            self.assertTrue(supervisor._reap_one(slot.process_id, child, slot))
            self.assertEqual(slot.status, "timed_out")
            self.assertEqual(slot.attempt, 1)
            self.assertTrue(slot.details["retry_exhausted"])
            self.assertEqual(slot.details["failure_class"], "timeout")
            self.assertIsNone(slot.pid)
            self.assertIsNone(slot.start_identity)
            self.assertNotIn(slot.process_id, supervisor._children)

            supervisor.store.upsert_slot(slot)
            self.assertEqual(supervisor._dispatch_due(), [])

    def test_reap_unknown_process_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(enabled={pid: False for pid in PROCESS_IDS}))
            supervisor = sup.Supervisor(
                python=root / "python",
                config_path=config,
                db_path=root / "state.sqlite",
                dry_run=True,
                state_root=root / "supervisor",
                candidate_id="a" * 64,
                generation="gen-test",
                config_sha256=sup._config_sha256(config),
            )
            slot = sup.DispatchSlot(
                process_id="not-in-inventory",
                dispatch_id="dispatch",
                command=("python", "-m", "missing"),
                command_digest="d" * 64,
                candidate_id=supervisor.candidate_id,
                generation=supervisor.generation,
                config_sha256=supervisor.config_sha256,
                due_at=0.0,
                status="running",
                pid=1,
                start_identity="ghost",
            )
            child = sup.ChildHandle(pid=1, start_identity="ghost", _exit_code=0)
            with self.assertRaises(sup.SupervisorError):
                supervisor._reap_one(slot.process_id, child, slot)




class SupervisorCliTests(unittest.TestCase):
    def test_main_once_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                _document(enabled={pid: False for pid in PROCESS_IDS}),
            )
            db = root / "state.sqlite"
            # Production CLI reads the durable generation pointer rather than
            # using the candidate environment variable as a bypass.
            generation_path = root / "generation"
            generation_path.write_text("1" * 64 + "\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "FALA_CANDIDATE_ID": "1" * 64,
                    "HERMES_LOKAY_GENERATION_PATH": str(generation_path),
                },
                clear=False,
            ):
                code = sup.main(
                    [
                        "--config",
                        str(config),
                        "--db",
                        str(db),
                        "--dry-run",
                        "--json",
                        "--once",
                    ]
                )
            self.assertEqual(code, 0)
    def test_candidate_pin_rejects_non_hex_and_zero(self) -> None:
        for candidate_id in ("z" * 64, "0" * 64):
            with self.assertRaisesRegex(
                sup.SupervisorError, "non-zero lowercase sha256 hex"
            ):
                sup._validate_candidate_pin(candidate_id)

    def test_candidate_pin_matches_immutable_candidate_manifest(self) -> None:
        candidate_id = "a" * 64
        with mock.patch.object(
            sup,
            "_immutable_candidate_identity",
            return_value=(candidate_id, Path("/candidate")),
        ), mock.patch.object(sup, "_verify_candidate_manifest") as verify:
            self.assertEqual(sup._validate_candidate_pin(candidate_id), candidate_id)
            verify.assert_called_once_with(candidate_id, Path("/candidate"))
        with mock.patch.object(
            sup,
            "_immutable_candidate_identity",
            return_value=(candidate_id, Path("/candidate")),
        ):
            with self.assertRaisesRegex(sup.SupervisorError, "immutable candidate path"):
                sup._validate_candidate_pin("b" * 64)



    def test_supervisor_requires_candidate_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _write_config(root, _document(enabled={pid: False for pid in PROCESS_IDS}))
            with mock.patch.dict(
                os.environ,
                {
                    "FALA_CANDIDATE_ID": "",
                    "HERMES_LOKAY_CANDIDATE_ID": "",
                    "LOKAY_CANDIDATE_ID": "",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(sup.SupervisorError, "non-zero lowercase sha256 hex"):
                    sup.Supervisor(
                        python=root / "python",
                        config_path=config,
                        db_path=root / "state.sqlite",
                        dry_run=True,
                    )
if __name__ == "__main__":
    unittest.main()
