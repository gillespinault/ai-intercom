from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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

    async def stop(self, mission_id: str) -> bool:
        proc = self._active.get(mission_id)
        if proc:
            proc.kill()
            await proc.wait()
            self._active.pop(mission_id, None)
            return True
        return False
