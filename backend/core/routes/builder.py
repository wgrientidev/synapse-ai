"""
Builder agent endpoint: POST /api/builder/chat

A meta-agent that lets users design and create agents/orchestrations
through natural-language conversation.  Streams SSE events exactly
like the regular chat endpoint so the frontend can use the same
SSE-handling code, and exposes an additional set of builder-specific
events (orchestration_saved, agent_saved) that the BuilderPanel uses.
"""
import json
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.builder_tools import (
    BUILDER_TOOL_SCHEMAS,
    BUILDER_SYSTEM_PROMPT,
    execute_builder_tool,
)
from core.config import load_settings
from core.llm_providers import generate_response as llm_generate_response, detect_mode_from_model
from core.react_engine import parse_tool_call

router = APIRouter()

MAX_BUILDER_TURNS = 15


class BuilderChatRequest(BaseModel):
    message: str
    history: list[dict] = []          # [{role, content}, ...]
    selected_agent_ids: list[str] = []
    can_create_agents: bool = False
    model: str | None = None          # model override
    current_orchestration_id: str | None = None


# ─── Core streaming generator ─────────────────────────────────────────────────

async def run_builder_stream(request: BuilderChatRequest, server_module):
    """
    Async generator that runs the builder ReAct loop and yields dicts.

    Implements the same current_context_text accumulation pattern as
    run_agent_step() in react_engine.py so all providers behave correctly:
      - Gemini/OpenAI/Anthropic (cloud): tools passed via API (native function calling)
      - CLI providers (cli.*): tools injected into flat prompt by _build_cli_prompt()
      - Bedrock: system prompt guides the model; no API-level tool calling
      - Ollama (local): tools passed via /api/chat tools param

    Event types emitted:
      thinking            — planning/status messages
      chunk               — streamed text tokens (simulated word-by-word)
      tool_call           — tool invocation about to happen
      tool_result         — result of the tool call
      orchestration_saved — an orchestration was created or updated
      agent_saved         — an agent was created or updated
      final               — the final text response
      error               — unrecoverable error
    """
    settings = load_settings()

    # ── Resolve model + mode ───────────────────────────────────────────────────
    model = request.model or settings.get("model", "mistral")
    mode = detect_mode_from_model(model)

    # Build system prompt with context about selected agents / current orch
    sys_prompt = BUILDER_SYSTEM_PROMPT

    if request.selected_agent_ids:
        sys_prompt += (
            f"\n\nContext: The user has pre-selected the following agent IDs for inclusion: "
            f"{', '.join(request.selected_agent_ids)}. "
            "Start by treating these as candidates for the orchestration steps."
        )
    if request.can_create_agents:
        sys_prompt += (
            "\n\nThe user has granted you permission to create new agents if needed."
        )
    if request.current_orchestration_id:
        sys_prompt += (
            f"\n\nThe user is currently viewing orchestration ID: {request.current_orchestration_id}. "
            "When they ask for changes 'to this', use update_orchestration with that ID."
        )

    # ── ReAct loop state (mirrors run_agent_step in react_engine.py) ──────────
    user_message = request.message
    # Accumulated context: tool thoughts + results are appended here each turn.
    # Turn 0 sends user_message directly (with history for conversational context).
    # Turn 1+ sends current_context_text as the prompt so the model sees its own
    # reasoning chain, exactly as run_agent_step does.
    current_context_text = f"User Request: {user_message}\n"
    recent_history_messages: list[dict] = list(request.history)
    tool_repetition_counts: dict[str, int] = {}
    final_response = ""

    yield {"type": "thinking", "message": "Thinking…"}

    for turn in range(MAX_BUILDER_TURNS):
        print(f"\n{'#'*50}\n### BUILDER TURN {turn + 1}/{MAX_BUILDER_TURNS} ###\n{'#'*50}")

        # Turn 0: send the raw user message + conversation history (same as
        # react_engine turn 0 so the model has prior-chat context).
        # Turn 1+: send the accumulated context text without history so the
        # model only sees its own reasoning chain for this task.
        if turn == 0:
            active_prompt = user_message
            active_history = recent_history_messages
        else:
            active_prompt = current_context_text
            active_history = []

        yield {"type": "thinking", "message": f"Thinking… (step {turn + 1}/{MAX_BUILDER_TURNS})"}

        # ── Call LLM ──────────────────────────────────────────────────────────
        print(f"DEBUG BUILDER: 🔄 Calling LLM (mode={mode}, model={model})…", flush=True)
        try:
            response_text = await llm_generate_response(
                prompt_msg=active_prompt,
                sys_prompt=sys_prompt,
                mode=mode,
                current_model=model,
                current_settings=settings,
                tools=BUILDER_TOOL_SCHEMAS,   # provider conversion handles format
                history_messages=active_history,
                source="builder",
            )
        except Exception as exc:
            yield {"type": "error", "message": str(exc)}
            return

        print(f"DEBUG BUILDER: 🤖 Response: {response_text[:300]}{'…' if len(response_text) > 300 else ''}")

        # ── Simulate streaming: emit word-by-word chunks ──────────────────────
        words = response_text.split(" ")
        for i, word in enumerate(words):
            yield {"type": "chunk", "content": word if i == len(words) - 1 else word + " "}
            await asyncio.sleep(0)

        # ── Parse tool call ───────────────────────────────────────────────────
        tool_call, json_error = parse_tool_call(response_text)

        if json_error:
            # LLM emitted something that looks like JSON but is malformed.
            # Inject an error into the context and let the model retry.
            current_context_text += f"\nSystem: JSON parse error: {json_error}. Please retry with valid JSON.\n"
            continue

        if tool_call is None:
            # No tool call → this is the final conversational answer.
            final_response = response_text
            break

        # ── Tool call detected ────────────────────────────────────────────────
        # Persist the LLM's reasoning into context so it sees its own chain-of-
        # thought on the next turn (mirrors react_engine behaviour).
        if response_text.strip():
            current_context_text += f"\nAssistant Thought: {response_text}\n"

        tool_name = tool_call.get("tool") or tool_call.get("name", "")
        args = tool_call.get("arguments") or tool_call.get("args") or {}

        print(f"DEBUG BUILDER: 🔧 Tool call: {tool_name} args={json.dumps(args, default=str)[:300]}")

        # ── Repetition guard (mirrors sequentialthinking cap in react_engine) ─
        tool_repetition_counts[tool_name] = tool_repetition_counts.get(tool_name, 0) + 1
        if tool_repetition_counts[tool_name] > 4:
            block_msg = (
                f"Tool '{tool_name}' has already been called "
                f"{tool_repetition_counts[tool_name]} times this session. "
                "Try a different tool or provide a final answer."
            )
            print(f"DEBUG BUILDER: 🔁 Repetition cap hit for '{tool_name}' — blocked", flush=True)
            current_context_text += f"\nSystem: {block_msg}\n"
            yield {"type": "tool_result", "tool_name": tool_name, "result": block_msg}
            continue

        yield {"type": "tool_call", "tool_name": tool_name, "args": args}

        # ── Execute tool ──────────────────────────────────────────────────────
        try:
            result_str = await execute_builder_tool(tool_name, args, server_module)
        except Exception as exc:
            result_str = json.dumps({"error": str(exc)})

        print(f"DEBUG BUILDER: 📤 Tool result ({tool_name}): {result_str[:300]}")
        yield {"type": "tool_result", "tool_name": tool_name, "result": result_str}

        # ── Domain-specific frontend events ───────────────────────────────────
        try:
            result_obj = json.loads(result_str)
            if tool_name in ("create_orchestration", "update_orchestration"):
                orch = result_obj.get("orchestration") or result_obj
                if "id" in orch:
                    yield {"type": "orchestration_saved", "orchestration": orch}
            elif tool_name in ("create_agent", "update_agent"):
                agent = result_obj.get("agent") or result_obj
                if "id" in agent:
                    yield {"type": "agent_saved", "agent": agent}
        except Exception:
            pass  # result was an error string — don't crash

        # ── Accumulate tool result into context ───────────────────────────────
        # This is the key difference from the old history-appending approach:
        # the context grows linearly, the model always sees the full reasoning
        # chain, and we never pass stale history alongside accumulated context.
        current_context_text += f"\nTool '{tool_name}' Output: {result_str}\n"

    if not final_response:
        final_response = "I've reached the maximum number of reasoning steps. Please rephrase your request or break it into smaller pieces."

    yield {"type": "final", "response": final_response}


