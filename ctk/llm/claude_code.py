"""Claude Code CLI provider for CTK.

Runs ``claude -p <prompt>`` as a subprocess in headless mode, using the
Claude Code Pro/Max plan's OAuth auth (keychain / browser login) rather
than an ``ANTHROPIC_API_KEY``.

The conversation history is injected via ``--system-prompt`` so every turn
sees the full prior context.  The subprocess outputs ``--output-format
stream-json`` lines which we parse for real-time text deltas.

Configuration profile (in ``~/.ctk/config.json``)::

    "providers": {
        "default": "claude_code",
        "claude_code": {
            "type": "claude_code",
            "default_model": "claude-sonnet-5",
            "timeout": 300
        }
    }

``ANTHROPIC_API_KEY`` is explicitly stripped from the subprocess environment
so that Claude Code bills the Pro/Max subscription rather than API credits.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ctk.llm.base import (
    AuthenticationError,
    ChatResponse,
    LLMProvider,
    LLMProviderError,
    Message,
    MessageRole,
    ModelInfo,
    StreamEvent,
)

logger = logging.getLogger(__name__)

# Models available through Claude Code; keep this list current with Anthropic's
# model releases.  ``get_models()`` returns this static list rather than
# making a network call.
CLAUDE_CODE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="claude-opus-5",
        name="Claude Opus 5",
        context_window=200_000,
        supports_streaming=True,
        supports_system_message=True,
        supports_tools=False,
    ),
    ModelInfo(
        id="claude-sonnet-5",
        name="Claude Sonnet 5",
        context_window=200_000,
        supports_streaming=True,
        supports_system_message=True,
        supports_tools=False,
    ),
    ModelInfo(
        id="claude-fable-5",
        name="Claude Fable 5",
        context_window=200_000,
        supports_streaming=True,
        supports_system_message=True,
        supports_tools=False,
    ),
    ModelInfo(
        id="claude-haiku-4-5",
        name="Claude Haiku 4.5",
        context_window=200_000,
        supports_streaming=True,
        supports_system_message=True,
        supports_tools=False,
    ),
    ModelInfo(
        id="claude-sonnet-4-6",
        name="Claude Sonnet 4.6",
        context_window=200_000,
        supports_streaming=True,
        supports_system_message=True,
        supports_tools=False,
    ),
]

# Default model when none is configured.
DEFAULT_MODEL = "claude-sonnet-5"


def _build_subprocess_env() -> Dict[str, str]:
    """Return a sanitized environment for the ``claude`` subprocess.

    Removes ``ANTHROPIC_API_KEY`` so Claude Code uses the OAuth/keychain
    credentials (Pro/Max plan) rather than billing API credits.
    """
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _format_history_as_system_prompt(
    messages: List[Message],
) -> tuple[Optional[str], Optional[str]]:
    """Split ``messages`` into (system_prompt_text, conversation_history_text).

    The returned system_prompt_text combines any SYSTEM message with the
    formatted prior user/assistant turns so the subprocess sees full context.
    The last USER message is intentionally excluded — it becomes the ``-p``
    positional argument.

    Returns ``(None, None)`` when there is nothing to inject.
    """
    # Separate roles
    system_parts: List[str] = []
    prior_turns: List[Message] = []

    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            system_parts.append(msg.content.strip())

    # Prior user/assistant turns = everything except the last message
    # (the last message must be USER — that's what we'll send as the prompt)
    turn_messages = [m for m in messages if m.role != MessageRole.SYSTEM]
    if turn_messages:
        prior_turns = turn_messages[:-1]  # everything except the final user msg

    history_lines: List[str] = []
    for msg in prior_turns:
        if msg.role == MessageRole.USER:
            history_lines.append(f"[Human]: {msg.content.strip()}")
        elif msg.role == MessageRole.ASSISTANT:
            history_lines.append(f"[Assistant]: {msg.content.strip()}")

    # Build combined system prompt text
    parts: List[str] = []
    if system_parts:
        parts.append("\n\n".join(system_parts))
    if history_lines:
        parts.append("--- Prior conversation ---\n" + "\n\n".join(history_lines))

    combined = "\n\n".join(parts) if parts else None
    return combined, None  # second slot reserved for future use


class ClaudeCodeProvider(LLMProvider):
    """LLM provider that delegates to the ``claude`` CLI in headless mode.

    Uses ``claude -p <prompt> --output-format stream-json --verbose
    --include-partial-messages`` and parses the streaming JSON output.

    Configuration keys:
    * ``type``          — must be ``"claude_code"`` (read by factory).
    * ``default_model`` — model alias (default ``claude-sonnet-5``).
    * ``timeout``       — subprocess timeout in seconds (default 300).
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        if not self.model:
            self.model = config.get("default_model") or DEFAULT_MODEL
        self.timeout: float = config.get("timeout") or 300.0
        self._claude_bin: Optional[str] = shutil.which("claude")
        # Set by _iter_stream_events when the "system/init" line is parsed.
        # The TUI reads these after stream_chat() returns to persist the
        # session_id into the conversation's custom_data for future resumption.
        self.last_session_id: Optional[str] = None
        self.last_session_cwd: Optional[str] = None

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the ``claude`` binary is on PATH."""
        return self._claude_bin is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_cmd(
        self,
        prompt: str,
        system_prompt: Optional[str],
        session_id: Optional[str] = None,
    ) -> List[str]:
        """Construct the ``claude`` subprocess argv.

        Two operating modes:

        * **Resuming** (``session_id`` provided): ``--resume <id>`` replays
          Claude Code's own session so no history re-injection is needed and
          the session file is kept for future turns.
        * **Fresh** (no ``session_id``): starts a new persistent session so
          subsequent turns can resume it. History is injected via
          ``--system-prompt`` when there are prior turns to include.
        """
        if self._claude_bin is None:
            raise LLMProviderError(
                "The 'claude' binary is not on PATH. "
                "Install Claude Code: https://claude.ai/code"
            )
        cmd: List[str] = [
            self._claude_bin,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if self.model:
            cmd += ["--model", self.model]

        if session_id:
            # Resume existing session — Claude Code already holds the context.
            cmd += ["--resume", session_id]
        else:
            # New session: inject any prior-turn context and let the session
            # file persist so the next turn can use --resume.
            if system_prompt:
                cmd += ["--system-prompt", system_prompt]
        return cmd

    def _extract_last_user_message(self, messages: List[Message]) -> str:
        """Return the content of the last USER message."""
        self.validate_messages(messages)
        for msg in reversed(messages):
            if msg.role == MessageRole.USER:
                return msg.content
        raise LLMProviderError("No USER message found in message list")

    # ------------------------------------------------------------------
    # Session cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def _session_jsonl_path(session_id: str, cwd: str) -> Optional[Path]:
        """Return the path to Claude Code's JSONL file for *session_id*.

        Claude Code stores each headless session as::

            ~/.claude/projects/<cwd-with-slashes-as-dashes>/<session_id>.jsonl

        For example, CWD ``/Users/alice/code`` becomes the project dir
        ``~/.claude/projects/-Users-alice-code/``.
        """
        if not session_id or not cwd:
            return None
        # Replace every "/" with "-"; the leading "/" turns into a leading "-"
        project_dir_name = cwd.replace("/", "-")
        return (
            Path.home()
            / ".claude"
            / "projects"
            / project_dir_name
            / f"{session_id}.jsonl"
        )

    @staticmethod
    def _delete_session_artifacts(session_id: str, cwd: str) -> None:
        """Delete the JSONL file Claude Code wrote for the completed session."""
        jsonl = ClaudeCodeProvider._session_jsonl_path(session_id, cwd)
        if jsonl is None:
            return
        try:
            if jsonl.exists():
                jsonl.unlink()
                logger.debug("Deleted Claude Code session artifact: %s", jsonl)
        except OSError as exc:
            # Non-fatal: log and continue
            logger.debug("Could not delete session artifact %s: %s", jsonl, exc)

    # ------------------------------------------------------------------
    # Streaming helpers
    # ------------------------------------------------------------------

    def _iter_stream_events(
        self,
        proc: "subprocess.Popen[bytes]",
    ) -> Iterator[StreamEvent]:
        """Parse stream-json lines from *proc* stdout and yield StreamEvents.

        Relevant line types:
        - ``system`` / ``init``      → sets ``self.last_session_id`` / ``last_session_cwd``
        - ``stream_event`` / ``content_block_delta`` / ``text_delta`` → text chunk
        - ``result``                 → done + finish_reason
        Everything else is silently skipped.
        """
        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("claude: non-JSON line: %s", line[:200])
                    continue

                event_type = obj.get("type")

                # Init event — capture session_id and cwd for the caller
                if event_type == "system" and obj.get("subtype") == "init":
                    self.last_session_id = obj.get("session_id") or None
                    self.last_session_cwd = obj.get("cwd") or None
                    continue

                # Real-time text delta
                if event_type == "stream_event":
                    inner = obj.get("event") or {}
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield StreamEvent(kind="text", text=text)

                # Final result line → "done" event
                elif event_type == "result":
                    is_error = obj.get("is_error", False)
                    if is_error:
                        err_msg = obj.get("result") or "claude subprocess error"
                        raise LLMProviderError(f"Claude Code error: {err_msg}")
                    finish_reason = obj.get("stop_reason") or "end_turn"
                    yield StreamEvent(kind="done", finish_reason=finish_reason)
                    return

        finally:
            proc.stdout.close()
            proc.wait()

    def _run_streaming(
        self,
        messages: List[Message],
        session_id: Optional[str] = None,
    ) -> Iterator[StreamEvent]:
        """Start the ``claude`` subprocess and yield StreamEvents.

        When *session_id* is provided the subprocess uses ``--resume`` to
        reconnect to Claude Code's existing session — no history re-injection
        needed, saving tokens on every turn after the first.  When absent a
        fresh session is started and its ID is captured via
        ``self.last_session_id`` for the caller to persist.
        """
        self.last_session_id = None
        self.last_session_cwd = None

        prompt = self._extract_last_user_message(messages)
        # Only inject history when starting fresh (no prior session to resume)
        system_prompt: Optional[str] = None
        if not session_id:
            system_prompt, _ = _format_history_as_system_prompt(messages)

        cmd = self._build_cmd(prompt, system_prompt, session_id=session_id)
        logger.debug("ClaudeCodeProvider cmd: %s", cmd)

        env = _build_subprocess_env()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise LLMProviderError(f"Failed to launch claude: {exc}") from exc

        yield from self._iter_stream_events(proc)

        # Check for subprocess error exit (non-zero after stream consumed)
        if proc.returncode and proc.returncode != 0:
            stderr_output = b""
            if proc.stderr:
                stderr_output = proc.stderr.read()
            logger.warning(
                "claude exited %d: %s", proc.returncode, stderr_output[:500]
            )

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def stream_chat(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Stream text tokens from Claude Code CLI.

        *temperature* and *max_tokens* are silently ignored — the ``claude``
        CLI does not expose those knobs in headless mode.

        Pass ``claude_code_session_id=<id>`` via *kwargs* to resume an
        existing Claude Code session instead of re-injecting history.  After
        this method returns ``self.last_session_id`` holds the session ID for
        the caller to persist.
        """
        session_id: Optional[str] = kwargs.get("claude_code_session_id")
        for event in self._run_streaming(messages, session_id=session_id):
            if event.kind == "text" and event.text:
                yield event.text

    def chat(
        self,
        messages: List[Message],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Blocking chat: collect all streaming tokens into a ChatResponse."""
        session_id: Optional[str] = kwargs.get("claude_code_session_id")
        chunks: List[str] = []
        finish_reason: Optional[str] = None
        for event in self._run_streaming(messages, session_id=session_id):
            if event.kind == "text":
                chunks.append(event.text)
            elif event.kind == "done":
                finish_reason = event.finish_reason

        return ChatResponse(
            content="".join(chunks),
            model=self.model or DEFAULT_MODEL,
            finish_reason=finish_reason,
            metadata={"provider": "claude_code"},
        )

    def get_models(self) -> List[ModelInfo]:
        """Return the static list of Claude Code models."""
        return list(CLAUDE_CODE_MODELS)

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def supports_tool_calling(self) -> bool:
        # Claude Code runs its own tool loop internally; CTK's OpenAI-style
        # tool schema injection is not supported.
        return False

    @property
    def name(self) -> str:
        return "claude_code"
