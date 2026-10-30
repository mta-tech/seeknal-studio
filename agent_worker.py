#!/usr/bin/env python3
"""Authenticated, draft-only Seeknal agent worker for Studio.

The worker speaks newline-delimited JSON on stdin/stdout.  It deliberately
exposes only project reads and writes beneath ``.seeknal/drafts``.  Applying a
draft and running a pipeline remain separate Studio actions.  Seeknal's
``ask_user`` confirmations pause the turn until Studio sends the user's answer.
"""

from __future__ import annotations

import hashlib
import hmac
import asyncio
import json
import os
from pathlib import Path
import queue
import re
import tempfile
import sys
import threading
import urllib.error
import urllib.request
from typing import Any
from importlib import metadata
import uuid


DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "deepseek-v4.1-flash:netra"
DEFAULT_BASE_URL = "https://ai.sumopod.com/v1"
EXPECTED_SDK_VERSION = "2.14.0"
SAFE_TOOL_NAMES = (
    "read_pipeline",
    "search_pipelines",
    "search_project_files",
    "draft_node",
    "write_draft",
    "ask_user",
)
ASK_USER_TIMEOUT_SECONDS = 900
_DRAFT_NAME = re.compile(r"^draft_[A-Za-z_]+_[A-Za-z0-9_]+\.(?:yml|yaml|py)$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token)(\s*[:=]\s*|\s+)[^\s,;]+"
)


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _redact(value: object) -> str:
    text = str(value)
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return text[-4000:]


