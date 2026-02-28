from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TOOL_LABELS: dict[str, tuple[str, str]] = {
    "Read": ("\U0001f4d6", "Lecture de"),
    "Edit": ("\u270f\ufe0f", "Modification de"),
    "Write": ("\U0001f4dd", "Ecriture de"),
    "Bash": ("\U0001f4bb", "Execution"),
    "Glob": ("\U0001f50d", "Recherche fichiers"),
    "Grep": ("\U0001f50d", "Recherche dans le code"),
    "Agent": ("\U0001f916", "Sous-agent"),
    "WebSearch": ("\U0001f310", "Recherche web"),
    "WebFetch": ("\U0001f310", "Lecture web"),
    "Skill": ("\u2699\ufe0f", "Skill"),
    "TaskCreate": ("\U0001f4cb", "Creation tache"),
    "TaskUpdate": ("\U0001f4cb", "Mise a jour tache"),
}


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """Extract a short detail string from tool input."""
    if tool_name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path", "")
        if path:
            parts = Path(path).parts
            return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1] if parts else path
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    elif tool_name in ("Grep", "Glob"):
        return tool_input.get("pattern", "")
    elif tool_name == "Agent":
        return tool_input.get("description", tool_input.get("prompt", "")[:60])
    elif tool_name == "Skill":
        return tool_input.get("skill", "")
    elif tool_name in ("WebSearch", "WebFetch"):
        return tool_input.get("query", tool_input.get("url", ""))[:60]
    return ""


@dataclass
class FeedbackItem:
    """A single piece of feedback from an agent during a mission."""
    timestamp: str
    kind: str       # "tool", "text", "turn", "system"
    summary: str    # Ex: "📖 Lecture de src/config.py"


@dataclass
class MissionResult:
    """Tracks the lifecycle of a background agent mission."""
    status: str = "running"  # running, completed, failed
    output: str | None = None
    started_at: str = ""
    finished_at: str | None = None
    feedback: list[FeedbackItem] = field(default_factory=list)
    turn_count: int = 0


