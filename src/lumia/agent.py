"""The agent loop.

A manual tool loop rather than the SDK's tool runner, because every tool
call has to pass through the autonomy gate before it executes and the
result of that decision has to be fed back to the model as a normal tool
result. That interception is the whole point of the harness.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .autonomy import ApprovalRequest, AutonomyLevel, classify
from .deadline import Deadline
from .observability import (
    GATE_DECIDED, MODEL_REPLIED, RUN_KILLED, TOOL_EXECUTED, TOOL_GATED,
    TOOL_PROPOSED, TOOL_REFUSED, TURN_STARTED, Killed, RunObserver,
)
from .llm import ClaudeClient, build_client, text_of, tool_uses
from .prompts import system_prompt
from .tools import Toolbox
from .workspace import Workspace

log = logging.getLogger(__name__)

MAX_ITERATIONS = 12
MAX_PAUSE_RESUMES = 3


@dataclass
class ToolCallLog:
    tool: str
    arguments: dict[str, Any]
    level: int
    executed: bool
    reason: str
    result_summary: str


@dataclass
class AgentRun:
    role: str
    task: str
    reply: str = ""
    tool_calls: list[ToolCallLog] = field(default_factory=list)
    approvals_raised: list[str] = field(default_factory=list)
    iterations: int = 0
    stopped_because: str = "end_turn"
    timed_out: bool = False
    killed: bool = False

    def summary(self) -> str:
        executed = sum(1 for c in self.tool_calls if c.executed)
        blocked = len(self.tool_calls) - executed
        parts = [f"{self.iterations} turn(s)", f"{executed} tool call(s)"]
        if blocked:
            parts.append(f"{blocked} awaiting approval")
        return ", ".join(parts)


class Agent:
    """One Lumia specialist. Role selects the prompt addendum and toolset."""

    def __init__(
        self,
        role: str,
        workspace: Workspace,
        toolbox: Toolbox,
        client: ClaudeClient | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self.role = role
        self.ws = workspace
        self.toolbox = toolbox
        self.client = client or build_client(workspace.settings)
        self.allowed_tools = allowed_tools
        self.phase = ""

    def _permitted(self, tool: str) -> bool:
        """Whether this agent, in this phase, may run this tool at all."""
        return self.allowed_tools is None or tool in self.allowed_tools

    @property
    def system(self) -> str:
        return system_prompt(self.role, self.ws.settings)

    def run(
        self,
        task: str,
        max_iterations: int = MAX_ITERATIONS,
        deadline: Deadline | None = None,
        observer: RunObserver | None = None,
        kill_switch: Any = None,
    ) -> AgentRun:
        run = AgentRun(role=self.role, task=task)
        self.observer = observer
        self.kill_switch = kill_switch
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        schemas = self.toolbox.schemas(self.allowed_tools)
        pause_resumes = 0

        # Integrations consult the same budget, so one slow provider cannot
        # spend the whole run's time on retries.
        if deadline is not None:
            self.ws.set_deadline(deadline)

        for iteration in range(1, max_iterations + 1):
            run.iterations = iteration

            stop = self._stop_requested()
            if stop is not None:
                return self._killed(run, stop, "starting another turn")

            if deadline is not None and not deadline.usable:
                return self._out_of_time(run, deadline, "starting another turn")

            if observer is not None:
                observer.emit(TURN_STARTED, turn=iteration,
                              remaining_seconds=deadline.remaining if deadline else None)
                observer.budget_check(deadline)

            response = self.client.create(system=self.system, messages=messages, tools=schemas)

            if observer is not None:
                observer.emit(MODEL_REPLIED, turn=iteration,
                              stop_reason=str(getattr(response, "stop_reason", "")),
                              tool_calls=len(tool_uses(response)))

            # Safety classifiers can decline; check before reading content.
            if getattr(response, "stop_reason", None) == "refusal":
                run.stopped_because = "refusal"
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                run.reply = f"The request was declined by safety classifiers (category: {category})."
                return run

            # Preserve the full content — thinking blocks must be echoed back.
            messages.append({"role": "assistant", "content": response.content})

            if getattr(response, "stop_reason", None) == "pause_turn":
                if pause_resumes >= MAX_PAUSE_RESUMES:
                    run.stopped_because = "pause_turn_limit"
                    run.reply = text_of(response)
                    return run
                pause_resumes += 1
                continue

            calls = tool_uses(response)
            if not calls:
                run.reply = text_of(response)
                run.stopped_because = str(getattr(response, "stop_reason", "end_turn"))
                return run

            # The budget stops new work; it never interrupts a call already in
            # flight. A send cancelled mid-write may still have been delivered,
            # and a message the record shows as unsent but the client received
            # is worse than one that finishes a few seconds late.
            stop = self._stop_requested()
            if stop is not None:
                return self._killed(run, stop, f"running {calls[0].name}")

            if deadline is not None and not deadline.usable:
                return self._out_of_time(run, deadline, f"running {calls[0].name}")

            results = [self._handle_tool_call(call, run) for call in calls]
            messages.append({"role": "user", "content": results})

        run.stopped_because = "iteration_limit"
        run.reply = run.reply or (
            f"Stopped after {max_iterations} turns without finishing. "
            "Narrow the task or raise the iteration limit."
        )
        return run

    def _stop_requested(self) -> dict[str, Any] | None:
        """Whether an operator has asked this run to stop."""
        switch = getattr(self, "kill_switch", None)
        if switch is None or not self.ws.run_ref:
            return None
        return switch.requested(self.ws.run_ref)

    def _killed(self, run: AgentRun, stop: dict[str, Any], before: str) -> AgentRun:
        """Stop because an operator said so, and say what had already happened."""
        done = [c.tool for c in run.tool_calls if c.executed]
        run.stopped_because = "killed"
        run.killed = True
        reason = str(stop.get("reason") or "no reason given")
        run.reply = (
            f"Stopped by an operator before {before} — {reason}. "
            + (f"Already completed: {', '.join(done)}. " if done else "Nothing was sent. ")
            + "Anything not listed did not happen."
        )
        observer = getattr(self, "observer", None)
        if observer is not None:
            observer.emit(RUN_KILLED, reason=reason, scope=stop.get("scope"), before=before)
        log.warning("run %s killed by operator: %s", self.ws.run_ref, reason)
        return run

    def _out_of_time(self, run: AgentRun, deadline: Deadline, before: str) -> AgentRun:
        """Stop cleanly and say exactly what did and did not happen."""
        done = [c.tool for c in run.tool_calls if c.executed]
        held = [c.tool for c in run.tool_calls if not c.executed]
        run.stopped_because = "deadline_exceeded"
        run.timed_out = True
        run.reply = (
            f"Stopped at the {float(deadline.budget_seconds):.0f}-second limit for {deadline.label}, "
            f"before {before}. "
            + (f"Completed: {', '.join(done)}. " if done else "Nothing was sent. ")
            + (f"Queued for approval: {', '.join(held)}. " if held else "")
            + "Anything not listed did not happen."
        )
        log.warning("run hit its %.0fs budget after %s", deadline.budget_seconds, run.summary())
        return run

    # --- tool dispatch with the autonomy gate ---------------------------

    def _handle_tool_call(self, call: Any, run: AgentRun) -> dict[str, Any]:
        name = call.name
        arguments = dict(call.input or {})
        observer = getattr(self, "observer", None)
        if observer is not None:
            observer.emit(TOOL_PROPOSED, tool=name, arguments=json.dumps(arguments, default=str))

        # Enforced here, not merely by leaving the schema out. Omitting a tool
        # from the list sent to the model makes it unlikely to be called;
        # checking at dispatch makes it impossible to run. In a phased run
        # that difference is the whole guarantee — a gathering phase must not
        # be able to send, however the request arrives.
        if not self._permitted(name):
            reason = (
                f"'{name}' is not available in the {self.phase or 'current'} phase. "
                f"Available here: {', '.join(self.allowed_tools or []) or 'none'}."
            )
            run.tool_calls.append(
                ToolCallLog(tool=name, arguments=arguments, level=0, executed=False,
                            reason=reason, result_summary="refused: outside this phase")
            )
            if observer is not None:
                observer.emit(TOOL_REFUSED, tool=name, phase=self.phase, reason=reason)
            log.warning("refused out-of-phase tool %s in phase %s", name, self.phase)
            return _tool_result(call.id, {"error": reason, "not_performed": True}, is_error=True)

        account = self._account_for(arguments)
        draft = self._draft_for(arguments)

        level, reason = classify(name, arguments, account, draft)

        if observer is not None:
            observer.emit(GATE_DECIDED, tool=name, level=int(level), reason=reason)

        if level is AutonomyLevel.APPROVAL_REQUIRED:
            request = self.ws.approvals.submit(
                ApprovalRequest(
                    tool=name,
                    arguments=arguments,
                    reason=reason,
                    agent=self.role,
                    account_id=str(arguments.get("account_id", "")),
                    project_id=str((draft or {}).get("project_id", "") or arguments.get("project_id", "")),
                    run_ref=self.ws.run_ref,
                )
            )
            # The draft stays in the ledger as held, not silently abandoned.
            if draft is not None:
                self.ws.comms.mark_awaiting_approval(str(draft["id"]), request.id)
            run.approvals_raised.append(request.id)
            run.tool_calls.append(
                ToolCallLog(
                    tool=name,
                    arguments=arguments,
                    level=int(level),
                    executed=False,
                    reason=reason,
                    result_summary=f"queued for approval as {request.id}",
                )
            )
            if observer is not None:
                observer.emit(TOOL_GATED, tool=name, approval_id=request.id, reason=reason)

            payload = {
                "status": "awaiting_human_approval",
                "approval_id": request.id,
                "autonomy_level": 3,
                "reason": reason,
                "instruction": (
                    "This action was NOT performed. Do not claim it was. Tell the user it is "
                    "queued for approval, then continue with work you can do autonomously."
                ),
            }
            return _tool_result(call.id, payload)

        result = self.toolbox.call(name, arguments)
        run.tool_calls.append(
            ToolCallLog(
                tool=name,
                arguments=arguments,
                level=int(level),
                executed=True,
                reason=reason,
                result_summary=_summarize(result),
            )
        )
        is_error = isinstance(result, dict) and "error" in result
        if observer is not None:
            observer.emit(TOOL_EXECUTED, tool=name, ok=not is_error, result=_summarize(result))
        return _tool_result(call.id, result, is_error=is_error)

    def _account_for(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        account_id = arguments.get("account_id")
        if not account_id:
            return None
        return self.ws.crm.get_account(str(account_id))

    def _draft_for(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """The stored message a send is about to transmit.

        Read from the ledger rather than from the call's arguments: the
        gate screens what will actually go out, not what the call claims
        it contains.
        """
        draft_id = arguments.get("draft_id")
        if not draft_id:
            return None
        return self.ws.comms.get_communication(str(draft_id))


def _tool_result(tool_use_id: str, payload: Any, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(payload, default=str, indent=2)[:60_000],
    }
    if is_error:
        block["is_error"] = True
    return block


def _summarize(result: Any) -> str:
    if isinstance(result, dict):
        if "error" in result:
            return f"error: {result['error']}"
        for key in ("id", "status", "count", "total_accounts"):
            if key in result:
                return f"{key}={result[key]}"
        return f"{len(result)} field(s)"
    return str(result)[:120]