def project_fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    paths = [project / "seeknal_project.yml"]
    paths.extend((project / "seeknal").glob("**/*"))
    paths.extend((project / "data").glob("**/*"))
    for path in sorted(path for path in paths if path.is_file() and not path.is_symlink()):
        digest.update(path.relative_to(project).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _safe_drafts_root(project_path: Path) -> Path:
    project = project_path.resolve()
    internal = project / ".seeknal"
    if internal.is_symlink():
        raise ValueError(".seeknal must not be a symlink")
    internal.mkdir(exist_ok=True)
    drafts_path = internal / "drafts"
    if drafts_path.is_symlink():
        raise ValueError(".seeknal/drafts must not be a symlink")
    drafts_path.mkdir(exist_ok=True)
    drafts = drafts_path.resolve()
    if project not in drafts.parents:
        raise ValueError("draft directory escapes the selected project")
    return drafts


async def draft_node(
    node_type: str,
    name: str,
    python: bool = False,
    deps: list[str] | None = None,
) -> str:
    """Create a Seeknal template only inside the verified Studio draft directory."""
    from seeknal.ask.agents.tools._context import get_tool_context
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    import seeknal.workflow.draft as seeknal_draft
    from seeknal.workflow.draft import (
        NODE_TYPES, TEMPLATE_FILES, PYTHON_TEMPLATE_FILES,
        generate_filename, render_template,
    )

    context = get_tool_context()
    try:
        drafts = _safe_drafts_root(context.project_path)
    except ValueError as error:
        return f"Draft path error: {error}"
    if node_type not in NODE_TYPES:
        return f"Invalid node type '{node_type}'."
    if not name or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", name):
        return "Invalid name. Use alphanumeric characters and underscores only."
    normalized = NODE_TYPES[node_type]
    template_map = PYTHON_TEMPLATE_FILES if python else TEMPLATE_FILES
    if normalized not in template_map:
        return f"No {'Python' if python else 'YAML'} template is available for {node_type}."
    filename = generate_filename(normalized, name, python=python)
    target = drafts / filename
    if target.exists() or target.is_symlink():
        return f"Draft file already exists: {filename}."
    try:
        content = render_template(
            Environment(
                loader=FileSystemLoader(str(Path(str(seeknal_draft.__file__)).resolve().parent / "templates")),
                autoescape=select_autoescape(), trim_blocks=True, lstrip_blocks=True,
            ),
            normalized, name,
            description=None, python=python, deps=deps,
        )
        with context.fs_lock:
            _atomic_draft_write(drafts, filename, content, replace=False)
        context.artifact_discovery.refresh()
        return f"Created draft for Studio review: .seeknal/drafts/{filename}\n\n{content}"
    except Exception as error:
        return f"Failed to create draft: {_redact(error)}"


def _atomic_draft_write(drafts: Path, file_name: str, content: str, *, replace: bool) -> None:
    target = drafts / file_name
    if target.parent != drafts or target.is_symlink():
        raise ValueError("draft target is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".studio-agent-", dir=drafts)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if target.exists() and not target.is_file():
                raise ValueError("draft target is not a regular file")
            os.replace(temporary, target)
        else:
            os.link(temporary, target, follow_symlinks=False)
            temporary.unlink()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_draft(file_name: str, content: str) -> str:
    """Write complete YAML/Python content to an existing or new draft file."""
    from seeknal.ask.agents.tools._context import get_tool_context

    if not isinstance(file_name, str) or not _DRAFT_NAME.fullmatch(Path(file_name).name):
        return "Invalid draft filename; use draft_<type>_<name>.yml|.yaml|.py"
    if Path(file_name).name != file_name or not isinstance(content, str):
        return "Draft writes must use a filename only and text content."
    if len(content.encode()) > 300_000:
        return "Draft content exceeds the 300 KB Studio limit."
    context = get_tool_context()
    try:
        drafts = _safe_drafts_root(context.project_path)
    except ValueError as error:
        return f"Draft path error: {error}"
    target = drafts / file_name
    if target.parent != drafts or target.is_symlink():
        return "Draft path is unsafe."
    try:
        with context.fs_lock:
            _atomic_draft_write(drafts, file_name, content, replace=True)
        context.artifact_discovery.refresh()
    except (OSError, ValueError) as error:
        return f"Draft write failed: {_redact(error)}"
    return f"Saved draft for Studio review: .seeknal/drafts/{file_name}"


def _read_api_key(path_value: str) -> str:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("configured API key file is missing or unsafe")
    if path.stat().st_mode & 0o077:
        raise ValueError("API key file permissions must be owner-only (chmod 600)")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("configured API key file is empty")
    return key


def _request_json(url: str, key: str, *, payload: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"provider HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError("provider is unreachable") from error
    if not isinstance(result, dict):
        raise RuntimeError("provider returned an invalid JSON response")
    return result


def verify_provider(config: dict, key: str) -> None:
    if config.get("provider") != DEFAULT_PROVIDER:
        raise ValueError("pilot requires the OpenAI-compatible provider")
    if config.get("model") != DEFAULT_MODEL:
        raise ValueError(f"pilot requires exact model {DEFAULT_MODEL}")
    if config.get("base_url", "").rstrip("/") != DEFAULT_BASE_URL:
        raise ValueError("pilot requires the verified Sumopod endpoint")
    models = _request_json(f"{DEFAULT_BASE_URL}/models", key)
    ids = {
        item.get("id")
        for item in models.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if DEFAULT_MODEL not in ids:
        raise RuntimeError(f"exact model {DEFAULT_MODEL} is unavailable")
    probe = _request_json(
        f"{DEFAULT_BASE_URL}/chat/completions",
        key,
        payload={
            "model": DEFAULT_MODEL,
            "messages": [{"role": "user", "content": "Call studio_probe exactly once."}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "studio_probe",
                    "description": "Verify function calling for Seeknal Studio.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "studio_probe"}},
            "max_tokens": 32,
        },
    )
    choices = probe.get("choices") or []
    calls = choices[0].get("message", {}).get("tool_calls", []) if choices else []
    if not calls or calls[0].get("function", {}).get("name") != "studio_probe":
        raise RuntimeError("exact model did not return the required tool call")


def _prepare_environment(config: dict, key: str) -> None:
    os.environ["OPENAI_API_KEY"] = key
    os.environ["OPENAI_BASE_URL"] = DEFAULT_BASE_URL
    os.environ["SEEKNAL_ASK_OPENAI_BASE_URL"] = DEFAULT_BASE_URL
    os.environ["SEEKNAL_ASK_LLM_PROVIDER"] = "openai"
    os.environ["SEEKNAL_ASK_MODEL"] = DEFAULT_MODEL
    for blocked in (
        "ATLAS_API_URL", "ATLAS_PORTAL_URL", "FIRECRAWL_API_KEY",
        "IBA_ENGINE_API_KEY", "IBA_STORAGE_API_KEY",
    ):
        os.environ.pop(blocked, None)


def _sdk_identity(config: dict) -> dict[str, str]:
    import seeknal

    dist_version = metadata.version("seeknal")
    if seeknal.__file__ is None:
        raise RuntimeError("Seeknal SDK module has no file identity")
    module_file = Path(seeknal.__file__).resolve()
    expected = str(config.get("expected_seeknal_version", EXPECTED_SDK_VERSION))
    if expected != EXPECTED_SDK_VERSION or dist_version != EXPECTED_SDK_VERSION:
        raise RuntimeError(
            f"unsupported Seeknal SDK: expected {EXPECTED_SDK_VERSION}, got {dist_version}"
        )
    configured_root = Path(str(config.get("sdk_root", module_file.parent))).resolve()
    if configured_root != module_file.parent:
        raise RuntimeError("configured Seeknal SDK root does not match the imported package")
    return {
        "sdk_version": dist_version,
        "sdk_module_file": str(module_file),
        "expected_sdk_version": expected,
    }


_ANSWER_LOCK = threading.Lock()
# question_id -> (event loop, future) for questions the user has not answered.
_ANSWER_WAITERS: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
_INPUT_CLOSED = threading.Event()
_ACTIVE_REQUEST: str | None = None


def _ask_user_options(options: object) -> tuple[list[dict], bool, str | None]:
    cleaned: list[dict] = []
    multi_select = False
    if isinstance(options, list) and len(options) > 6:
        return [], False, "Offer at most 6 options; ask again with fewer."
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        label = str(option.get("label", "")).strip()
        if not label:
            continue
        if len(label) > 120:
            # The answer must echo a label exactly, so never shorten one.
            return [], False, "Option labels must be 120 characters or fewer; ask again with shorter labels."
        multi_select = multi_select or str(option.get("multi_select", "")).lower() == "true"
        cleaned.append({
            "label": label,
            "description": str(option.get("description", "")).strip()[:500],
            "recommended": str(option.get("recommended", "")).lower() == "true",
        })
    return cleaned, multi_select, None


async def ask_user(question: str, options: list[dict[str, Any]]) -> str:
    # Studio replaces Seeknal's terminal menu: the question goes to the agent
    # panel and the turn waits for the answer the user picks or types there.
    # The docstring is copied from Seeknal's own ask_user tool at registration.
    cleaned, multi_select, problem = _ask_user_options(options)
    if problem:
        return problem
    if not cleaned:
        return "No options provided."
    question_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    answer: asyncio.Future = loop.create_future()
    with _ANSWER_LOCK:
        if _INPUT_CLOSED.is_set():
            return "Studio closed the session before the user could answer."
        _ANSWER_WAITERS[question_id] = (loop, answer)
    try:
        _emit({
            "type": "event", "request_id": _ACTIVE_REQUEST,
            "event": {"type": "ask_user", "data": {
                "question_id": question_id, "question": str(question).strip()[:2000],
                "options": cleaned, "multi_select": multi_select,
            }},
        })
        try:
            result = await asyncio.wait_for(answer, ASK_USER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            _emit({
                "type": "event", "request_id": _ACTIVE_REQUEST,
                "event": {"type": "ask_user_timeout", "data": {"question_id": question_id}},
            })
            return (f"The user did not answer within {ASK_USER_TIMEOUT_SECONDS // 60} minutes. "
                    "Do not assume a choice; stop and say which decision is still needed.")
        if result is None:
            raise RuntimeError("Studio closed the session before the user answered")
        return result
    finally:
        with _ANSWER_LOCK:
            _ANSWER_WAITERS.pop(question_id, None)


def _resolve(future: asyncio.Future, value: str | None) -> None:
    if not future.done():
        future.set_result(value)


def _deliver_answer(request: dict) -> None:
    question_id = request.get("question_id")
    answer = request.get("answer")
    if not isinstance(question_id, str) or not isinstance(answer, str) or not 0 < len(answer) <= 2000:
        return
    with _ANSWER_LOCK:
        waiter = _ANSWER_WAITERS.get(question_id)
    if waiter is not None:
        loop, future = waiter
        try:
            loop.call_soon_threadsafe(_resolve, future, answer)
        except RuntimeError:
            pass  # the turn ended and closed its loop; the answer is moot


def _read_requests(requests: queue.Queue) -> None:
    # Answers must reach a turn that is blocked inside ask_user, so stdin is
    # read on its own thread; every other request is handled in order.
    try:
        for line in sys.stdin:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("operation") == "answer":
                _deliver_answer(parsed)
            else:
                requests.put(line)
    finally:
        # Studio went away: release any question still waiting, then stop.
        with _ANSWER_LOCK:
            _INPUT_CLOSED.set()
            waiters = list(_ANSWER_WAITERS.values())
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(_resolve, future, None)
            except RuntimeError:
                pass
        requests.put(None)


def _safe_toolset():
    from pydantic_deep import DeepAgentDeps
    from pydantic_ai import Tool
    from pydantic_ai.toolsets import FunctionToolset
    from seeknal.ask.agents.tools.ask_user_tool import ask_user as seeknal_ask_user
    from seeknal.ask.agents.tools.read_pipeline import read_pipeline
    from seeknal.ask.agents.tools.search_pipelines import search_pipelines
    from seeknal.ask.agents.tools.search_project_files import search_project_files

    ask_user.__doc__ = seeknal_ask_user.__doc__
    return FunctionToolset[DeepAgentDeps](
        # One question at a time: the panel and runtime track a single pending question.
        tools=[read_pipeline, search_pipelines, search_project_files, draft_node, write_draft,
               Tool(ask_user, sequential=True)],
        id="seeknal-studio-draft-only",
    )


def verify_effective_tools(project: Path, config: dict, key: str) -> list[str]:
    """Prove the actual model request exposes exactly the Studio allowlist."""
    from pydantic_ai.models.test import TestModel
    from seeknal.ask.agents.agent import create_agent

    _prepare_environment(config, key)
    agent, deps, _history, _cost = create_agent(
        project,
        provider="openai",
        model=DEFAULT_MODEL,
        api_key=key,
        include_web=False,
        environment="gateway",
    )
    model = TestModel(call_tools=[], custom_output_text="tool surface verified")
    with agent.override(model=model, toolsets=[_safe_toolset()]):
        agent.run_sync("Return a short readiness acknowledgement.", deps=deps)
    parameters = model.last_model_request_parameters
    if parameters is None:
        raise RuntimeError("effective agent tool request was not captured")
    names = sorted(tool.name for tool in parameters.function_tools)
    native_tools = (
        parameters.native_tools
        if hasattr(parameters, "native_tools")
        else parameters.builtin_tools
    )
    if names != sorted(SAFE_TOOL_NAMES) or native_tools:
        raise RuntimeError("effective agent tool surface does not match the Studio allowlist")
    return names


async def _run_turn(
    project: Path, config: dict, key: str, question: str, history: list, request_id: str,
) -> dict:
    _prepare_environment(config, key)
    from seeknal.ask.agents.agent import create_agent
    from pydantic_ai.usage import UsageLimits
    from pydantic_ai import AgentRunResultEvent
    from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent

    agent, deps, _unused_history, _cost = create_agent(
        project,
        provider="openai",
        model=DEFAULT_MODEL,
        api_key=key,
        include_web=False,
        environment="gateway",
    )
    safety = """
You are operating inside Seeknal Studio's draft-only pilot.
You may inspect pipeline definitions and create or update files only under
.seeknal/drafts using draft_node and write_draft. Do not apply drafts, edit
active pipeline files, execute code or SQL, run pipelines, publish, browse the
web, or claim validation/execution occurred. Finish by naming every draft path
created and state that Studio review, validation, apply, and run are pending.
Studio overrides the earlier gateway note: ask_user IS available here. When a
decision only the user can make is needed, call ask_user (one question at a
time); Studio shows the options in its agent panel and returns the user's
answer. Set multi_select="true" on the options when several may be chosen; the
answer is then the chosen labels joined by ", ".
"""
    sdk_root = Path(str(config.get("sdk_root") or config.get("signal_root", ""))).resolve()
    skill_path = (
        sdk_root / "ask" / "builtin_skills" / "build-pipeline-node" / "SKILL.md"
    ).resolve()
    if skill_path.is_file() and not skill_path.is_symlink():
        safety += (
            "\n\n## Installed Seeknal build-pipeline-node skill\n"
            + skill_path.read_text(encoding="utf-8")[:30_000]
            + "\n\nStudio override: stop after creating the complete draft. "
              "Validation, apply, run, and output inspection are separate user-approved Studio actions."
        )
    global _ACTIVE_REQUEST
    _ACTIVE_REQUEST = request_id
    result: Any = None
    with agent.override(toolsets=[_safe_toolset()]):
        async with agent.run_stream_events(
            question,
            deps=deps,
            message_history=history,
            instructions=safety,
            capabilities=[],
            usage_limits=UsageLimits(request_limit=24, tool_calls_limit=16),
        ) as stream:
            async for event in stream:
                if isinstance(event, FunctionToolCallEvent):
                    name = getattr(event.part, "tool_name", "unknown")
                    if name not in SAFE_TOOL_NAMES:
                        raise RuntimeError(f"agent attempted unavailable tool {name}")
                    _emit({
                        "type": "event", "request_id": request_id,
                        "event": {"type": "tool_call", "data": {"name": name}},
                    })
                elif isinstance(event, FunctionToolResultEvent):
                    name = getattr(event.result, "tool_name", "unknown")
                    content = _redact(getattr(event.result, "content", ""))
                    _emit({
                        "type": "event", "request_id": request_id,
                        "event": {"type": "tool_result", "data": {"name": name, "summary": content[:1200]}},
                    })
                elif isinstance(event, AgentRunResultEvent):
                    result = event.result
    if result is None:
        raise RuntimeError("agent completed without a result")
    history[:] = result.all_messages()
    return {"answer": result.output or ""}


def main() -> int:
    first = sys.stdin.readline()
    if not first:
        return 2
    try:
        init = json.loads(first)
        project = Path(init["project_path"]).resolve()
        if not project.is_dir() or not (project / "seeknal_project.yml").is_file():
            raise ValueError("selected project is not a Seeknal project")
        nonce = init["nonce"]
        token = init["token"]
        config = init["config"]
        sdk_identity = _sdk_identity(config)
        key = _read_api_key(config["api_key_file"])
        verify_provider(config, key)
        effective_tools = verify_effective_tools(project, config, key)
        fingerprint = project_fingerprint(project)
        proof_body = f"{nonce}\n{project}\n{fingerprint}".encode()
        proof = hmac.new(token.encode(), proof_body, hashlib.sha256).hexdigest()
        _emit({
            "type": "hello",
            "nonce": nonce,
            "project_path": str(project),
            "project_fingerprint": fingerprint,
            "proof": proof,
            "provider": DEFAULT_PROVIDER,
            "model": DEFAULT_MODEL,
            "model_verified": True,
            "tool_calling_verified": True,
            "capabilities": {"draft_only": True, "tools": effective_tools},
            **sdk_identity,
        })
    except Exception as error:
        _emit({"type": "fatal", "error": _redact(error)})
        return 1

    histories: dict[str, list] = {}
    requests: queue.Queue = queue.Queue()
    threading.Thread(target=_read_requests, args=(requests,), daemon=True).start()
    while (line := requests.get()) is not None:
        request: dict[str, Any] | None = None
        try:
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                raise ValueError("agent request must be an object")
            request = parsed
            request_id = request["request_id"]
            operation = request["operation"]
            if not isinstance(request_id, str) or not isinstance(operation, str):
                raise ValueError("invalid agent request envelope")
            if operation == "ask":
                session_id = request.get("session_id", "")
                question = request.get("question", "")
                if (
                    not isinstance(session_id, str)
                    or not _SESSION_ID.fullmatch(session_id)
                    or not isinstance(question, str)
                    or not question.strip()
                ):
                    raise ValueError("invalid agent turn")
                result = asyncio.run(_run_turn(
                    project, config, key, question.strip(), histories.setdefault(session_id, []), request_id,
                ))
                _emit({"type": "response", "request_id": request_id, "ok": True, "result": result})
            elif operation == "ping":
                _emit({"type": "response", "request_id": request_id, "ok": True, "result": {"pong": True}})
            else:
                raise ValueError("unsupported operation")
        except Exception as error:
            _emit({
                "type": "response",
                "request_id": request.get("request_id") if request is not None else None,
                "ok": False,
                "error": _redact(error),
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
