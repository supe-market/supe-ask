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
from .llm import LLMProviderError, llm_service
from .prompts import build_codegen_user_prompt, build_correction_turn_prompt
from .retrieval import retrieval_service
from .run_stream import emit_live_run_event
from .runner import ExecutionBootstrapError, active_runner
from .validator import CodeValidationError, validate_python_code

logger = logging.getLogger(__name__)


MAX_ERROR_RETRY = 5
MAX_CODEGEN_RETRY = 1  # outer loop: one full regen after correction loop exhausted


class ExecutionRetryExhausted(Exception):
    """Correction loop exhausted — signals the outer loop to regenerate code."""

    def __init__(self, error_summary: str) -> None:
        super().__init__(error_summary)
        self.error_summary = error_summary


def _check_output_quality(stdout: str) -> str | None:
    """Scan execution stdout for common data quality signals.

    Returns a human-readable warning string if any signal is detected, else None.
    """
    lower = stdout.lower()
    checks = [
        ("nan", "Some computed values returned NaN — check for division by zero or missing data joins."),
        ("empty dataframe", "One or more queries returned an empty DataFrame — check date filters or join conditions."),
        (" 0 rows", "A query returned 0 rows — results may be incomplete."),
        ("inf", "Some computed values are infinite — possible division by zero."),
    ]
    for pattern, message in checks:
        if pattern in lower:
            return message
    return None


def get_attempt_message(attempt_number: int) -> str:
    messages = {
        1: "Something went off track... Investigating the issue and figuring things out. Hang tight!",
        2: "I've identified what might be causing the issue. Working on a solution now!",
        3: "Got it! I'm implementing the solution... making sure everything lines up perfectly.",
        4: "It's a bit tricky, so I'm applying a workaround. This should be the final touch. Fingers crossed!",
    }
    return messages.get(attempt_number, "This issue is more complex than anticipated. Applying advanced workarounds...")


class RunCancelled(Exception):
    """Raised when a run was cancelled while the pipeline was still in progress."""

    pass


