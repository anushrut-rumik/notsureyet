import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent.arkiv import ArkivError
from agent.config import Config
from agent.inference import InferenceError
from agent.runtime import Runtime


class ChatInput(BaseModel):
    message: str
    user_id: str = "local"


def create_app(config: Config | None = None, *, models=None):
    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(config or Config(), models)
        app.state.runtime = runtime
        worker = None
        try:
            runtime.recover()
            worker = asyncio.create_task(runtime.worker())
            yield
        finally:
            if worker:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            await runtime.close()

    app = FastAPI(title="Your agent", lifespan=lifespan)

    @app.exception_handler(ArkivError)
    async def arkiv_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/health")
    async def health():
        app.state.runtime.store.db.execute("SELECT 1")
        return {"ok": True}

    @app.post("/chat")
    async def chat(body: ChatInput):
        try:
            return {"reply": await app.state.runtime.chat(body.user_id, body.message)}
        except InferenceError as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
            raise HTTPException(503, {"message": str(exc), **exc.details()}, headers=headers) from None
        except (ArkivError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from None
        except Exception as exc:
            raise HTTPException(503, f"Inference failed: {type(exc).__name__}. Check agent config.") from None

    @app.get("/tasks")
    async def tasks(user_id: str = "local"):
        return app.state.runtime.store.tasks(user_id)

    @app.get("/events")
    async def events(user_id: str = "local", after: int = 0):
        return app.state.runtime.store.events(user_id, after=after)

    @app.post("/tasks/{task_id}/reply")
    async def task_reply(task_id: str, body: ChatInput):
        store = app.state.runtime.store
        try:
            task = store.task(body.user_id, task_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        store.event(body.user_id, "task_reply", {"text": body.message}, task_id=task_id, source="user")
        store.update_task(
            body.user_id,
            task_id,
            status="running" if task["status"] == "running" else "queued",
            instruction=task["instruction"] + "\nUser update: " + body.message,
            wake_at=None,
            failures=0,
        )
        return {"resumed": task_id}

    return app
