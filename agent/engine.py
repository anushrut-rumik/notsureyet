import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

from agent import prompts
from agent.channel_updates import ChannelUpdate
from agent.chat_errors import record_chat_error
from agent.inference import InferenceError
from agent.reactions import ChatReply, decide, parse_reply, reaction_delay
from agent.tools import Context


def compact(messages: list[dict], limit=60000) -> list[dict]:
    """Drop old complete exchanges, never half of an assistant/tool exchange."""
    groups = []
    for message in messages:
        if message["role"] == "tool" and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    while len(groups) > 8 and len(json.dumps(groups)) > limit:
        groups.pop(0)
    return [message for group in groups for message in group]


class Engine:
    def __init__(self, config, store, models, registry):
        self.config, self.store, self.models, self.registry = config, store, models, registry
        self.locks = defaultdict(asyncio.Lock)
        self.reaction_jobs = set()
        self.update_retry_at = {}

    async def close(self):
        jobs = list(self.reaction_jobs)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def quick_reaction(self, user, event_id, text, recent, read_at):
        emoji = await decide(self.models, recent, text)
        if emoji:
            # Timing is measured from read, not from completion of inference.
            await asyncio.sleep(max(0, read_at + reaction_delay(text, emoji) - time.monotonic()))
            try:
                self.store.react(user, event_id, emoji)
            except (OSError, sqlite3.Error):
                pass  # An optional reaction must never fail the user's request.

    def system(self, ctx):
        from agent.payments import capabilities

        data = {
            "now_utc": datetime.now(UTC).isoformat(),
            "memories": self.store.memories(ctx.user_id),
            "payment_capabilities": capabilities(self.wallets),
        }
        if ctx.task_id:
            data["task"] = self.store.task(ctx.user_id, ctx.task_id)
            data["payments"] = [item for item in self.wallets.list(ctx.user_id) if item["task_id"] == ctx.task_id]
        else:
            # Prompt context is a local routing hint, not an authoritative task listing.
            # A greeting must not need a remote Arkiv query. list_tasks still queries Arkiv.
            data["task_routing_context"] = {
                "source": "local execution context; use list_tasks for authoritative status",
                "tasks": [
                    dict(row)
                    for row in self.store.db.execute(
                        "SELECT id,title,status,notes FROM tasks WHERE user_id=? ORDER BY updated_at DESC LIMIT 20",
                        (ctx.user_id,),
                    )
                ],
            }
        if ctx.mode in {"chat", "task"}:
            # Both reply writers need to see what the user was already told, including
            # command results and notices outside this model's own conversation.
            rows = self.store.db.execute(
                "SELECT id,task_id,data FROM events WHERE user_id=? AND kind='chat_message' ORDER BY id DESC LIMIT 24",
                (ctx.user_id,),
            ).fetchall()
            data["recent_channel_messages"] = [
                {"event_id": row["id"], "task_id": row["task_id"], **json.loads(row["data"])} for row in reversed(rows)
            ]
        if hasattr(self, "lending"):
            data["lending_mode"] = self.lending.mode
            data["lending_rules"] = (
                "All user interactions use natural language. Never require slash commands, JSON, shell commands "
                "or technical approval codes. Interpret the user's request and CALL the matching tools. "
                "For 'manage/invest my tokens', 'set up CRE' or 'get started', check lending_setup_status; "
                "if needs_setup, call lending_prepare_setup and show its exact review. If setup is ready, "
                "call lending_create_plan, using duration_seconds or days and check_interval_seconds from the request. "
                "Plans support hours and minutes: two hours is duration_seconds=7200; ten-minute checks are "
                "check_interval_seconds=600. Never round an hourly plan up to a day or change the requested duration. "
                "Setup is separate from the management plan and moves no tokens. Never claim setup is complete "
                "until the tool confirms ready. If deploying, give status rather than proposing another setup. "
                "For setup progress, waiting time, a timer, or 'is it ready', call lending_setup_status now; "
                "do not answer from old chat messages. Its message explains the current stage, completed work, "
                "remaining wait and user action. Status tools return facts, not a required reply. "
                "After reading them, continue the user's original request. Answer strategy questions and "
                "recommendations directly instead of repeating the setup announcement or its 'tell me how' prompt. "
                "For recommendations, inspect lending_portfolio and lending_markets as needed to ground the "
                "suggestion in this user's holdings, rates and fees. When the user asks for a plan and setup is "
                "ready, continue to lending_create_plan rather than stopping at setup status. "
                "Only lending_prepare_setup and lending_create_plan show exact approval reviews; "
                "do not show one merely because you checked status. Never invent a countdown "
                "or call network confirmation a new deployment. "
                "For 'stop updates' or 'only tell me when ready', "
                "call lending_setup_notifications with ready_only=true. "
                "Call it with false to resume updates. "
                "Do not promise a preference change without saving it with this tool. "
                "Approving the exact management plan automatically queues funding and deposit in the worker. "
                "Never ask for another start instruction or approval after plan approval. 'awaiting_deposit' "
                "means startup is in progress, not that the user still needs to start it. Management is active "
                "only after the deposit is verified. For an explicit start/retry request use lending_start. "
                "Use liveProgress.startupProgress to distinguish policy registration, fee funding, token funding "
                "and deposit confirmation. Never describe a policy-registration transaction as a token deposit. "
                "For 'stop/pause management' "
                "call lending_pause; for 'withdraw/return my managed tokens' call lending_withdraw; for "
                "'what is happening/how is it doing' call lending_status; for market comparisons call "
                "lending_markets; for holdings call wallet_balances or lending_portfolio. "
                "Collect only missing settings; use the verified beginner preset when none were supplied. "
                "The user must explicitly approve the exact review from WhatsApp. Models cannot approve. "
                "They can reply naturally, such as 'go ahead', 'yes' or 'approve'. "
                "The application handles those raw replies before you run: it records approval and its background "
                "worker submits the six setup transactions. You do not need a model-callable approval tool. "
                "Never say there is no control to deploy. If the user wants to deploy but setup still awaits "
                "approval, explain that approval has not been recorded and call lending_prepare_setup to show "
                "the exact review. For questions about setup, explain the status without replaying the review. "
                "Never give shell commands or raw calldata for lending. Do not call start, pause or withdraw "
                "unless the user requests that action. Routine scheduling is handled by the application. "
                "Keep mock execution, testnet reads, projected interest and confirmed receipts distinct. "
                "Live testnet execution requires reviewed compatible deployments and funded agent wallets. "
                "Never claim a receipt before verification."
            )
        return (
            prompts.PERSONA
            + (
                prompts.TASK
                if ctx.mode == "task"
                else prompts.CHAT
                if ctx.mode == "chat"
                else "Solve the assigned subproblem using tools. Return your findings and evidence IDs."
            )
            + ("\n" + prompts.MESSAGE_STYLE if ctx.mode in {"chat", "task"} else "")
            + "\n"
            + json.dumps(data)
        )

    def repair(self, scope):
        messages = self.store.messages(scope)
        answered = {message["tool_call_id"] for message in messages if message["role"] == "tool"}
        for message in messages:
            for call in message.get("tool_calls", []):
                if call["id"] not in answered:
                    self.store.message(
                        scope,
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(
                                {
                                    "ok": False,
                                    "error": "Session interrupted before recording this result. "
                                    "Inspect effects and reality before repeating actions.",
                                }
                            ),
                        },
                    )

    async def complete(self, ctx, role, messages, tools):
        # Retry only inference, never the surrounding turn or already executed tools.
        for attempt in range(2):
            try:
                timeout = self.config.chat_inference_timeout if ctx.mode == "chat" else None
                async with asyncio.timeout(timeout):
                    return await self.models.complete(role, messages, tools)
            except TimeoutError:
                raise InferenceError(
                    "response_timeout",
                    "The model took too long to respond. This model request was stopped; "
                    "check the existing task or receipt before retrying a payment.",
                ) from None
            except InferenceError as exc:
                delay = max(0.5, exc.retry_after or 1)
                if ctx.mode != "chat" or attempt or not exc.retryable or delay > 3:
                    raise
                self.store.event(ctx.user_id, "inference_retry", {"attempt": 1, **exc.details()})
                await asyncio.sleep(delay)

    async def loop(self, ctx, role):
        self.repair(ctx.scope)
        for _ in range(self.config.steps_per_run):
            if ctx.task_id and self.store.task(ctx.user_id, ctx.task_id)["status"] == "cancelled":
                ctx.stopped = True
                return ""
            messages = [{"role": "system", "content": self.system(ctx)}, *compact(self.store.messages(ctx.scope))]
            answer = await self.complete(ctx, role, messages, self.registry.schemas(ctx.mode))
            if answer.metadata:
                self.store.event(ctx.user_id, "inference_usage", answer.metadata, task_id=ctx.task_id)
            self.store.message(ctx.scope, answer.message())
            if ctx.task_id:
                self.store.flush_task(ctx.task_id)
                await self.store.checkpoint_barrier(ctx.task_id)
            if not answer.calls:
                if ctx.mode != "task":
                    return parse_reply(answer.text) if ctx.mode == "chat" else answer.text
                self.store.message(
                    ctx.scope,
                    {
                        "role": "user",
                        "content": "Continue the work. End with finish_task and evidence, wait_until, or ask_user.",
                    },
                )
                continue
            payment_tools = {
                "prepare_payment",
                "refresh_payment_review",
                "simulate_ccip_transfer",
                "ccip_simulation_status",
                "prepare_ccip_sender_deployment",
            }
            payment_turn = ctx.mode == "chat" and any(
                call["function"]["name"] in payment_tools for call in answer.calls
            )
            if ctx.mode == "chat" and ctx.interaction:
                await ctx.interaction.working()
            for call in answer.calls:
                if ctx.task_id and self.store.task(ctx.user_id, ctx.task_id)["status"] == "cancelled":
                    ctx.stopped = True
                if ctx.stopped:
                    result = {"ok": False, "error": "The task ended or paused earlier in this step. Tool was not run."}
                elif payment_turn and (call["function"]["name"] not in payment_tools or ctx.payment_response):
                    result = {"ok": False, "error": "Payment validation takes priority; other actions were not run."}
                elif ctx.mode == "chat" and ctx.review_response:
                    result = {"ok": False, "error": "Review the exact lending plan before any further action."}
                elif ctx.mode == "chat" and ctx.started_tasks:
                    result = {"ok": False, "error": "Work was handed to the background task. Tool was not run here."}
                else:
                    result = await self.registry.execute(ctx, call)
                    if (
                        ctx.mode == "chat"
                        and call["function"]["name"] in payment_tools
                        and not result.get("ok")
                        and not ctx.payment_response
                    ):
                        ctx.payment_response = "Cannot prepare this payment: " + result["error"]
                shown = json.dumps(result, ensure_ascii=False, default=str)
                if len(shown) > 16000:
                    shown = json.dumps(
                        {
                            "event_id": result.get("event_id"),
                            "preview": shown[:15000],
                            "note": "Read the task journal for the complete stored result.",
                        }
                    )
                self.store.message(ctx.scope, {"role": "tool", "tool_call_id": call["id"], "content": shown})
                if ctx.task_id:
                    self.store.flush_task(ctx.task_id)
                    await self.store.checkpoint_barrier(ctx.task_id)
            if ctx.mode == "chat" and (ctx.review_response or ctx.payment_response):
                reply = ChatReply(emoji_reaction=None, response_messages=[ctx.review_response or ctx.payment_response])
                # Exact reviews bypass model composition, but the next turn must
                # still see the assistant message that was actually sent.
                self.store.message(ctx.scope, {"role": "assistant", "content": reply.model_dump_json()})
                return reply
            if ctx.mode == "chat" and ctx.started_tasks:
                return await self.acknowledge(ctx, role)
            if ctx.stopped:
                return ""
        return None

    async def acknowledge(self, ctx, role):
        """Explain the handoff with the model, without granting another action turn."""
        fallback = "task started. i'll send the result or ask if i need a detail from you"
        bridge = getattr(self.store, "bridge", None)
        if bridge and hasattr(bridge, "pending") and bridge.pending(ctx.user_id):
            fallback = (
                "your wallet signature is needed to save the task on Arkiv. "
                "use the signing link in this chat. work hasn't started yet"
            )
        messages = [{"role": "system", "content": self.system(ctx)}, *compact(self.store.messages(ctx.scope))]
        messages.append(
            {
                "role": "system",
                "content": (
                    "A task was created. If its tool result says awaiting_wallet_approval, explain that it is "
                    "waiting for the wallet signature and has not been stored on Arkiv or started yet. "
                    "Otherwise it was saved for background execution. Briefly acknowledge the specific request "
                    "in the user's language. Do not claim completion or ask them to use commands. Do not call tools."
                ),
            }
        )
        try:
            answer = await self.complete(ctx, role, messages, [])
            if answer.metadata:
                self.store.event(ctx.user_id, "inference_usage", answer.metadata)
            if answer.text.strip() and not answer.calls:
                reply = parse_reply(answer.text)
                if not reply.response_messages:
                    raise ValueError("A task handoff needs a written acknowledgement")
                self.store.message(ctx.scope, answer.message())
                return reply
        except Exception as exc:
            record_chat_error(self.store, ctx.user_id, exc, phase="task_acknowledgement")
        self.store.message(ctx.scope, {"role": "assistant", "content": fallback})
        return ChatReply(emoji_reaction=None, response_messages=[fallback])

    async def chat(self, user_id, text, *, commands=None, interaction=None):
        scope = f"chat:{user_id}"
        async with self.locks[scope]:
            self.repair(scope)
            data = {"role": "user", "content": text}
            if interaction:
                data["channel"] = interaction.metadata
            event_id = self.store.event(user_id, "chat_message", data, source="user")
            self.store.message(scope, {"role": "user", "content": text})
            read = await interaction.read() if interaction else True
            read_at = time.monotonic()
            if read:
                self.store.event(user_id, "chat_read", {"message_event_id": event_id})
            eligible = bool(text.strip()) and read and (not interaction or interaction.reactions)
            if eligible:
                rows = self.store.db.execute(
                    "SELECT data FROM events WHERE user_id=? AND kind='chat_message' AND id<? ORDER BY id DESC LIMIT 6",
                    (user_id, event_id),
                ).fetchall()
                recent = [json.loads(row[0]) for row in reversed(rows)]
                job = asyncio.create_task(self.quick_reaction(user_id, event_id, text, recent, read_at))
                self.reaction_jobs.add(job)
                job.add_done_callback(self.reaction_jobs.discard)
            if commands is not None:
                try:
                    reply = await commands.handle(user_id, text, user_event=event_id)
                except ValueError as exc:
                    reply = str(exc)
                if reply is not None:
                    if isinstance(reply, ChannelUpdate):
                        ctx = Context(self, user_id, scope, f"turn:{event_id}", event_id)
                        reply = await self.compose_update(ctx, reply.facts)
                        reply = "\n\n".join(reply.response_messages)
                    self.store.message(scope, {"role": "assistant", "content": reply})
                    self.store.notice(user_id, reply)
                    self.store.answer_chat(user_id, event_id)
                    return reply
            ctx = Context(self, user_id, scope, f"turn:{event_id}", event_id)
            ctx.interaction = interaction
            reply = await self.loop(ctx, "chat")
            if reply is None:
                if not ctx.started_tasks:
                    task = self.store.create_task(
                        user_id, text[:80], text, "The requested outcome is achieved and verified"
                    )
                    self.store.update_task(user_id, task["id"], notes=json.dumps(self.store.messages(scope)[-8:]))
                reply = ChatReply(
                    emoji_reaction=None,
                    response_messages=["this is continuing. i'll send an update when there's a result"],
                )
                self.store.message(scope, {"role": "assistant", "content": reply.model_dump_json()})
            if not isinstance(reply, ChatReply):
                reply = parse_reply(reply)
            if eligible and reply.emoji_reaction:
                self.store.react(user_id, event_id, reply.emoji_reaction)
            for message in reply.response_messages:
                self.store.notice(user_id, message)
            # Only the completed main turn answers the input. An early reaction
            # while tools/inference continue never sets this bit.
            self.store.answer_chat(user_id, event_id)
            return "\n\n".join(reply.response_messages)

    async def compose_update(self, ctx, facts):
        messages = [{"role": "system", "content": self.system(ctx)}, *compact(self.store.messages(ctx.scope))]
        messages.append({"role": "system", "content": (
            "Write a conversational reply using the application result below and the recent conversation. "
            "This is a writing step only. Do not call tools or claim any unrecorded action. "
            "Explain what changed and any actual next step in plain language. Do not dump status fields, "
            "repeat the plan review, use canned acknowledgements, or tell the user to say a specific phrase. "
            "Approval is distinct from a submitted deposit or active management. Do not invent a dependency "
            "failure, its cause, or claim that funds moved or stayed unchanged without evidence. "
            "When no_token_transfer_verified is true, explicitly say that this operation sent no tokens. "
            "A source_transaction_hash by itself does not prove a token transfer or destination deposit. "
            "When ccip_explorer_url is present, include that exact link so the user can follow the transfer. "
            "When start_requested is true, startup is already queued and needs no follow-up instruction. "
            "Explain that it is starting automatically. Do not ask the user whether to start. "
            "Use the normal emoji_reaction/response_messages JSON format, with at least one written message.\n"
            + json.dumps({"application_result": facts})
        )})
        answer = await self.complete(ctx, "message", messages, [])
        if answer.calls:
            raise ValueError("Channel update composition cannot execute tools")
        reply = parse_reply(answer.text)
        if not reply.response_messages:
            raise ValueError("A channel update needs a written message")
        if answer.metadata:
            self.store.event(ctx.user_id, "inference_usage", answer.metadata)
        return reply

    async def deliver_updates(self):
        rows = self.store.db.execute(
            "SELECT e.id,e.user_id,e.data FROM events e WHERE e.kind='lending_update' "
            "AND NOT EXISTS (SELECT 1 FROM events sent WHERE sent.user_id=e.user_id "
            "AND sent.kind IN ('chat_message','lending_update_discarded') "
            "AND json_extract(sent.data,'$.lendingUpdateId')=e.id) "
            "ORDER BY e.id LIMIT 10"
        ).fetchall()
        for row in rows:
            if time.monotonic() < self.update_retry_at.get(row["id"], 0):
                continue
            user, scope = row["user_id"], f"chat:{row['user_id']}"
            async with self.locks[scope]:
                ctx = Context(self, user, scope, f"update:{row['id']}", 0)
                payload = json.loads(row["data"])
                current = self.lending.read(user)
                plan = current.get("plan") or {}
                facts = payload["facts"]
                if (
                    payload["event"] == "management_check_failed" and not current.get("error")
                    or facts.get("plan_id") and facts["plan_id"] != plan.get("id")
                    or self.newer_operation_update(user, row["id"], facts)
                ):
                    self.store.event(user, "lending_update_discarded", {"lendingUpdateId": row["id"]})
                    continue
                payload["current_state"] = {"plan_status": plan.get("status"), "error": current.get("error")}
                try:
                    reply = await self.compose_update(ctx, payload)
                except Exception as exc:
                    record_chat_error(self.store, user, exc, phase="channel_update")
                    self.update_retry_at[row["id"]] = time.monotonic() + 60
                    continue
                if self.newer_operation_update(user, row["id"], facts):
                    self.store.event(user, "lending_update_discarded", {"lendingUpdateId": row["id"]})
                    continue
                # One durable channel event per update also acts as its delivery marker.
                self.store.event(user, "chat_message", {
                    "role": "assistant", "content": "\n\n".join(reply.response_messages),
                    "lendingUpdateId": row["id"],
                })
                self.store.message(scope, {"role": "assistant", "content": reply.model_dump_json()})
                self.update_retry_at.pop(row["id"], None)

    def newer_operation_update(self, user, event_id, facts):
        operation = facts.get("operation_id")
        return operation and self.store.db.execute(
            "SELECT 1 FROM events WHERE user_id=? AND kind='lending_update' AND id>? "
            "AND json_extract(data,'$.facts.operation_id')=? LIMIT 1", (user, event_id, operation),
        ).fetchone()

    async def work(self, task):
        user, task_id = task["user_id"], task["id"]
        scope = f"task:{task_id}"
        first = self.store.events(user, task_id=task_id, limit=1)[0]["id"]
        ctx = Context(self, user, scope, task_id, first, "task", task_id)
        if not self.store.messages(scope):
            self.store.message(scope, {"role": "user", "content": task["instruction"]})
        try:
            await self.store.checkpoint_barrier(task_id)
            await self.loop(ctx, "planner")
            current = self.store.task(user, task_id)
            if current["status"] == "running":
                self.store.update_task(user, task_id, status="queued", wake_at=time.time() + 1, failures=0)
        except asyncio.CancelledError:
            if self.store.task(user, task_id)["status"] == "running":
                self.store.update_task(user, task_id, status="queued")
            raise
        except Exception as exc:
            failures = task["failures"] + 1
            known = isinstance(exc, InferenceError)
            error = exc.code if known else type(exc).__name__
            details = exc.details() if known else {}
            self.store.event(user, "inference_error", {"type": error, "attempt": failures, **details}, task_id=task_id)
            status = "needs_input" if (known and not exc.retryable) or failures >= 3 else "waiting"
            delay = max(min(2**failures, 60), (exc.retry_after or 0) if known else 0)
            self.store.update_task(
                user,
                task_id,
                status=status,
                failures=failures,
                wake_at=time.time() + delay if status == "waiting" else None,
                last_error=error,
            )
            if status == "needs_input":
                self.store.notice(
                    user,
                    f"{task['title']}: {str(exc) if known else error} Resume the task after resolving it.",
                    task_id,
                )

    async def delegate(self, parent, objective):
        scope = f"worker:{uuid4().hex}"
        self.store.message(scope, {"role": "user", "content": objective})
        ctx = Context(self, parent.user_id, scope, parent.turn_key, parent.user_event, "worker", parent.task_id)
        return {"findings": await self.loop(ctx, "worker")}