class AskOrchestrator:
    """Coordinate the control-plane side of an Ask run."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        force_flush: bool = False,
    ) -> None:
        """Broadcast a live run event and update the compact persisted stream state."""
        emit_live_run_event(tenant_id, run_id, event_type, payload, force_flush=force_flush)

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
        """Convert retrieval output into an explicit scope-and-assumptions narrative."""
        final_context = retrieved.get("finalContext") or {}
        analysis_plan = final_context.get("analysisPlan") or {}
        relevant_tables = final_context.get("relevantTables") or []
        matched_metrics = analysis_plan.get("matchedMetrics") or []
        matched_entities = analysis_plan.get("matchedEntities") or []
        grouping = analysis_plan.get("grouping") or []
        filters = analysis_plan.get("filters") or []
        outputs = analysis_plan.get("outputs") or []
        intent = str(analysis_plan.get("intent") or "summary").replace("_", " ")
        time_grain = str(analysis_plan.get("matchedTimeGrain") or "mtd").upper()

        question_lower = question.lower()
        explicit_time_tokens = (
            "today",
            "yesterday",
            "mtd",
            "wtd",
            "qtd",
            "ytd",
            "last week",
            "last month",
            "this month",
            "this quarter",
            "last quarter",
            "this year",
            "last year",
            "month",
            "quarter",
            "year",
            "date",
        )
        time_was_explicit = any(token in question_lower for token in explicit_time_tokens)

        assumptions: list[str] = []
        assumptions.append(f"I am treating this as a {intent} question focused on {', '.join(matched_metrics) if matched_metrics else 'the main business metric'}")
        if time_was_explicit:
            assumptions.append(f"I will use the time frame implied in your question, with {time_grain} as the working grain")
        else:
            assumptions.append(f"No explicit period was given, so I am starting with {time_grain} and comparing against the previous comparable period where useful")
        if filters or matched_entities:
            scope_parts = [*matched_entities, *filters]
            assumptions.append(f"I will scope the first pass around {', '.join(dict.fromkeys([str(item) for item in scope_parts]))}")
        else:
            assumptions.append("No entity or geography filter was specified, so I am starting at total business level before drilling into contributors")
        if grouping:
            assumptions.append(f"I will break the answer down by {', '.join(grouping)}")
        else:
            assumptions.append("No breakdown was requested explicitly, so I will add the most useful contributor cut automatically")
        if outputs:
            assumptions.append(f"I will return the result as {', '.join(outputs)}")
        else:
            assumptions.append("I will return a leadership-style answer with KPIs, a chart, and a supporting table")

        preface = f"Here is my working plan for '{question}':"
        table_clause = f"I have {len(relevant_tables)} relevant tables to work with."
        numbered = " ".join(f"{index}. {item}." for index, item in enumerate(assumptions, start=1))
        return f"{preface} {numbered} {table_clause}"

    def _raise_if_cancelled(self, tenant_id: str, run_id: str) -> None:
        """Stop work early if the run has already been cancelled."""
        current = repository.get_run(tenant_id, run_id)
        if current and current.get("status") == "cancelled":
            raise RunCancelled()

    def _execute_with_retry(
        self,
        tenant_id: str,
        run_id: str,
        question: str,
        response: dict[str, Any],
        final_context: dict[str, Any],
        python_code: str,
        *,
        retries_remaining: int = MAX_ERROR_RETRY,
        conversation_history: list[dict[str, Any]],
    ) -> None:
        """Execute generated code with up to MAX_ERROR_RETRY self-correction passes.

        Mirrors ScalerField's recursive ``_handle_execute_code_with_retry`` pattern:
        - attempt_number = MAX_ERROR_RETRY + 1 - retries_remaining
        - On failure: the error turn is appended to ``conversation_history`` and
          passed to the LLM so it has full context of every prior attempt.
        - On correction: the model turn is appended so the next error turn builds
          on the complete exchange — exactly how ScalerField accumulates history.
        - When retries_remaining == 0 and still failing: surface run.failed.
        """
        attempt_number = MAX_ERROR_RETRY + 1 - retries_remaining
        self._emit(
            tenant_id, run_id, "run.execution.started",
            {"runner": active_runner.name, "dispatchMode": "warm_pool", "attempt": attempt_number},
            force_flush=True,
        )

        buffered_events, runtime_errors, return_code = active_runner.execute_sync(run_id, tenant_id, python_code)
        self._raise_if_cancelled(tenant_id, run_id)

        if not runtime_errors and return_code == 0:
            # Success — replay buffered events then complete.
            active_runner.replay_events(tenant_id, run_id, buffered_events)

            # Heuristic quality scan: warn the user if stdout suggests NaN/empty data.
            stdout_text = "\n".join(
                event["payload"].get("line", "")
                for event in buffered_events
                if event["event_type"] == "run.execution.stdout"
            )
            quality_warning = _check_output_quality(stdout_text)
            if quality_warning:
                logger.info("Output quality warning for run %s: %s", run_id, quality_warning)
                self._emit(tenant_id, run_id, "run.planning.delta", {"delta": f"\nNote: {quality_warning}", "chunkIndex": 99})

            repository.update_run_execution(run_id, status="completed", runner_completed=True)
            repository.update_run(run_id, status="completed", completed=True)
            self._emit(tenant_id, run_id, "run.completed", {"status": "completed"}, force_flush=True)
            return

        error_summary, error_detail = runtime_errors[-1] if runtime_errors else ("Non-zero exit", "Runner exited with a non-zero status")
        logger.warning(
            "Ask execution attempt %d failed",
            attempt_number,
            extra={"run_id": run_id, "error": error_summary, "retries_remaining": retries_remaining},
        )

        if retries_remaining <= 0:
            # All correction retries exhausted — signal the outer loop to regenerate.
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=error_summary)
            raise ExecutionRetryExhausted(error_summary)

        # Emit the attempt-specific correction message (ScalerField get_attempt_message style).
        self._emit(
            tenant_id, run_id, "run.thinking",
            {"stage": "correction", "message": get_attempt_message(attempt_number)},
            force_flush=True,
        )
        self._emit(tenant_id, run_id, "run.retry.started", {"attempt": attempt_number + 1, "error": error_summary})

        # Extract stdout lines from the buffered events so the model can see
        # what the code produced before it failed — mirroring ScalerField's
        # format_code_output_for_llm that passes execution output alongside errors.
        execution_output = "\n".join(
            event["payload"].get("line", "")
            for event in buffered_events
            if event["event_type"] == "run.execution.stdout"
        )

        # Append this attempt's error as a "user" turn so the model sees the
        # full exchange up to this point (original prompt → model code →
        # error feedback → model correction → error feedback → ...).
        updated_history = conversation_history + [
            {"role": "user", "parts": [{"text": build_correction_turn_prompt(error_detail, execution_output, current_code=python_code)}]},
        ]

        try:
            correction, correction_raw_text = llm_service.generate_correction(updated_history, current_code=python_code)
            corrected_code = correction["python_code"]
            validate_python_code(corrected_code)
        except (LLMProviderError, CodeValidationError) as error:
            # Correction LLM failed — signal the outer loop to regenerate.
            logger.warning(
                "Self-correction attempt %d failed (%s) — signalling outer loop",
                attempt_number,
                type(error).__name__,
                extra={"run_id": run_id},
            )
            repository.update_run_execution(run_id, status="failed", runner_completed=True, stop_reason=error_summary)
            raise ExecutionRetryExhausted(error_summary)

        # Append the model's correction as a "model" turn so the next error
        # feedback turn lands in proper alternating order.
        updated_history = updated_history + [
            {"role": "model", "parts": [{"text": correction_raw_text}]},
        ]

        # Clear artifacts from the failed attempt so they don't accumulate.
        repository.delete_run_artifacts(run_id)
        self._emit(tenant_id, run_id, "run.artifacts.reset", {}, force_flush=True)

        repository.update_run(run_id, python_code=corrected_code)
        self._emit(
            tenant_id, run_id, "run.codegen.completed",
            {
                "title": response.get("title", ""),
                "assistantSummary": response.get("assistant_summary", ""),
                "pythonCode": corrected_code,
                "artifactPlan": response.get("artifact_plan", {}),
                "corrected": True,
                "attempt": attempt_number + 1,
            },
            force_flush=True,
        )

        # Recurse with the grown history and one fewer retry remaining.
        self._execute_with_retry(
            tenant_id, run_id, question, response, final_context, corrected_code,
            retries_remaining=retries_remaining - 1,
            conversation_history=updated_history,
        )

    def _fail_run(self, tenant_id: str, run_id: str, stage: str, message: str, *, traceback: Any | None = None) -> None:
        """Persist a terminal failure and emit one standardized run event."""
        repository.update_run(run_id, status="failed", error_message=message, completed=True)
        self._emit(
            tenant_id,
            run_id,
            "run.failed",
            {"message": message, "stage": stage, "traceback": traceback},
            force_flush=True,
        )

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
        self._emit(tenant_id, run_id, "run.cancelled", {"status": "cancelled"}, force_flush=True)

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
            self._emit(
                tenant_id,
                run_id,
                "run.thinking",
                {
                    "stage": "retrieval",
                    "message": "Analyzing your question against the data catalog...",
                },
                force_flush=True,
            )

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
            self._emit(
                tenant_id,
                run_id,
                "run.thinking",
                {
                    "stage": "codegen",
                    "message": "Generating analysis code...",
                },
                force_flush=True,
            )
            self._emit(tenant_id, run_id, "run.codegen.started", {})

            final_context = retrieved.get("finalContext") or {}
            prior_failure: str | None = None

            # ── STAGE 3+4: Outer codegen loop — regenerate on correction exhaustion ──
            for outer_attempt in range(MAX_CODEGEN_RETRY + 1):
                if outer_attempt > 0:
                    repository.delete_run_artifacts(run_id)
                    self._emit(tenant_id, run_id, "run.artifacts.reset", {}, force_flush=True)
                    self._emit(
                        tenant_id, run_id, "run.thinking",
                        {"stage": "codegen", "message": "Previous approach failed. Trying a fresh strategy..."},
                        force_flush=True,
                    )
                    self._emit(tenant_id, run_id, "run.codegen.started", {})

                # Inject prior failure context so the model tries a different approach.
                effective_question = question
                if prior_failure:
                    effective_question = (
                        f"{question}\n\n"
                        f"[CONTEXT: A previous code attempt failed after all self-correction passes. "
                        f"Last error: {prior_failure}. "
                        f"Please try a fundamentally different approach — different tables, "
                        f"different aggregation logic, or a simpler query structure.]"
                    )

                accumulated_json = ""

                def on_codegen_chunk(chunk: str) -> None:
                    nonlocal accumulated_json
                    accumulated_json += chunk
                    self._emit(tenant_id, run_id, "run.codegen.delta", {"delta": chunk, "raw": True})

                try:
                    response, codegen_raw_text = llm_service.generate_analysis(
                        effective_question,
                        final_context,
                        on_chunk=on_codegen_chunk,
                    )
                except Exception as error:
                    logger.exception("Ask code generation failed", extra={"run_id": run_id, "tenant_id": tenant_id})
                    self._fail_run(tenant_id, run_id, "codegen", str(error))
                    return

                repository.update_run(
                    run_id,
                    title=response.get("title", ""),
                    assistant_summary=response.get("assistant_summary", ""),
                    python_code=response.get("python_code", ""),
                    artifact_plan=response.get("artifact_plan", {}),
                )
                self._raise_if_cancelled(tenant_id, run_id)

                self._emit(
                    tenant_id,
                    run_id,
                    "run.codegen.completed",
                    {
                        "title": response.get("title", ""),
                        "assistantSummary": response.get("assistant_summary", ""),
                        "pythonCode": response.get("python_code", ""),
                        "artifactPlan": response.get("artifact_plan", {}),
                    },
                    force_flush=True,
                )

                validate_python_code(response.get("python_code", ""))

                if outer_attempt == 0:
                    repository.create_message(tenant_id, thread_id, "assistant", response.get("assistant_summary", ""), run_id=run_id)

                self._emit(
                    tenant_id, run_id, "run.thinking",
                    {"stage": "execution", "message": "Running analysis..."},
                    force_flush=True,
                )

                python_code = response.get("python_code", "")
                conversation_history: list[dict[str, Any]] = [
                    {"role": "user", "parts": [{"text": build_codegen_user_prompt(effective_question, final_context)}]},
                    {"role": "model", "parts": [{"text": codegen_raw_text}]},
                ]

                try:
                    self._execute_with_retry(
                        tenant_id, run_id, question, response, final_context, python_code,
                        conversation_history=conversation_history,
                    )
                    break  # Success — exit outer loop
                except ExecutionRetryExhausted as exc:
                    prior_failure = exc.error_summary
                    if outer_attempt >= MAX_CODEGEN_RETRY:
                        self._fail_run(tenant_id, run_id, "execution", prior_failure)
                        return
                    logger.warning(
                        "Correction loop exhausted (outer attempt %d/%d) — regenerating code from scratch",
                        outer_attempt + 1, MAX_CODEGEN_RETRY + 1,
                        extra={"run_id": run_id},
                    )

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
