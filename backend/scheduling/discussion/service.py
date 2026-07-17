from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from db.db_context import create_db_session
from db.models.message import MessageCreate, MessageEntity
from db.models.thread import ThreadCreate
from scheduling.agent_registry import ensure_default_agent_profiles, get_agent_profile
from scheduling.agent_registry.models import AgentProfileEntity
from scheduling.scheduler import (
    SchedulingChatRequest,
    SchedulingRuntimeContext,
    SchedulingRuntimeOptions,
    SchedulingService,
)
from service.thread.message_service import MessageService
from service.thread.thread_service import ThreadService
from sqlmodel import select

from .models import (
    DiscussionParticipantEntity,
    DiscussionRunEntity,
    DiscussionTurnEntity,
)
from .schemas import DiscussionRunCreateRequest


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _text_from_content(content: list[dict] | None) -> str:
    if not content:
        return ""
    return "\n".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _discussion_meta(content: list[dict] | None) -> dict[str, Any]:
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "discussion_meta":
            return dict(part)
    return {}


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"value": str(value)}


def _parse_decision(text: str) -> dict[str, str]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    start = raw.find("{")
    end = raw.rfind("}")
    data: dict[str, Any] = {}
    if start != -1 and end != -1 and start < end:
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            data = {}

    action = str(data.get("action") or "speak").strip().lower()
    if action not in {"speak", "pass", "stop"}:
        action = "speak"
    content = str(data.get("content") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if action == "speak" and not content:
        content = text.strip()
        reason = reason or "The model did not return structured discussion JSON."
    return {"action": action, "content": content, "reason": reason}


def _assistant_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _public_text_for_decision(decision: dict[str, str]) -> str:
    if decision["action"] == "speak":
        return decision["content"].strip()
    if decision["action"] in {"pass", "stop"}:
        return (decision["content"] or decision["reason"]).strip()
    return ""


class DiscussionService:
    def __init__(
        self,
        options: SchedulingRuntimeOptions,
        *,
        runtime_context: SchedulingRuntimeContext | None = None,
    ) -> None:
        self.options = options
        self.runtime_context = runtime_context or SchedulingRuntimeContext()

    async def create_run(self, request: DiscussionRunCreateRequest) -> DiscussionRunEntity:
        agent_ids = [agent_id for agent_id in request.participant_agent_ids if agent_id]
        if not agent_ids:
            raise ValueError("participant_agent_ids must not be empty.")
        max_rounds = max(1, min(request.max_rounds, 10))
        moderator_agent_id = request.moderator_agent_id or agent_ids[-1]

        async with create_db_session() as session:
            await ensure_default_agent_profiles(session)
            profiles: dict[str, AgentProfileEntity] = {}
            for agent_id in agent_ids:
                profile = await get_agent_profile(session, agent_id)
                if profile is None or not profile.enabled:
                    raise ValueError(f"Agent '{agent_id}' not found.")
                profiles[agent_id] = profile
            if moderator_agent_id not in profiles:
                raise ValueError("moderator_agent_id must be one of participant_agent_ids.")

            thread_service = ThreadService(session)
            public_thread = await thread_service.create_thread(
                ThreadCreate(user_id=request.user_id, title=request.topic[:80]),
            )
            run = DiscussionRunEntity(
                public_thread_id=public_thread.id,
                user_id=request.user_id,
                topic=request.topic,
                participant_agent_ids=agent_ids,
                moderator_agent_id=moderator_agent_id,
                max_rounds=max_rounds,
            )
            session.add(run)
            await session.flush()
            await session.refresh(run)

            for index, agent_id in enumerate(agent_ids):
                private_thread = await thread_service.create_thread(
                    ThreadCreate(
                        user_id=request.user_id,
                        title=f"{request.topic[:50]} / {agent_id}",
                    )
                )
                participant = DiscussionParticipantEntity(
                    run_id=run.id,
                    agent_id=agent_id,
                    agent_session_id=f"discussion:{run.id}:{agent_id}",
                    agent_thread_id=private_thread.id,
                    agent_profile_snapshot=profiles[agent_id].to_read_model(),
                    position=index,
                )
                session.add(participant)

            await self._create_public_message(
                session=session,
                run=run,
                speaker_type="user",
                speaker_id=request.user_id,
                speaker_name="User",
                role="user",
                text=request.topic,
                round_index=0,
                turn_id=None,
            )
            run.status = "pending"
            run.updated_at = _utc_now_naive()
            session.add(run)
            await session.flush()
            await session.refresh(run)
            return run

    async def add_user_message(
        self,
        run_id: str,
        content: str,
        user_id: str = "default_user",
    ) -> dict[str, Any]:
        async with create_db_session() as session:
            run = await session.get(DiscussionRunEntity, run_id)
            if run is None:
                raise ValueError(f"Discussion run '{run_id}' not found.")
            message = await self._create_public_message(
                session=session,
                run=run,
                speaker_type="user",
                speaker_id=user_id,
                speaker_name="User",
                role="user",
                text=content,
                round_index=run.current_round,
                turn_id=None,
            )
            run.updated_at = _utc_now_naive()
            session.add(run)
            return self._message_payload(message)

    async def execute_run(self, run_id: str) -> dict[str, Any]:
        await self._mark_run_started(run_id)
        stop = False
        for round_index in range(1, (await self._get_run(run_id)).max_rounds + 1):
            spoke_in_round = False
            participants = await self._list_participants(run_id)
            for participant in participants:
                result = await self._execute_turn(run_id, participant, round_index)
                if result["action"] == "speak":
                    spoke_in_round = True
                if result["action"] == "stop" and result["agent_id"] == result["moderator_agent_id"]:
                    stop = True
                    break
            await self._update_round(run_id, round_index)
            if stop or not spoke_in_round:
                break
        await self._mark_run_finished(run_id)
        return await self.get_run_payload(run_id)

    async def stream_run(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        await self._mark_run_started(run_id)
        yield {"event": "discussion_started", "data": {"run_id": run_id}}
        stop = False
        for round_index in range(1, (await self._get_run(run_id)).max_rounds + 1):
            spoke_in_round = False
            participants = await self._list_participants(run_id)
            for participant in participants:
                async for item in self._stream_turn(run_id, participant, round_index):
                    yield item
                    if item["event"] == "turn_finished":
                        action = item["data"].get("action")
                        if action == "speak":
                            spoke_in_round = True
                        if (
                            action == "stop"
                            and item["data"].get("agent_id")
                            == item["data"].get("moderator_agent_id")
                        ):
                            stop = True
                if stop:
                    break
            await self._update_round(run_id, round_index)
            if stop or not spoke_in_round:
                break
        payload = await self._mark_run_finished(run_id)
        yield {"event": "discussion_finished", "data": payload}

    async def get_run_payload(self, run_id: str) -> dict[str, Any]:
        async with create_db_session() as session:
            run = await session.get(DiscussionRunEntity, run_id)
            if run is None:
                raise ValueError(f"Discussion run '{run_id}' not found.")
            participants = await self._list_participants_with_session(session, run_id)
            turns = await self._list_turns_with_session(session, run_id)
            messages = await MessageService(session).list_messages(run.public_thread_id)

        turns_by_message = {
            turn.public_message_id: self._turn_payload(turn)
            for turn in turns
            if turn.public_message_id
        }
        return {
            "run": self._run_payload(run),
            "participants": [self._participant_payload(item) for item in participants],
            "messages": [
                {
                    **self._message_payload(message),
                    "turn": turns_by_message.get(message.id),
                }
                for message in messages
            ],
            "turns": [self._turn_payload(turn) for turn in turns],
        }

    async def _execute_turn(
        self,
        run_id: str,
        participant: DiscussionParticipantEntity,
        round_index: int,
    ) -> dict[str, Any]:
        run = await self._get_run(run_id)
        turn = await self._create_turn(run, participant, round_index)
        try:
            profile = await self._get_profile(participant.agent_id)
            prompt = await self._build_agent_prompt(run, participant)
            request = SchedulingChatRequest(
                message=prompt,
                user_id=run.user_id,
                thread_id=participant.agent_thread_id,
                session_id=participant.agent_session_id,
                stream=False,
            )
            result = await SchedulingService(self.options).chat(
                profile,
                request,
                runtime_context=self.runtime_context,
            )
            content = _assistant_content(result.get("response") or {})
            decision = _parse_decision(content)
            public_message = None
            public_text = _public_text_for_decision(decision)
            if public_text:
                async with create_db_session() as session:
                    fresh_run = await session.get(DiscussionRunEntity, run_id)
                    public_message = await self._create_public_message(
                        session=session,
                        run=fresh_run,
                        speaker_type="agent",
                        speaker_id=participant.agent_id,
                        speaker_name=participant.agent_profile_snapshot.get("name")
                        or participant.agent_id,
                        role="assistant",
                        text=public_text,
                        round_index=round_index,
                        turn_id=turn.id,
                    )
                    turn.public_message_id = public_message.id

            await self._finish_turn(turn, decision, result, public_message)
            return {
                "agent_id": participant.agent_id,
                "moderator_agent_id": run.moderator_agent_id,
                **decision,
            }
        except Exception as exc:
            await self._fail_turn(turn, exc)
            return {
                "agent_id": participant.agent_id,
                "moderator_agent_id": run.moderator_agent_id,
                "action": "pass",
                "content": "",
                "reason": f"Turn failed: {exc}",
            }

    async def _stream_turn(
        self,
        run_id: str,
        participant: DiscussionParticipantEntity,
        round_index: int,
    ) -> AsyncIterator[dict[str, Any]]:
        run = await self._get_run(run_id)
        turn = await self._create_turn(run, participant, round_index)
        yield {
            "event": "turn_started",
            "data": {
                "run_id": run_id,
                "turn_id": turn.id,
                "agent_id": participant.agent_id,
                "round_index": round_index,
                "turn_index": turn.turn_index,
            },
        }
        result: dict[str, Any] = {"response": {"steps": []}}
        final_content = ""
        try:
            profile = await self._get_profile(participant.agent_id)
            prompt = await self._build_agent_prompt(run, participant)
            request = SchedulingChatRequest(
                message=prompt,
                user_id=run.user_id,
                thread_id=participant.agent_thread_id,
                session_id=participant.agent_session_id,
                stream=True,
            )
            async for event in SchedulingService(self.options).stream_chat(
                profile,
                request,
                runtime_context=self.runtime_context,
            ):
                single_event = event.model_dump()
                data = event.data or {}
                if event.event == "metadata":
                    result["skills"] = data.get("skills")
                if event.event == "chunk":
                    choices = data.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        final_content += delta.get("content") or ""
                    if data.get("observation"):
                        result.setdefault("response", {}).setdefault("steps", []).append(data["observation"])
                if event.event == "final":
                    final_content = str((data or {}).get("content") or final_content)
                    result.setdefault("response", {})["usage"] = (data or {}).get("usage")
                yield {
                    "event": event.event,
                    "data": {
                        "run_id": run_id,
                        "turn_id": turn.id,
                        "agent_id": participant.agent_id,
                        "round_index": round_index,
                        "turn_index": turn.turn_index,
                        "single_event": single_event,
                    },
                }

            result.setdefault("response", {}).setdefault("choices", [
                {"message": {"content": final_content}}
            ])
            decision = _parse_decision(final_content)
            public_message = None
            public_text = _public_text_for_decision(decision)
            if public_text:
                async with create_db_session() as session:
                    fresh_run = await session.get(DiscussionRunEntity, run_id)
                    public_message = await self._create_public_message(
                        session=session,
                        run=fresh_run,
                        speaker_type="agent",
                        speaker_id=participant.agent_id,
                        speaker_name=participant.agent_profile_snapshot.get("name")
                        or participant.agent_id,
                        role="assistant",
                        text=public_text,
                        round_index=round_index,
                        turn_id=turn.id,
                    )
                    turn.public_message_id = public_message.id
            await self._finish_turn(turn, decision, result, public_message)
            yield {
                "event": "turn_finished",
                "data": {
                    "run_id": run_id,
                    "turn_id": turn.id,
                    "agent_id": participant.agent_id,
                    "moderator_agent_id": run.moderator_agent_id,
                    "round_index": round_index,
                    "turn_index": turn.turn_index,
                    **decision,
                    "public_message_id": public_message.id if public_message else None,
                },
            }
        except Exception as exc:
            await self._fail_turn(turn, exc)
            yield {
                "event": "turn_finished",
                "data": {
                    "run_id": run_id,
                    "turn_id": turn.id,
                    "agent_id": participant.agent_id,
                    "moderator_agent_id": run.moderator_agent_id,
                    "round_index": round_index,
                    "turn_index": turn.turn_index,
                    "action": "pass",
                    "content": "",
                    "reason": f"Turn failed: {exc}",
                    "error": str(exc),
                },
            }

    async def _create_public_message(
        self,
        session,
        run: DiscussionRunEntity,
        speaker_type: str,
        speaker_id: str,
        speaker_name: str,
        role: str,
        text: str,
        round_index: int,
        turn_id: str | None,
    ) -> MessageEntity:
        meta = {
            "type": "discussion_meta",
            "run_id": run.id,
            "speaker_type": speaker_type,
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "round_index": round_index,
            "turn_id": turn_id,
        }
        return await MessageService(session).create_message(
            MessageCreate(
                thread_id=run.public_thread_id,
                role=role,
                content=[meta, {"type": "text", "text": text}],
            )
        )

    async def _build_agent_prompt(
        self,
        run: DiscussionRunEntity,
        participant: DiscussionParticipantEntity,
    ) -> str:
        transcript = await self._format_transcript(run.public_thread_id)
        return "\n\n".join(
            [
                "You are participating in a multi-agent discussion.",
                f"Discussion topic:\n{run.topic}",
                f"Current public transcript:\n{transcript or '(no prior messages)'}",
                (
                    f"You are {participant.agent_id}. Speak only from this agent's "
                    "perspective and do not impersonate other agents."
                ),
                (
                    "If useful, call your available tools first. Your final answer "
                    "must be valid JSON only, with exactly this shape:\n"
                    '{"action":"speak|pass|stop","content":"...","reason":"..."}'
                ),
                (
                    "Choose speak only when you add new evidence, analysis, or a "
                    "useful objection. Choose pass when you have no new contribution. "
                    "Choose stop when the discussion is complete."
                ),
            ]
        )

    async def _format_transcript(self, thread_id: str) -> str:
        async with create_db_session() as session:
            messages = await MessageService(session).list_messages(thread_id)
        lines = []
        for message in messages:
            meta = _discussion_meta(message.content)
            speaker = meta.get("speaker_id") or message.role
            text = _text_from_content(message.content).strip()
            if text:
                lines.append(f"[{speaker}] {text}")
        return "\n".join(lines)

    async def _create_turn(
        self,
        run: DiscussionRunEntity,
        participant: DiscussionParticipantEntity,
        round_index: int,
    ) -> DiscussionTurnEntity:
        async with create_db_session() as session:
            statement = select(DiscussionTurnEntity).where(
                DiscussionTurnEntity.run_id == run.id
            )
            result = await session.exec(statement)
            turn_index = len(list(result.all())) + 1
            turn = DiscussionTurnEntity(
                run_id=run.id,
                round_index=round_index,
                turn_index=turn_index,
                agent_id=participant.agent_id,
                private_thread_id=participant.agent_thread_id,
                private_session_id=participant.agent_session_id,
                status="running",
                started_at=_utc_now_naive(),
            )
            session.add(turn)
            await session.flush()
            await session.refresh(turn)
            return turn

    async def _finish_turn(
        self,
        turn: DiscussionTurnEntity,
        decision: dict[str, str],
        result: dict[str, Any],
        public_message: MessageEntity | None,
    ) -> None:
        async with create_db_session() as session:
            stored = await session.get(DiscussionTurnEntity, turn.id)
            stored.action = decision["action"]
            stored.reason = decision["reason"]
            stored.public_message_id = public_message.id if public_message else turn.public_message_id
            stored.response_json = _jsonable(result.get("response") or result)
            stored.tool_steps_json = _jsonable((result.get("response") or {}).get("steps") or [])
            stored.skills_json = _jsonable(result.get("skills")) if result.get("skills") else None
            stored.status = "completed"
            stored.finished_at = _utc_now_naive()
            session.add(stored)

    async def _fail_turn(self, turn: DiscussionTurnEntity, exc: Exception) -> None:
        async with create_db_session() as session:
            stored = await session.get(DiscussionTurnEntity, turn.id)
            if stored:
                stored.action = "pass"
                stored.reason = f"Turn failed: {exc}"
                stored.error = str(exc)
                stored.status = "failed"
                stored.finished_at = _utc_now_naive()
                session.add(stored)

    async def _get_profile(self, agent_id: str) -> AgentProfileEntity:
        async with create_db_session() as session:
            profile = await get_agent_profile(session, agent_id)
        if profile is None or not profile.enabled:
            raise ValueError(f"Agent '{agent_id}' not found.")
        return profile

    async def _get_run(self, run_id: str) -> DiscussionRunEntity:
        async with create_db_session() as session:
            run = await session.get(DiscussionRunEntity, run_id)
        if run is None:
            raise ValueError(f"Discussion run '{run_id}' not found.")
        return run

    async def _list_participants(self, run_id: str) -> list[DiscussionParticipantEntity]:
        async with create_db_session() as session:
            return await self._list_participants_with_session(session, run_id)

    async def _list_participants_with_session(
        self,
        session,
        run_id: str,
    ) -> list[DiscussionParticipantEntity]:
        result = await session.exec(
            select(DiscussionParticipantEntity)
            .where(DiscussionParticipantEntity.run_id == run_id)
            .order_by(DiscussionParticipantEntity.position.asc())
        )
        return list(result.all())

    async def _list_turns_with_session(self, session, run_id: str) -> list[DiscussionTurnEntity]:
        result = await session.exec(
            select(DiscussionTurnEntity)
            .where(DiscussionTurnEntity.run_id == run_id)
            .order_by(
                DiscussionTurnEntity.round_index.asc(),
                DiscussionTurnEntity.turn_index.asc(),
            )
        )
        return list(result.all())

    async def _mark_run_started(self, run_id: str) -> None:
        async with create_db_session() as session:
            run = await session.get(DiscussionRunEntity, run_id)
            run.status = "running"
            run.started_at = run.started_at or _utc_now_naive()
            run.updated_at = _utc_now_naive()
            session.add(run)

    async def _update_round(self, run_id: str, round_index: int) -> None:
        async with create_db_session() as session:
            run = await session.get(DiscussionRunEntity, run_id)
            run.current_round = round_index
            run.updated_at = _utc_now_naive()
            session.add(run)

    async def _mark_run_finished(self, run_id: str) -> dict[str, Any]:
        async with create_db_session() as session:
            run = await session.get(DiscussionRunEntity, run_id)
            messages = await MessageService(session).list_messages(run.public_thread_id)
            last_text = ""
            for message in reversed(messages):
                if message.role == "assistant":
                    last_text = _text_from_content(message.content)
                    break
            run.status = "completed"
            run.final_output = {"content": last_text}
            run.finished_at = _utc_now_naive()
            run.updated_at = _utc_now_naive()
            session.add(run)
        return await self.get_run_payload(run_id)

    def _run_payload(self, run: DiscussionRunEntity) -> dict[str, Any]:
        return {
            "id": run.id,
            "public_thread_id": run.public_thread_id,
            "user_id": run.user_id,
            "topic": run.topic,
            "participant_agent_ids": run.participant_agent_ids,
            "moderator_agent_id": run.moderator_agent_id,
            "status": run.status,
            "max_rounds": run.max_rounds,
            "current_round": run.current_round,
            "final_output": run.final_output,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }

    def _participant_payload(self, item: DiscussionParticipantEntity) -> dict[str, Any]:
        return {
            "id": item.id,
            "run_id": item.run_id,
            "agent_id": item.agent_id,
            "agent_session_id": item.agent_session_id,
            "agent_thread_id": item.agent_thread_id,
            "agent_profile_snapshot": item.agent_profile_snapshot,
            "position": item.position,
            "enabled": item.enabled,
            "created_at": item.created_at.isoformat(),
        }

    def _turn_payload(self, turn: DiscussionTurnEntity) -> dict[str, Any]:
        return {
            "id": turn.id,
            "run_id": turn.run_id,
            "round_index": turn.round_index,
            "turn_index": turn.turn_index,
            "agent_id": turn.agent_id,
            "action": turn.action,
            "reason": turn.reason,
            "public_message_id": turn.public_message_id,
            "private_thread_id": turn.private_thread_id,
            "private_session_id": turn.private_session_id,
            "status": turn.status,
            "response": turn.response_json,
            "steps": turn.tool_steps_json,
            "skills": turn.skills_json,
            "error": turn.error,
            "created_at": turn.created_at.isoformat(),
            "started_at": turn.started_at.isoformat() if turn.started_at else None,
            "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
        }

    def _message_payload(self, message: MessageEntity) -> dict[str, Any]:
        meta = _discussion_meta(message.content)
        return {
            "id": message.id,
            "thread_id": message.thread_id,
            "role": message.role,
            "content": message.content,
            "text": _text_from_content(message.content),
            "discussion": meta or None,
            "created_at": message.created_at.isoformat(),
        }
