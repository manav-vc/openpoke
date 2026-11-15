"""Check queue limits, ordering, timeouts, retries, and recovery without live APIs."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.services.execution.queue import ExecutionService
from server.services.execution.registry import AgentRegistry


async def until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("Condition did not become true")
        await asyncio.sleep(0.005)


class ExecutionQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = AgentRegistry(Path(self.temp.name) / "agents.db")
        self.agents = [self.registry.create_agent(f"Agent {i}", f"Task {i}") for i in range(6)]
        self.settings = SimpleNamespace(agent_max_concurrency=4, agent_queue_capacity=100,
            agent_queue_wait_seconds=300, agent_execution_timeout_seconds=90,
            agent_shutdown_seconds=0.01, agent_max_followup_stages=5, agent_max_submissions=20)
        self.services = []
        self.deliveries = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.stop()
        self.temp.cleanup()

    def service(self, runner=None, delivery=None, clock=time.time):
        async def default_runner(job):
            return {"success": True, "response": job["instructions"]}
        async def default_delivery(payload, context):
            self.deliveries.append((payload, context))
            return {"success": True}
        service = ExecutionService(self.registry, runner=runner or default_runner,
            delivery=delivery or default_delivery, settings=self.settings, clock=clock)
        self.services.append(service)
        return service

    async def submit(self, service, agent=0, request="job", batch=None, **kwargs):
        batch = batch or service.create_batch("batch:" + request)
        job = await service.submit(self.agents[agent]["id"], "instructions:" + request,
            batch_id=batch["id"], request_id=request, **kwargs)
        return batch, job

    async def test_fast_completion_waits_for_seal_and_groups_are_independent(self):
        service = self.service()
        first, job = await self.submit(service, request="first")
        second, other = await self.submit(service, agent=1, request="second")
        await service.seal_batch(second["id"])
        await service.start()
        await until(lambda: service.job_status(job["id"])["status"] == "succeeded")
        await until(lambda: len(self.deliveries) == 1)
        self.assertEqual(self.deliveries[0][1]["batch_id"], second["id"])
        await service.seal_batch(first["id"])
        await until(lambda: len(self.deliveries) == 2)
        self.assertEqual(service.batch_status(first["id"])["delivery_state"], "delivered")
        with self.assertRaisesRegex(ValueError, "sealed"):
            await service.submit(self.agents[0]["id"], "late", batch_id=first["id"], request_id="late")

    async def test_same_agent_fifo_does_not_occupy_other_worker_slots(self):
        release = asyncio.Event()
        started = []
        async def runner(job):
            started.append(job["request_id"])
            if job["request_id"] == "a1":
                await release.wait()
            return {"success": True, "response": "done"}
        self.settings.agent_max_concurrency = 2
        service = self.service(runner=runner)
        batch = service.create_batch("fifo")
        await self.submit(service, request="a1", batch=batch)
        await self.submit(service, request="a2", batch=batch)
        await self.submit(service, agent=1, request="b1", batch=batch)
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: "b1" in started)
        self.assertEqual(started, ["a1", "b1"])
        release.set()
        await until(lambda: "a2" in started)
        await until(lambda: len(self.deliveries) == 1)

    async def test_global_limit_is_shared_and_never_exceeded(self):
        release = asyncio.Event()
        active = peak = 0
        async def runner(job):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1
            return {"success": True}
        service = self.service(runner=runner)
        batch = service.create_batch("limit")
        for i in range(6):
            await self.submit(service, agent=i, request=str(i), batch=batch)
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: peak == 4)
        self.assertEqual(len([j for j in service.pending_jobs() if j["status"] == "running"]), 4)
        release.set()
        await until(lambda: len(self.deliveries) == 1)
        self.assertEqual(peak, 4)

    async def test_queue_full_is_terminal_and_does_not_stick_group(self):
        self.settings.agent_queue_capacity = 1
        service = self.service()
        batch = service.create_batch("capacity")
        _, accepted = await self.submit(service, request="accepted", batch=batch)
        _, rejected = await self.submit(service, agent=1, request="rejected", batch=batch)
        self.assertEqual(rejected["status"], "rejected")
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: len(self.deliveries) == 1)
        self.assertEqual(len(self.deliveries[0][1]["results"]), 2)
        self.assertEqual(service.job_status(accepted["id"])["status"], "succeeded")

    async def test_expiry_is_distinct_from_execution_timeout(self):
        now = [1000.0]
        service = self.service(clock=lambda: now[0])
        batch, job = await self.submit(service)
        await service.seal_batch(batch["id"])
        now[0] += 301
        await service.start()
        await until(lambda: len(self.deliveries) == 1)
        self.assertEqual(service.job_status(job["id"])["status"], "expired")
        self.assertIsNone(service.job_status(job["id"])["started_at"])

    async def test_restart_preserves_queued_and_holds_interrupted_agent(self):
        first = self.service()
        batch, running = await self.submit(first, request="lost")
        _, held = await self.submit(first, request="held", batch=batch)
        _, runnable = await self.submit(first, agent=1, request="other", batch=batch)
        # Simulate a process disappearing after an atomic claim, before outcome.
        claimed = first._claim_next()
        self.assertEqual(claimed["id"], running["id"])
        await first.seal_batch(batch["id"])
        second = self.service()
        await second.start()
        await until(lambda: second.job_status(runnable["id"])["status"] == "succeeded")
        self.assertEqual(second.job_status(running["id"])["status"], "interrupted")
        self.assertEqual(second.job_status(held["id"])["status"], "queued")
        self.assertEqual(self.registry.get(self.agents[0]["id"])["state"], "needs_review")
        with self.assertRaisesRegex(ValueError, "explicit user"):
            await second.resolve_interrupted(running["id"], "abandon")
        await second.resolve_interrupted(running["id"], "abandon", explicit_user=True)
        await until(lambda: second.job_status(held["id"])["status"] == "succeeded")
        await until(lambda: len(self.deliveries) == 1)

    async def test_duplicate_request_operation_and_trigger_occurrences(self):
        service = self.service()
        batch, job = await self.submit(service, operation_key="operation")
        duplicate = await service.submit(self.agents[0]["id"], "instructions:job", batch_id=batch["id"],
            request_id="other-id", operation_key="operation")
        self.assertEqual(job["id"], duplicate["id"])
        trigger = {"id": 7, "scheduled_for": "2026-11-12T12:00:00Z", "recurrence_rule": None}
        trigger_batch = service.create_batch("trigger")
        triggered = await service.submit(self.agents[1]["id"], "original", batch_id=trigger_batch["id"], request_id="occurrence-a", trigger=trigger)
        await service.seal_batch(trigger_batch["id"])
        again = await service.submit(self.agents[1]["id"], "changed payload", batch_id=trigger_batch["id"], request_id="occurrence-b", trigger=trigger)
        self.assertEqual(triggered["id"], again["id"])
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: len(service.trigger_outcomes()) == 1)
        service.mark_trigger_projected(triggered["id"])
        self.assertEqual(service.trigger_outcomes(), [])

    async def test_dispatch_checks_inspected_version(self):
        service = self.service()
        agent = self.registry.get(self.agents[0]["id"])
        self.registry.update_agent(agent["id"], summary="changed")
        batch = service.create_batch("stale")
        with self.assertRaisesRegex(ValueError, "inspect"):
            await service.submit(agent["id"], "instructions", batch_id=batch["id"], request_id="stale", expected_version=agent["version"])

    async def test_submission_preserves_pending_approval_state_and_reopens_completed(self):
        service = self.service()
        self.registry.update_agent(self.agents[0]["id"], state="waiting_for_user", pending_work=True)
        await self.submit(service, request="approval-continuation")
        pending = self.registry.get(self.agents[0]["id"])
        self.assertEqual(pending["state"], "waiting_for_user")
        self.assertTrue(pending["pending_work"])
        self.registry.update_agent(self.agents[1]["id"], state="completed", pending_work=False,
                                   archived=True, completed_at="2026-01-01T00:00:00+00:00")
        await self.submit(service, agent=1, request="completed-continuation")
        reopened = self.registry.get(self.agents[1]["id"])
        self.assertEqual(reopened["state"], "open")
        self.assertFalse(reopened["archived"])
        self.assertIsNone(reopened["completed_at"])

    async def test_reviewed_retry_runs_before_held_followups_and_is_idempotent(self):
        started = []
        async def runner(job):
            started.append(job["request_id"])
            return {"success": True}
        service = self.service(runner=runner)
        batch, original = await self.submit(service, request="original")
        _, held = await self.submit(service, request="held", batch=batch)
        service._claim_next()
        await service.seal_batch(batch["id"])
        await service.start()
        retry = await service.resolve_interrupted(original["id"], "retry", explicit_user=True)
        again = await service.resolve_interrupted(original["id"], "retry", explicit_user=True)
        self.assertEqual(retry["retry_job"]["id"], again["retry_job"]["id"])
        await until(lambda: service.job_status(held["id"])["status"] == "succeeded")
        self.assertEqual(started, ["retry:" + original["id"], "held"])
        self.assertEqual(service.agent_jobs(self.agents[0]["id"])[0]["id"], retry["retry_job"]["id"])

    async def test_restart_recovers_committed_review_retry_intent(self):
        service = self.service()
        batch, original = await self.submit(service, request="reviewed")
        service._claim_next()
        # Simulate recovery and a committed user decision before retry creation.
        with self.registry.connect(write=True) as conn:
            conn.execute("UPDATE execution_jobs SET status='interrupted',review_resolution='retry' WHERE id=?", (original["id"],))
            conn.execute("UPDATE agents SET state='needs_review' WHERE id=?", (original["agent_id"],))
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: any(j["request_id"] == "retry:" + original["id"] and j["status"] == "succeeded" for j in service.agent_jobs(original["agent_id"])))
        with self.registry.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_jobs WHERE request_id=?", ("retry:" + original["id"],)).fetchone()[0], 1)

    async def test_restart_seals_committed_terminal_turn_but_not_running_turn(self):
        service = self.service()
        finished, job = await self.submit(service, request="finished-turn")
        running, other = await self.submit(service, agent=1, request="running-turn")
        # Simulate a crash after the interaction commits completion, before
        # finally seals its group. An unrelated running turn must stay open.
        with self.registry.connect(write=True) as conn:
            conn.execute("CREATE TABLE interaction_turns(request_id TEXT PRIMARY KEY,generation INTEGER,status TEXT)")
            conn.execute("INSERT INTO interaction_turns VALUES(?,?,'completed')", (finished["request_id"], self.registry.generation))
            conn.execute("INSERT INTO interaction_turns VALUES(?,?,'running')", (running["request_id"], self.registry.generation))
        await service.start()
        await until(lambda: len(self.deliveries) == 1)
        self.assertEqual(self.deliveries[0][1]["batch_id"], finished["id"])
        self.assertTrue(service.batch_status(finished["id"])["sealed"])
        self.assertFalse(service.batch_status(running["id"])["sealed"])

    async def test_uncertain_tool_result_interrupts_and_holds_followup(self):
        async def runner(job):
            return {"success": False, "requires_review": True,
                    "error": "Remote write timed out after dispatch; result unknown"}
        service = self.service(runner=runner)
        batch, job = await self.submit(service, request="uncertain")
        _, held = await self.submit(service, request="followup", batch=batch)
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: service.job_status(job["id"])["status"] == "interrupted")
        self.assertEqual(service.job_status(held["id"])["status"], "queued")
        self.assertEqual(self.registry.get(job["agent_id"])["state"], "needs_review")
        self.assertEqual(service.agent_jobs(job["agent_id"])[0]["id"], job["id"])

    async def test_rejected_reviewed_retry_retains_a_resolvable_review_id(self):
        self.settings.agent_queue_capacity = 1
        service = self.service()
        batch, original = await self.submit(service, request="original")
        with self.registry.connect(write=True) as conn:
            conn.execute("UPDATE execution_jobs SET status='interrupted' WHERE id=?", (original["id"],))
            conn.execute("UPDATE agents SET state='needs_review' WHERE id=?", (original["agent_id"],))
        await self.submit(service, agent=1, request="fills-queue")
        reviewed = await service.resolve_interrupted(original["id"], "retry", explicit_user=True)
        retry = reviewed["retry_job"]
        self.assertEqual(retry["status"], "interrupted")
        self.assertIsNone(retry["started_at"])
        self.assertIn("not started", retry["error"])
        await service.resolve_interrupted(retry["id"], "abandon", explicit_user=True)
        self.assertEqual(self.registry.get(original["agent_id"])["state"], "open")

    async def test_expired_reviewed_retry_can_be_reviewed_again(self):
        now = [1000.0]
        service = self.service(clock=lambda: now[0])
        batch, original = await self.submit(service, request="original")
        with self.registry.connect(write=True) as conn:
            conn.execute("UPDATE execution_jobs SET status='interrupted' WHERE id=?", (original["id"],))
            conn.execute("UPDATE agents SET state='needs_review' WHERE id=?", (original["agent_id"],))
        reviewed = await service.resolve_interrupted(original["id"], "retry", explicit_user=True)
        now[0] += 301
        service._expire_queued()
        retry = service.job_status(reviewed["retry_job"]["id"])
        self.assertEqual(retry["status"], "interrupted")
        self.assertIsNone(retry["started_at"])
        await service.resolve_interrupted(retry["id"], "abandon", explicit_user=True)
        self.assertEqual(self.registry.get(original["agent_id"])["state"], "open")

    async def test_shutdown_marks_uncertain_work_without_replay(self):
        started = asyncio.Event()
        async def runner(job):
            started.set()
            await asyncio.Event().wait()
        service = self.service(runner=runner)
        batch, job = await self.submit(service)
        await service.seal_batch(batch["id"])
        await service.start()
        await started.wait()
        await service.stop()
        self.assertEqual(service.job_status(job["id"])["status"], "interrupted")
        await service.start()
        await until(lambda: len(self.deliveries) == 1)
        self.assertEqual(service.job_status(job["id"])["status"], "interrupted")

    async def test_timeout_marks_review_and_clock_begins_after_claim(self):
        self.settings.agent_execution_timeout_seconds = 0.02
        async def runner(job):
            await asyncio.Event().wait()
        service = self.service(runner=runner)
        batch, job = await self.submit(service)
        await service.seal_batch(batch["id"])
        await service.start()
        result = await service.wait_for_job(job["id"], timeout=2)
        self.assertEqual(result["status"], "interrupted")
        self.assertIn("timed out", result["error"])
        self.assertIsNotNone(result["started_at"])

    def record_intent(self, job, tool="task_email_search"):
        return self.registry.append_event("tool_intent", {"tool": tool},
            agent_id=job["agent_id"], request_id=job["request_id"], generation=job["generation"])

    async def test_read_timeout_preserves_results_and_releases_same_agent_followup(self):
        self.settings.agent_execution_timeout_seconds = 0.02
        sources = []
        async def runner(job):
            if job["request_id"] == "slow-read":
                self.record_intent(job)
                sources.append(self.registry.append_event("tool_result", {"tool": "task_email_search",
                    "success": True, "result": {"thread_id": "found-thread"}}, agent_id=job["agent_id"],
                    request_id=job["request_id"], generation=job["generation"]))
                await asyncio.Event().wait()
            return {"success": True, "response": "Explainer found"}
        service = self.service(runner=runner)
        batch, first = await self.submit(service, request="slow-read")
        _, following = await self.submit(service, request="explainer", batch=batch)
        await service.seal_batch(batch["id"])
        await service.start()
        result = await service.wait_for_job(first["id"], timeout=2)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["result"]["retryable"])
        self.assertFalse(result["result"]["requires_review"])
        self.assertEqual(result["result"]["source_event_ids"], sources)
        self.assertEqual(self.registry.get_event(sources[0])["payload"]["result"]["thread_id"], "found-thread")
        next_result = await service.wait_for_job(following["id"], timeout=2)
        self.assertEqual(next_result["status"], "succeeded")
        self.assertGreaterEqual(next_result["started_at"], result["completed_at"])
        self.assertEqual(self.registry.get(first["agent_id"])["state"], "open")

    async def test_timeout_after_any_write_or_unknown_tool_keeps_review(self):
        self.settings.agent_execution_timeout_seconds = 0.02
        for index, tool in enumerate(("gmail_execute_draft", "createTrigger", "updateTrigger", "new_unclassified_tool")):
            async def runner(job, effect_tool=tool):
                self.record_intent(job, effect_tool)
                self.registry.append_event("tool_result", {"tool": effect_tool, "success": True},
                    agent_id=job["agent_id"], request_id=job["request_id"], generation=job["generation"])
                self.record_intent(job)
                await asyncio.Event().wait()
            service = self.service(runner=runner)
            batch, job = await self.submit(service, agent=index, request="write-timeout:" + tool)
            await service.seal_batch(batch["id"])
            await service.start()
            result = await service.wait_for_job(job["id"], timeout=2)
            self.assertEqual(result["status"], "interrupted", tool)
            self.assertFalse(result["result"]["retryable"])
            self.assertTrue(result["result"]["requires_review"])
            self.assertEqual(self.registry.get(job["agent_id"])["state"], "needs_review")
            await service.stop()

    async def test_model_timeout_before_first_tool_is_safe_only_with_tracking_marker(self):
        self.settings.agent_execution_timeout_seconds = 0.02
        async def runner(job):
            self.registry.append_event("execution_effect_tracking", {"enabled": True},
                agent_id=job["agent_id"], request_id=job["request_id"], generation=job["generation"])
            await asyncio.Event().wait()
        service = self.service(runner=runner)
        batch, job = await self.submit(service)
        await service.seal_batch(batch["id"])
        await service.start()
        result = await service.wait_for_job(job["id"], timeout=2)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["result"]["read_only"])

    async def test_restart_of_journaled_read_does_not_hold_followup(self):
        first = self.service()
        batch, original = await self.submit(first, request="lost-read")
        _, following = await self.submit(first, request="followup", batch=batch)
        first._claim_next()
        self.record_intent(original)
        await first.seal_batch(batch["id"])
        restarted = self.service()
        await restarted.start()
        result = await restarted.wait_for_job(following["id"], timeout=2)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(restarted.job_status(original["id"])["status"], "failed")
        self.assertEqual(self.registry.get(original["agent_id"])["state"], "open")

    async def test_legacy_read_timeout_can_be_reconciled_without_losing_history(self):
        service = self.service()
        batch, job = await self.submit(service)
        self.record_intent(job)
        with self.registry.connect(write=True) as conn:
            conn.execute("UPDATE execution_jobs SET status='interrupted',error=? WHERE id=?",
                ("Execution timed out; external effects require review", job["id"]))
            conn.execute("UPDATE agents SET state='needs_review',approval_pending=1 WHERE id=?", (job["agent_id"],))
        _, blocked = await self.submit(service, request="blocked")
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["result"]["blocking_execution_id"], job["id"])
        self.assertTrue(blocked["result"]["blocked"])
        reconciled = service.reconcile_read_only_interruption(job["id"])
        self.assertEqual(reconciled["status"], "failed")
        self.assertTrue(reconciled["result"]["recovered_from_review"])
        self.assertEqual(self.registry.get(job["agent_id"])["state"], "waiting_for_user")
        with self.registry.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='read_only_interruption_reconciled'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='tool_intent'").fetchone()[0], 1)
        _, resumed = await self.submit(service, request="resumed")
        self.assertEqual(resumed["status"], "queued")

    async def test_reconciliation_refuses_writes_unknown_and_missing_evidence(self):
        service = self.service()
        for index, tool in enumerate(("gmail_execute_draft", "unknown_tool", None)):
            _, job = await self.submit(service, agent=index, request="unsafe:" + str(tool))
            if tool:
                self.record_intent(job, tool)
            with self.registry.connect(write=True) as conn:
                conn.execute("UPDATE execution_jobs SET status='interrupted',error=? WHERE id=?",
                    ("Execution timed out; external effects require review", job["id"]))
                conn.execute("UPDATE agents SET state='needs_review' WHERE id=?", (job["agent_id"],))
            with self.assertRaisesRegex(ValueError, "does not prove"):
                service.reconcile_read_only_interruption(job["id"])
            self.assertEqual(service.job_status(job["id"])["status"], "interrupted")

    async def test_only_one_automatic_read_timeout_retry_and_fresh_user_can_continue(self):
        self.settings.agent_execution_timeout_seconds = 0.02
        async def runner(job):
            self.record_intent(job)
            await asyncio.Event().wait()
        service = self.service(runner=runner)
        batch, original = await self.submit(service, request="initial")
        root = batch["root_request_id"]
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: len(self.deliveries) == 1)
        self.assertTrue(self.deliveries[0][1]["allow_followup"])
        retry_batch = service.create_batch("automatic", root_request_id=root, stage=1)
        _, retry = await self.submit(service, request="retry", batch=retry_batch)
        self.assertEqual(retry["retry_of_job_id"], original["id"])
        await service.seal_batch(retry_batch["id"])
        await until(lambda: len(self.deliveries) == 2)
        self.assertFalse(self.deliveries[1][1]["allow_followup"])
        forbidden = service.create_batch("too-many", root_request_id=root, stage=2)
        _, denied = await self.submit(service, request="another-retry", batch=forbidden)
        self.assertEqual(denied["status"], "rejected")
        _, replacement = await self.submit(service, agent=1, request="replacement", batch=forbidden)
        self.assertEqual(replacement["status"], "rejected")
        self.assertIsNone(replacement["started_at"])
        _, new_user = await self.submit(service, request="fresh-user")
        self.assertEqual(new_user["status"], "queued")

    async def test_expired_work_cannot_start_an_automatic_expiry_loop(self):
        now = [1000.0]
        service = self.service(clock=lambda: now[0])
        batch, job = await self.submit(service)
        await service.seal_batch(batch["id"])
        now[0] += 301
        await service.start()
        await until(lambda: len(self.deliveries) == 1)
        self.assertFalse(self.deliveries[0][1]["allow_followup"])
        retry_batch = service.create_batch("expired-retry", root_request_id=batch["root_request_id"], stage=1)
        _, retry = await self.submit(service, request="expired-retry", batch=retry_batch)
        self.assertEqual(retry["status"], "rejected")
        self.assertIsNone(retry["started_at"])

    async def test_read_only_reviewed_retry_timeout_preserves_original_uncertainty(self):
        self.settings.agent_execution_timeout_seconds = 0.02
        async def runner(job):
            self.record_intent(job)
            await asyncio.Event().wait()
        service = self.service(runner=runner)
        _, original = await self.submit(service, request="uncertain-original")
        with self.registry.connect(write=True) as conn:
            conn.execute("UPDATE execution_jobs SET status='interrupted' WHERE id=?", (original["id"],))
            conn.execute("UPDATE agents SET state='needs_review' WHERE id=?", (original["agent_id"],))
        reviewed = await service.resolve_interrupted(original["id"], "retry", explicit_user=True)
        await service.start()
        result = await service.wait_for_job(reviewed["retry_job"]["id"], timeout=2)
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(result["result"]["retryable"])
        self.assertEqual(self.registry.get(original["agent_id"])["state"], "needs_review")
        with self.assertRaisesRegex(ValueError, "Reviewed retry"):
            service.reconcile_read_only_interruption(result["id"])
        _, blocked = await self.submit(service, request="must-not-open")
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(blocked["result"]["blocking_execution_id"], result["id"])

    async def test_restarted_reviewed_read_retry_still_requires_review(self):
        first = self.service()
        _, original = await self.submit(first, request="original-write")
        with self.registry.connect(write=True) as conn:
            conn.execute("UPDATE execution_jobs SET status='interrupted' WHERE id=?", (original["id"],))
            conn.execute("UPDATE agents SET state='needs_review' WHERE id=?", (original["agent_id"],))
        reviewed = await first.resolve_interrupted(original["id"], "retry", explicit_user=True)
        self.assertEqual(first._claim_next()["id"], reviewed["retry_job"]["id"])
        self.record_intent(reviewed["retry_job"])
        restarted = self.service()
        await restarted.start()
        result = restarted.job_status(reviewed["retry_job"]["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(self.registry.get(original["agent_id"])["state"], "needs_review")

    async def test_malformed_and_conflicting_tool_journals_stay_in_review(self):
        service = self.service()
        for index, bad_payload in enumerate(({"tool": []}, {"tool": "gmail_execute_draft", "effect": "read"})):
            _, job = await self.submit(service, agent=index, request="bad-journal:" + str(index))
            self.record_intent(job)
            self.registry.append_event("tool_result", bad_payload, agent_id=job["agent_id"],
                request_id=job["request_id"], generation=job["generation"])
            with self.registry.connect(write=True) as conn:
                conn.execute("UPDATE execution_jobs SET status='interrupted',error=? WHERE id=?",
                    ("Execution timed out; external effects require review", job["id"]))
            with self.assertRaisesRegex(ValueError, "does not prove"):
                service.reconcile_read_only_interruption(job["id"])

    async def test_successful_work_can_add_multiple_distinct_followups(self):
        service = self.service()
        batch, initial = await self.submit(service)
        await service.seal_batch(batch["id"])
        await service.start()
        await service.wait_for_job(initial["id"], timeout=2)
        following = service.create_batch("more-work", root_request_id=batch["root_request_id"], stage=1)
        _, same = await self.submit(service, request="same", batch=following)
        _, different = await self.submit(service, agent=1, request="different", batch=following)
        self.assertEqual(same["status"], "queued")
        self.assertEqual(different["status"], "queued")

    async def test_generation_fences_late_completion_and_reset_restart(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def runner(job):
            started.set()
            await release.wait()
            return {"success": True}
        service = self.service(runner=runner)
        batch, job = await self.submit(service)
        await service.seal_batch(batch["id"])
        await service.start()
        await started.wait()
        self.registry.reset()
        release.set()
        await until(lambda: not service._workers)
        with self.assertRaises(KeyError):
            service.job_status(job["id"])
        self.assertEqual(self.deliveries, [])
        await service.stop()
        await service.start()

    async def test_chain_limits_are_terminal_and_delivery_disables_followups(self):
        service = self.service()
        batch = service.create_batch("limited", stage=6)
        _, job = await self.submit(service, batch=batch)
        self.assertEqual(job["status"], "rejected")
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: len(self.deliveries) == 1)
        self.assertFalse(self.deliveries[0][1]["allow_followup"])

    async def test_failed_delivery_keeps_stable_identity_for_recovery(self):
        now, calls, published = [1000.0], [], set()
        async def delivery(payload, context):
            calls.append(context["request_id"])
            # Simulate the interaction journal's append-once conversation effect.
            published.add(context["request_id"])
            if len(calls) == 1:
                raise RuntimeError("crash after journaled reply")
            return {"success": True}
        service = self.service(delivery=delivery, clock=lambda: now[0])
        batch, job = await self.submit(service)
        await service.seal_batch(batch["id"])
        await service.start()
        await until(lambda: len(calls) == 1 and not service._deliveries)
        now[0] += 10
        service._wake.set()
        await until(lambda: service.batch_status(batch["id"])["delivery_state"] == "delivered")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(published), 1)
        self.assertEqual(calls[0], calls[1])


if __name__ == "__main__":
    unittest.main()