# ─── Compat wrapper (used by chat.py for main-chat routing) ───────────────────

async def run_builder_stream_compat(request, server_module):
    """
    Wraps run_builder_stream and converts events to the standard
    data: {...}\\n\\n SSE format used by the main chat endpoint so
    the frontend page.tsx processMessageSSE() needs zero changes.

    Builder-specific events are translated:
      orchestration_saved → tool_result preview + woven into final response
      agent_saved         → tool_result preview
      tool_call           → tool_execution (same shape as react_engine)
      chunk               → chunk (passthrough)
    """
    # Build a minimal BuilderChatRequest from the ChatRequest
    builder_req = BuilderChatRequest(
        message=request.message,
        history=getattr(request, "history_messages", []) or [],
        can_create_agents=True,
        model=getattr(request, "model", None),
    )

    async for event in run_builder_stream(builder_req, server_module):
        etype = event["type"]

        if etype == "thinking":
            yield f"data: {json.dumps({'type': 'status', 'message': event['message']})}\n\n"

        elif etype == "chunk":
            yield f"data: {json.dumps({'type': 'chunk', 'content': event['content']})}\n\n"

        elif etype == "tool_call":
            yield f"data: {json.dumps({'type': 'tool_execution', 'tool_name': event['tool_name'], 'args': event['args']})}\n\n"

        elif etype == "tool_result":
            try:
                result_obj = json.loads(event["result"])
                preview = json.dumps(result_obj)[:200]
            except Exception:
                preview = str(event["result"])[:200]
            yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': event['tool_name'], 'preview': preview})}\n\n"

        elif etype == "orchestration_saved":
            orch = event["orchestration"]
            preview = f"✓ Orchestration '{orch.get('name', orch.get('id', ''))}' saved"
            yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': 'orchestration_saved', 'preview': preview})}\n\n"

        elif etype == "agent_saved":
            agent = event["agent"]
            preview = f"✓ Agent '{agent.get('name', agent.get('id', ''))}' saved"
            yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': 'agent_saved', 'preview': preview})}\n\n"

        elif etype == "final":
            yield f"data: {json.dumps({'type': 'response', 'content': event['response'], 'intent': 'chat', 'data': None, 'tool_name': None}, default=str)}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        elif etype == "error":
            yield f"data: {json.dumps({'type': 'error', 'message': event['message']})}\n\n"

        await asyncio.sleep(0)


# ─── FastAPI route ─────────────────────────────────────────────────────────────

@router.post("/api/builder/chat")
async def builder_chat(request: BuilderChatRequest, http_request: Request):
    """
    SSE endpoint for the AI Builder panel.

    Streams builder events as Server-Sent Events.  The BuilderPanel
    frontend component reads these directly; the main chat is routed
    here transparently via run_builder_stream_compat().
    """
    server_module = http_request.app.state.server_module

    async def event_generator():
        try:
            async for event in run_builder_stream(request, server_module):
                yield f"data: {json.dumps(event, default=str)}\n\n"
                await asyncio.sleep(0)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
