"""HTTP routes for the supe-ask control plane."""

from __future__ import annotations

import json
from queue import Empty
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import AuthUser, clear_auth_cookie, require_auth, set_auth_cookie, verify_oauth_code
from ..event_bus import event_bus
from ..repository import repository
from ..services.orchestrator import orchestrator
from ..services.run_stream import normalize_stream_state, run_stream_service

router = APIRouter()


class CreateThreadBody(BaseModel):
    """Payload for creating an Ask thread."""

    title: str | None = None


class CreateMessageBody(BaseModel):
    """Payload for submitting a new Ask question into a thread."""

    question: str = Field(min_length=1, max_length=4000)


@router.get("/cookie")
@router.get("/api/v1/cookie")
async def validate_or_create_cookie(request: Request, response: Response, oauthCode: str | None = None):
    """Create or validate the Ask auth cookie using the shared auth service."""
    if oauthCode:
        verified = await verify_oauth_code(oauthCode)
        if not verified:
            raise HTTPException(status_code=403, detail="oauth not valid")
        token, user = verified
        set_auth_cookie(request, response, token)
        return {"success": True, "message": "cookie-set", "tenantId": user.tenant_id}

    user = await require_auth(request, response)
    return {"success": True, "message": "cookie-valid", "tenantId": user.tenant_id}


@router.delete("/cookie")
@router.delete("/api/v1/cookie")
async def delete_cookie(request: Request, response: Response):
    """Clear the Ask auth cookie."""
    clear_auth_cookie(request, response)
    return {"success": True, "message": "Deleted"}



@router.get("/api/v1/ask/threads")
async def list_threads(user: AuthUser = Depends(require_auth)):
    """List Ask threads for the authenticated tenant."""
    return {"success": True, "data": {"threads": repository.list_threads(user.tenant_id)}}


@router.post("/api/v1/ask/threads")
async def create_thread(body: CreateThreadBody, user: AuthUser = Depends(require_auth)):
    """Create a new Ask thread shell before any messages are added."""
    thread = repository.create_thread(user.tenant_id, user.id, body.title)
    return {"success": True, "data": {"thread": thread}}


@router.delete("/api/v1/ask/threads/{thread_id}")
async def delete_thread(thread_id: str, user: AuthUser = Depends(require_auth)):
    """Archive an Ask thread so it disappears from the sidebar."""
    thread = repository.get_thread(user.tenant_id, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    repository.archive_thread(user.tenant_id, thread_id)
    return {"success": True}


@router.get("/api/v1/ask/threads/{thread_id}")
async def get_thread(thread_id: str, user: AuthUser = Depends(require_auth)):
    """Load a thread together with its messages, runs, and persisted artifacts."""
    thread = repository.get_thread(user.tenant_id, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    runs = repository.list_runs(user.tenant_id, thread_id)
    artifacts_by_run: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        run_id = str(run["id"])
        run["stream_state"] = normalize_stream_state(run.get("stream_state"))
        artifacts_by_run[run_id] = repository.list_artifacts(user.tenant_id, run_id)
    return {
        "success": True,
        "data": {
            "thread": thread,
            "messages": repository.list_messages(user.tenant_id, thread_id),
            "runs": runs,
            "artifactsByRun": artifacts_by_run,
        },
    }


@router.post("/api/v1/ask/threads/{thread_id}/messages")
async def create_message(thread_id: str, body: CreateMessageBody, user: AuthUser = Depends(require_auth)):
    """Persist a user question, create the run record, and kick off orchestration."""
    thread = repository.get_thread(user.tenant_id, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    message = repository.create_message(user.tenant_id, thread_id, "user", body.question)
    title = thread.get("title") or ""
    if title == "New ask thread":
        repository.update_thread(thread_id, body.question[:80].strip())
    run = repository.create_run(user.tenant_id, thread_id, str(message["id"]), body.question)
    repository.update_thread(thread_id)
    orchestrator.start_run(user, thread_id, str(message["id"]), str(run["id"]), body.question)

    return {"success": True, "data": {"message": message, "run": run}}


@router.post("/api/v1/ask/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: AuthUser = Depends(require_auth)):
    """Cancel an in-flight Ask run."""
    run = repository.get_run(user.tenant_id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") in {"completed", "failed", "cancelled"}:
        return {"success": True, "data": {"run": run}}
    orchestrator.cancel_run(run_id, user.tenant_id)
    return {"success": True}


@router.get("/api/v1/ask/runs/{run_id}/events")
async def stream_run_events(run_id: str, user: AuthUser = Depends(require_auth)):
    """Emit one synthetic run snapshot, then stream live run events over SSE."""
    run = repository.get_run(user.tenant_id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    subscription = event_bus.subscribe(run_id)
    queue = subscription.queue

    def event_iterator():
        seen_ids: set[str] = set()
        try:
            current_run = repository.get_run(user.tenant_id, run_id) or run
            current_run["stream_state"] = run_stream_service.get_current_stream_state(
                user.tenant_id,
                run_id,
                current_run.get("stream_state"),
            )
            snapshot_event = {
                "id": run_stream_service.next_event_id(),
                "eventType": "run.snapshot",
                "payload": jsonable_encoder(
                    {
                        "run": current_run,
                        "artifacts": repository.list_artifacts(user.tenant_id, run_id),
                    }
                ),
                "createdAt": jsonable_encoder(current_run["stream_state"].get("updatedAt") or current_run.get("updated_at")),
            }
            seen_ids.add(str(snapshot_event["id"]))
            yield f"data: {json.dumps(snapshot_event)}\n\n"

            terminal_status = {"completed", "failed", "cancelled"}
            while True:
                current = repository.get_run(user.tenant_id, run_id)
                if current and current.get("status") in terminal_status and queue.empty():
                    break
                try:
                    event = queue.get(timeout=1.0)
                except Empty:
                    yield ": ping\n\n"
                    continue
                event_id = str(event.get("id") or "")
                if event_id and event_id in seen_ids:
                    continue
                if event_id:
                    seen_ids.add(event_id)
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            subscription.close()

    return StreamingResponse(event_iterator(), media_type="text/event-stream")
