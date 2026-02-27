from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class MissionResult:
    """Tracks the lifecycle of a background agent mission."""
    status: str = "running"  # running, completed, failed
    output: str | None = None
    started_at: str = ""
    finished_at: str | None = None


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
            output = await self.launch(
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