class AgentLauncher:
    def __init__(
        self,
        default_command: str,
        default_args: list[str],
        allowed_paths: list[str],
        max_duration: int,
    ):
        self.default_command = default_command
        self.default_args = default_args
        self.allowed_paths = [Path(p).resolve() for p in allowed_paths]
        self.max_duration = max_duration
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._results: dict[str, MissionResult] = {}

    def build_prompt(
        self,
        mission: str,
        context_messages: list[dict],
        mission_id: str,
    ) -> str:
        parts = [f"You are in mission {mission_id}.\n"]

        if context_messages:
            parts.append("Recent conversation context:")
            for msg in context_messages[-20:]:
                sender = msg.get("from", "unknown")
                text = msg.get("message", "")
                parts.append(f"  {sender}: {text}")
            parts.append("")

        parts.append(f"Current task:\n{mission}")
        parts.append(
            f"\nUse intercom_history('{mission_id}') if you need the full conversation history."
        )
        return "\n".join(parts)

    def validate_path(self, path: str) -> bool:
        if not self.allowed_paths:
            return True
        resolved = Path(path).resolve()
        return any(resolved == ap or ap in resolved.parents for ap in self.allowed_paths)

    async def launch(
        self,
        mission: str,
        context_messages: list[dict],
        mission_id: str,
        project_path: str,
        agent_command: str | None = None,
    ) -> str:
        if not self.validate_path(project_path):
            return f"Error: path {project_path} is not in allowed_paths"

        prompt = self.build_prompt(mission, context_messages, mission_id)
        command = agent_command or self.default_command
        args = self.default_args.copy()

        try:
            proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                prompt,
                cwd=project_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active[mission_id] = proc

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.max_duration,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: agent timed out after {self.max_duration}s"
            finally:
                self._active.pop(mission_id, None)

            if proc.returncode != 0:
                error = stderr.decode().strip()
                return f"Error (exit {proc.returncode}): {error[:500]}"

            return stdout.decode().strip()

        except FileNotFoundError:
            return f"Error: command not found: {command}"

    async def launch_streaming(
        self,
        mission: str,
        context_messages: list[dict],
        mission_id: str,
        project_path: str,
        agent_command: str | None = None,
    ) -> str:
        """Launch agent with stream-json output, collecting feedback in real-time."""
        if not self.validate_path(project_path):
            return f"Error: path {project_path} is not in allowed_paths"

        prompt = self.build_prompt(mission, context_messages, mission_id)
        command = agent_command or self.default_command
        args = []
        for arg in self.default_args:
            if arg == "json" and len(args) > 0 and args[-1] == "--output-format":
                args.append("stream-json")
            else:
                args.append(arg)
        # Ensure stream-json is set if not already
        if "--output-format" not in args:
            args.extend(["--output-format", "stream-json"])
        # stream-json with --print requires --verbose
        if "-p" in args or "--print" in args:
            if "--verbose" not in args:
                args.append("--verbose")

        result = self._results.get(mission_id)
        final_output = ""

        try:
            proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                prompt,
                cwd=project_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._active[mission_id] = proc

            try:
                async def _read_stream():
                    nonlocal final_output
                    assert proc.stdout is not None
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace").strip()
                        if text:
                            parsed_output = self._process_stream_line(text, mission_id)
                            if parsed_output is not None:
                                final_output = parsed_output

                await asyncio.wait_for(_read_stream(), timeout=self.max_duration)
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: agent timed out after {self.max_duration}s"
            finally:
                self._active.pop(mission_id, None)

            if proc.returncode != 0:
                stderr_data = await proc.stderr.read() if proc.stderr else b""
                error = stderr_data.decode().strip()
                return f"Error (exit {proc.returncode}): {error[:500]}"

            return final_output or ""

        except FileNotFoundError:
            return f"Error: command not found: {command}"

    def _process_stream_line(self, line: str, mission_id: str) -> str | None:
        """Parse a single stream-json line and create feedback items.

        Returns the final output text if this is a result event, else None.
        """
        result = self._results.get(mission_id)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        event_type = event.get("type")

        # High-level assistant message with content blocks
        if event_type == "assistant":
            if result:
                result.turn_count += 1
            content_blocks = event.get("message", {}).get("content", [])
            for block in content_blocks:
                block_type = block.get("type")
                if block_type == "tool_use" and result:
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    emoji, label = TOOL_LABELS.get(tool_name, ("\U0001f527", tool_name))
                    detail = _summarize_tool_input(tool_name, tool_input)
                    summary = f"{emoji} {label} {detail}".strip() if detail else f"{emoji} {label}"
                    result.feedback.append(FeedbackItem(
                        timestamp=_now(),
                        kind="tool",
                        summary=summary,
                    ))
                elif block_type == "text" and result:
                    text_content = block.get("text", "")
                    if len(text_content) > 20:
                        result.feedback.append(FeedbackItem(
                            timestamp=_now(),
                            kind="text",
                            summary="\U0001f4ac Redaction de la reponse...",
                        ))

        # Result event: final output
        elif event_type == "result":
            text = event.get("result", "")
            # Also check subkey if present
            if not text:
                text = event.get("text", "")
            return text

        return None

    async def launch_background(
        self,
        mission: str,
        context_messages: list[dict],
        mission_id: str,
        project_path: str,
        agent_command: str | None = None,
    ) -> str:
        """Launch agent as a background task, return immediately with mission_id."""
        if not self.validate_path(project_path):
            result = MissionResult(
                status="failed",
                output=f"Error: path {project_path} is not in allowed_paths",
                started_at=_now(),
                finished_at=_now(),
            )
            self._results[mission_id] = result
            return mission_id

        result = MissionResult(status="running", started_at=_now())
        self._results[mission_id] = result
        task = asyncio.create_task(
            self._run_agent(mission_id, mission, context_messages, project_path, agent_command)
        )
        self._tasks[mission_id] = task
        return mission_id

    async def _run_agent(
        self,
        mission_id: str,
        mission: str,
        context_messages: list[dict],
        project_path: str,
        agent_command: str | None,
    ) -> None:
        """Execute agent in background, store result when done."""
        result = self._results[mission_id]
        try:
            output = await self.launch_streaming(
                mission=mission,
                context_messages=context_messages,
                mission_id=mission_id,
                project_path=project_path,
                agent_command=agent_command,
            )
            if output.startswith("Error"):
                result.status = "failed"
            else:
                result.status = "completed"
            result.output = output
        except Exception as e:
            result.status = "failed"
            result.output = str(e)
        finally:
            result.finished_at = _now()
            self._tasks.pop(mission_id, None)

    def get_status(self, mission_id: str) -> MissionResult | None:
        """Get the current status of a mission."""
        return self._results.get(mission_id)

    async def stop(self, mission_id: str) -> bool:
        proc = self._active.get(mission_id)
        if proc:
            proc.kill()
            await proc.wait()
            self._active.pop(mission_id, None)
            # Also mark result as failed
            if mission_id in self._results:
                self._results[mission_id].status = "failed"
                self._results[mission_id].output = "Stopped by user"
                self._results[mission_id].finished_at = _now()
            return True
        # Cancel background task if no process
        task = self._tasks.pop(mission_id, None)
        if task:
            task.cancel()
            if mission_id in self._results:
                self._results[mission_id].status = "failed"
                self._results[mission_id].output = "Cancelled"
                self._results[mission_id].finished_at = _now()
            return True
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
