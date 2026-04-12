"""Top-level Ask run orchestration.

This service owns the end-to-end lifecycle of a run: retrieval, code generation,
validation, execution start, and terminal event emission.

The pipeline emits live SSE events at every stage so the frontend can show
progressive feedback — thinking animation, streamed narrative, token-level
code streaming, and structured artifacts.
"""

from __future__ import annotations

import logging
from threading import Thread
from typing import Any

from ..auth import AuthUser
from ..repository import repository
from .artifacts import artifact_service
from .llm import LLMProviderError, llm_service
from .retrieval import retrieval_service
from .run_events import emit_run_event
from .runner import ExecutionBootstrapError, active_runner, execute_local_run
from .validator import CodeValidationError, validate_python_code

logger = logging.getLogger(__name__)


class RunCancelled(Exception):
    """Raised when a run was cancelled while the pipeline was still in progress."""

    pass


class AskOrchestrator:
    """Coordinate the control-plane side of an Ask run."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, tenant_id: str, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Persist and broadcast a run event through the shared event bus."""
        emit_run_event(tenant_id, run_id, event_type, payload)

    def _emit_text_chunks(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        text: str,
        *,
        chunk_size: int = 140,
    ) -> None:
        """Emit deterministic UI-friendly text chunks without needing model streaming support."""
        content = (text or "").strip()
        if not content:
            return
        buffer = ""
        index = 0
        for token in content.split():
            candidate = f"{buffer} {token}".strip()
            if buffer and len(candidate) > chunk_size:
                self._emit(tenant_id, run_id, event_type, {"delta": buffer, "chunkIndex": index})
                index += 1
                buffer = token
            else:
                buffer = candidate
        if buffer:
            self._emit(tenant_id, run_id, event_type, {"delta": buffer, "chunkIndex": index})

    def _build_planning_summary(self, question: str, retrieved: dict[str, Any]) -> str:
        """Convert retrieval output into a fast leadership-console style narrative."""
        final_context = retrieved.get("finalContext") or {}
        grounding = final_context.get("questionGrounding") or {}
        relevant_tables = final_context.get("relevantTables") or []
        matched_metrics = grounding.get("matchedMetrics") or []
        matched_entities = grounding.get("matchedEntities") or []
        grouping = grounding.get("grouping") or []
        filters = grounding.get("filters") or []
        intent = grounding.get("intent") or "summary"
        parts = [
            f"Understanding '{question}' as a {intent.replace('_', ' ')} request.",
            f"Matched metrics: {', '.join(matched_metrics) if matched_metrics else 'none yet'}.",
            f"Matched entities: {', '.join(matched_entities) if matched_entities else 'none yet'}.",
            f"Using {len(relevant_tables)} relevant tables.",
        ]
        if grouping:
            parts.append(f"Grouping by {', '.join(grouping)}.")
        if filters:
            parts.append(f"Applying filters on {', '.join(filters)}.")
        return " ".join(parts)

    def _raise_if_cancelled(self, tenant_id: str, run_id: str) -> None:
        """Stop work early if the run has already been cancelled."""
        current = repository.get_run(tenant_id, run_id)
        if current and current.get("status") == "cancelled":
            raise RunCancelled()

    def _fail_run(self, tenant_id: str, run_id: str, stage: str, message: str, *, traceback: Any | None = None) -> None:
        """Persist a terminal failure and emit one standardized run event."""
        repository.update_run(run_id, status="failed", error_message=message, completed=True)
        self._emit(tenant_id, run_id, "run.failed", {"message": message, "stage": stage, "traceback": traceback})

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start_run(self, user: AuthUser, thread_id: str, message_id: str, run_id: str, question: str) -> None:
        """Start the Ask pipeline on a background thread."""
        worker = Thread(
            target=self._run_pipeline,
            args=(user, thread_id, message_id, run_id, question),
            daemon=True,
        )
        worker.start()

    def cancel_run(self, run_id: str, tenant_id: str) -> None:
        """Cancel the active runner and mark the run terminal in the control plane."""
        repository.update_run_execution(run_id, status="cancelled", cancel_requested=True, stop_reason="Run cancelled by user")
        active_runner.cancel(run_id)
        repository.update_run(run_id, status="cancelled", error_message="Run cancelled by user", completed=True)
        self._emit(tenant_id, run_id, "run.cancelled", {"status": "cancelled"})

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def _run_pipeline(self, user: AuthUser, thread_id: str, message_id: str, run_id: str, question: str) -> None:
        """Execute the full Ask flow for one question."""
        tenant_id = user.tenant_id
        try:
            self._raise_if_cancelled(tenant_id, run_id)
            repository.update_run(run_id, status="running")
            self._emit(tenant_id, run_id, "run.created", {"threadId": thread_id, "messageId": message_id, "runId": run_id})

            # ── STAGE 1: Thinking ─────────────────────────────────────
            # Emit immediately so the frontend shows the thinking animation
            # within milliseconds of the question being submitted.
            self._emit(tenant_id, run_id, "run.thinking", {
                "stage": "retrieval",
                "message": "Analyzing your question against the data catalog...",
            })

            # ── STAGE 2: Retrieval ────────────────────────────────────
            self._emit(tenant_id, run_id, "run.retrieval.started", {"question": question})
            try:
                retrieved = retrieval_service.plan_and_retrieve(
                    tenant_id,
                    question,
                    event_handler=lambda event_type, payload: self._emit(tenant_id, run_id, event_type, payload),
                )
            except Exception as error:
                logger.exception("Ask retrieval failed", extra={"run_id": run_id, "tenant_id": tenant_id})
                self._fail_run(tenant_id, run_id, "retrieval", str(error))
                return
            repository.update_run(run_id, retrieval_context=retrieved)
            self._raise_if_cancelled(tenant_id, run_id)

            # Emit planning summary from retrieval results
            self._emit_text_chunks(
                tenant_id,
                run_id,
                "run.planning.delta",
                self._build_planning_summary(question, retrieved),
            )
            self._emit(
                tenant_id,
                run_id,
                "run.retrieval.completed",
                {
                    "strategy": retrieved.get("strategy"),
                    "count": len((retrieved.get("finalContext") or {}).get("relevantTables") or []),
                    "items": (retrieved.get("finalContext") or {}).get("relevantTables") or [],
                },
            )
            self._emit(tenant_id, run_id, "run.planning.completed", {"status": "completed"})

            # ── STAGE 3: Code generation (streamed) ───────────────────
            self._emit(tenant_id, run_id, "run.thinking", {
                "stage": "codegen",
                "message": "Generating analysis code...",
            })
            self._emit(tenant_id, run_id, "run.codegen.started", {})

            # Stream raw JSON tokens to the frontend so the code tab
            # populates in real-time (like ScalarField's live code preview).
            accumulated_json = ""

            def on_codegen_chunk(chunk: str) -> None:
                nonlocal accumulated_json
                accumulated_json += chunk
                self._emit(tenant_id, run_id, "run.codegen.delta", {"delta": chunk, "raw": True})

            try:
                response = llm_service.generate_analysis(
                    question,
                    retrieved.get("finalContext") or {},
                    on_chunk=on_codegen_chunk,
                )
            except Exception as error:
                logger.exception("Ask code generation failed", extra={"run_id": run_id, "tenant_id": tenant_id})
                self._fail_run(tenant_id, run_id, "codegen", str(error))
                return

            repository.update_run(
                run_id,
                title=response["title"],
                assistant_summary=response["assistant_summary"],
                python_code=response["python_code"],
                artifact_plan=response["artifact_plan"],
            )
            self._raise_if_cancelled(tenant_id, run_id)

            # Emit title + summary as a structured header event BEFORE
            # execution starts so the user sees the answer forming.
            self._emit(
                tenant_id,
                run_id,
                "run.codegen.completed",
                {
                    "title": response["title"],
                    "assistantSummary": response["assistant_summary"],
                    "pythonCode": response["python_code"],
                    "artifactPlan": response["artifact_plan"],
                },
            )

            validate_python_code(response["python_code"])

            repository.create_message(tenant_id, thread_id, "assistant", response["assistant_summary"], run_id=run_id)

            # ── STAGE 4: Execution ────────────────────────────────────
            self._emit(tenant_id, run_id, "run.thinking", {
                "stage": "execution",
                "message": "Running analysis...",
            })

            def handle_runner_event(event: dict[str, Any]) -> None:
                """Normalize local-runner output into persisted Ask artifacts/events."""
                event_type = event.get("type")
                payload = dict(event.get("payload") or {})
                if event_type == "progress":
                    self._emit(tenant_id, run_id, "run.execution.progress", payload)
                    return
                if event_type == "stdout":
                    self._emit(tenant_id, run_id, "run.execution.stdout", payload)
                    return
                if event_type == "artifact":
                    artifact_payload = dict(payload.get("payload") or {})
                    artifact_type = str(payload.get("artifact_type") or "unknown")
                    title = str(payload.get("title") or artifact_type.title())
                    artifact = artifact_service.persist_artifact(
                        tenant_id,
                        run_id,
                        artifact_type,
                        title,
                        full_payload=artifact_payload,
                        ordinal=repository.next_artifact_ordinal(tenant_id, run_id),
                        metadata={"source": "local_runner"},
                    )
                    self._emit(tenant_id, run_id, "run.artifact", {"artifact": artifact})
                    return
                if event_type == "error":
                    message = str(payload.get("message") or "Execution failed")
                    raise RuntimeError(message)

            if active_runner.name == "ecs":
                # ECS execution is asynchronous: after launch, the runner takes over
                # and reports progress back through the signed callback endpoint.
                launch_result = active_runner.launch(run_id, tenant_id, response["python_code"])
                self._emit(
                    tenant_id,
                    run_id,
                    "run.execution.started",
                    {"runner": active_runner.name, "taskArn": launch_result.task_arn},
                )
                return

            self._emit(tenant_id, run_id, "run.execution.started", {"runner": active_runner.name})
            repository.upsert_run_execution(tenant_id, run_id, active_runner.name, "running", metadata={"mode": "sync"})
            launch_result = execute_local_run(run_id, tenant_id, response["python_code"], handle_runner_event)

            if launch_result.logs:
                artifact = artifact_service.persist_artifact(
                    tenant_id,
                    run_id,
                    "log",
                    "Execution log",
                    full_payload={"lines": launch_result.logs},
                    ordinal=repository.next_artifact_ordinal(tenant_id, run_id),
                    metadata={"source": "local_runner"},
                )
                self._emit(tenant_id, run_id, "run.artifact", {"artifact": artifact})

            if launch_result.return_code != 0:
                raise RuntimeError("Runner exited with a non-zero status")

            repository.update_run_execution(run_id, status="completed", runner_completed=True)
            repository.update_run(run_id, status="completed", completed=True)
            self._emit(tenant_id, run_id, "run.completed", {"status": "completed"})
        except RunCancelled:
            return
        except ExecutionBootstrapError as error:
            logger.exception("Ask execution bootstrap failed", extra={"run_id": run_id, "tenant_id": tenant_id})
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=str(error))
            self._fail_run(tenant_id, run_id, "execution_bootstrap", str(error))
        except CodeValidationError as error:
            logger.exception("Ask validation failed", extra={"run_id": run_id, "tenant_id": tenant_id})
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=str(error))
            self._fail_run(tenant_id, run_id, "validation", str(error))
        except LLMProviderError as error:
            logger.exception("Unexpected Ask provider failure escaped stage handling", extra={"run_id": run_id, "tenant_id": tenant_id})
            self._fail_run(tenant_id, run_id, "codegen", str(error))
        except Exception as error:
            current = repository.get_run(tenant_id, run_id)
            if current and current.get("status") == "cancelled":
                return
            logger.exception("Ask execution failed", extra={"run_id": run_id, "tenant_id": tenant_id})
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=str(error))
            self._fail_run(tenant_id, run_id, "execution", str(error))


orchestrator = AskOrchestrator()
