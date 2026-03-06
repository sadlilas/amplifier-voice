"""VoiceConnection - manages one voice session lifecycle.

Adapted for amplifierd daemon: uses SessionManager + EventBus instead of
FoundationBackend. The daemon's EventBus handles SSE streaming natively,
so this connection primarily manages session state and the voice-specific
event queue for the browser's custom SSE stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_EVENT_QUEUE_MAX_SIZE = 10000

# Synthetic version for dynamically-constructed child bundles.
_CHILD_BUNDLE_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Event forwarding for delegation status UI
# ---------------------------------------------------------------------------
# When the voice UI's DelegationOverlay is active, it needs three event types
# from child sessions:  tool_call (with delegating_agent), session_fork, and
# delegate_agent_completed.  These are produced by _ForwardingHook which is
# appended to each child bundle spawned via the delegate tool.
# ---------------------------------------------------------------------------


def _map_child_event(event: str, data: dict, agent_name: str) -> dict | None:
    """Map a child Amplifier event to a parent SSE wire dict.

    Returns None for events not worth forwarding.
    Adds delegating_agent to every forwarded dict.
    """
    if event == "tool:pre":
        return {
            "type": "tool_call",
            "tool_name": data.get("tool_name"),
            "tool_call_id": data.get("tool_call_id"),
            "arguments": data.get("arguments"),
            "status": "pending",
            "delegating_agent": agent_name,
        }
    if event == "session:fork":
        return {
            "type": "session_fork",
            "child_session_id": data.get("child_session_id"),
            "agent": data.get("agent"),
            "delegating_agent": agent_name,
        }
    if event == "orchestrator:complete":
        return {
            "type": "delegate_agent_completed",
            "delegating_agent": agent_name,
        }
    return None


class _ForwardingHook:
    """Lightweight hook appended to child bundles for delegation event forwarding."""

    name = "delegation-event-forwarder"
    priority = 90

    def __init__(
        self,
        forwarder: Callable[[dict], None],
        agent_name: str,
    ) -> None:
        self._forwarder = forwarder
        self._agent_name = agent_name

    async def __call__(self, event: str, data: dict) -> None:
        wire = _map_child_event(event, data, self._agent_name)
        if wire is not None:
            try:
                self._forwarder(wire)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "delegation-event-forwarder: failed to forward %s",
                    event,
                    exc_info=True,
                )


class VoiceConnection:
    """Manages one voice session lifecycle: create, teardown, end, cancel."""

    def __init__(
        self,
        repository: Any,
        session_manager: Any,
        event_bus: Any,
        sessions_dir: Any = None,
    ) -> None:
        self._repository = repository
        self._session_manager = session_manager
        self._event_bus = event_bus
        self._sessions_dir = sessions_dir
        self._event_queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_MAX_SIZE
        )
        self._session_id: str | None = None
        self._handle: Any = None
        self._subscription_task: asyncio.Task[None] | None = None

    @property
    def event_queue(self) -> asyncio.Queue[Any]:
        """The asyncio.Queue used as the event bus for this connection."""
        return self._event_queue

    @property
    def session_id(self) -> str | None:
        """The current session ID, or None if not yet created."""
        return self._session_id

    async def create(self, workspace_root: str, bundle_name: str | None = None) -> str:
        """Create a session for this voice connection via amplifierd SessionManager.

        1. Resolves bundle_name: uses the provided value, falls back to
           ``session_manager._settings.default_bundle``, or generates a plain
           UUID (voice-only mode) when no session manager / bundle is available.
        2. Creates session via session_manager.create()
        3. Re-registers spawn capability with event forwarding for delegation UI
        4. Subscribes to EventBus for this session's events
        5. Forwards events to the voice-specific event queue for SSE

        Voice-only mode (no amplifierd session): the WebRTC/audio path works
        normally but the delegate tool will report "No active session".
        """
        # Resolve bundle: explicit arg → session_manager default → None (UUID fallback)
        if bundle_name is None and self._session_manager is not None:
            bundle_name = getattr(
                getattr(self._session_manager, "_settings", None),
                "default_bundle",
                None,
            )

        # If no session manager or no bundle configured, run in voice-only mode.
        if self._session_manager is None or bundle_name is None:
            from uuid import uuid4

            self._session_id = str(uuid4())
            logger.info(
                "Voice session created in voice-only mode (no bundle configured): %s",
                self._session_id,
            )
            return self._session_id

        handle = await self._session_manager.create(
            bundle_name=bundle_name,
            working_dir=workspace_root,
        )
        self._handle = handle
        self._session_id = handle.session_id

        # Re-register spawn capability with event forwarding so child
        # session tool calls appear in the voice delegation overlay.
        self._register_spawn_with_forwarding()

        # Subscribe to EventBus and forward events to our queue
        self._subscription_task = asyncio.create_task(
            self._forward_events(handle.session_id)
        )

        return handle.session_id

    async def _forward_events(self, session_id: str) -> None:
        """Subscribe to EventBus and forward events to the voice event queue.

        Events from the EventBus arrive as TransportEvent objects with
        ``event_name`` and ``data`` fields (kernel format).  The voice UI
        expects wire dicts with a ``type`` field as mapped by
        EventStreamingHook.  We use the hook's mapping logic to translate.
        """
        from voice_plugin.protocols.event_streaming import EventStreamingHook

        mapper = EventStreamingHook(self._event_queue)

        try:
            async for event in self._event_bus.subscribe(session_id=session_id):
                if hasattr(event, "event_name") and hasattr(event, "data"):
                    sanitized = mapper._sanitize_for_streaming(event.data)
                    wire = mapper._map_event_to_message(event.event_name, sanitized)
                    if wire is not None:
                        with contextlib.suppress(asyncio.QueueFull):
                            self._event_queue.put_nowait(wire)
                else:
                    msg = (
                        event.to_sse_dict() if hasattr(event, "to_sse_dict") else event
                    )
                    with contextlib.suppress(asyncio.QueueFull):
                        self._event_queue.put_nowait(msg)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("Event forwarding error for %s", session_id, exc_info=True)

    async def teardown(self) -> None:
        """Handle client disconnect: mark session disconnected, cleanup."""
        try:
            if self._session_id is not None:
                self._repository.update_status(self._session_id, "disconnected")
        finally:
            self._cancel_subscription()
            self._event_queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX_SIZE)

    async def end(self, reason: str = "user_ended") -> None:
        """End the session permanently."""
        try:
            if self._session_id is not None and self._handle is not None:
                await self._handle.cleanup()
                self._repository.end_conversation(self._session_id, reason)
        finally:
            self._cancel_subscription()

    async def cancel(self, level: str = "graceful") -> None:
        """Cancel the running session."""
        if self._handle is not None:
            immediate = level == "immediate"
            self._handle.cancel(immediate=immediate)

    async def execute(self, prompt: str) -> str:
        """Execute a prompt on the session (for delegate tool)."""
        if self._handle is None:
            return "No active session"
        result = await self._handle.execute(prompt)
        return str(result) if result is not None else ""

    # ------------------------------------------------------------------
    # Spawn re-registration with event forwarding
    # ------------------------------------------------------------------

    def _register_spawn_with_forwarding(self) -> None:
        """Re-register ``session.spawn`` with a forwarding hook for delegation UI.

        amplifierd registers a basic ``session.spawn`` capability during
        session creation.  This method replaces it with one that attaches a
        ``_ForwardingHook`` to every child bundle, so child tool calls and
        completion events are forwarded to the voice SSE stream.
        """
        if self._handle is None:
            return

        session = getattr(self._handle, "session", None)
        prepared = getattr(self._handle, "_prepared_bundle", None)
        if session is None or prepared is None:
            logger.debug(
                "Cannot register spawn forwarding: missing session or prepared"
            )
            return

        try:
            from amplifier_foundation import Bundle  # type: ignore[import]
        except ImportError:
            logger.debug(
                "amplifier_foundation not available; skipping spawn forwarding"
            )
            return

        coordinator = session.coordinator
        session_id = self._handle.session_id

        def event_forwarder(wire_dict: dict) -> None:
            self._event_queue.put_nowait(wire_dict)

        async def spawn_fn(
            agent_name: str,
            instruction: str,
            parent_session: Any,
            agent_configs: dict[str, dict[str, Any]] | None = None,
            sub_session_id: str | None = None,
            orchestrator_config: dict[str, Any] | None = None,
            parent_messages: list[dict[str, Any]] | None = None,
            tool_inheritance: dict[str, list[str]] | None = None,
            hook_inheritance: dict[str, list[str]] | None = None,
            provider_preferences: list[Any] | None = None,
            self_delegation_depth: int = 0,
            **kwargs: Any,
        ) -> dict[str, Any]:
            configs = agent_configs or {}

            # --- Resolve agent name -> Bundle config ---
            if agent_name == "self":
                config: dict[str, Any] = {}
            elif agent_name in configs:
                config = configs[agent_name]
            elif (
                hasattr(prepared, "bundle")
                and hasattr(prepared.bundle, "agents")
                and agent_name in prepared.bundle.agents
            ):
                config = prepared.bundle.agents[agent_name]
            else:
                available = sorted(
                    list(configs.keys())
                    + (
                        list(prepared.bundle.agents.keys())
                        if hasattr(prepared, "bundle")
                        and hasattr(prepared.bundle, "agents")
                        else []
                    )
                )
                raise ValueError(
                    f"Agent '{agent_name}' not found. Available: {available}"
                )

            # --- Build hooks with forwarding hook ---
            _base_hooks: list = list(config.get("hooks", []))
            _child_hooks = [
                *_base_hooks,
                _ForwardingHook(event_forwarder, agent_name),
            ]

            child_bundle = Bundle(
                name=agent_name,
                version=_CHILD_BUNDLE_VERSION,
                session=config.get("session", {}),
                providers=config.get("providers", []),
                tools=config.get("tools", []),
                hooks=_child_hooks,
                instruction=(
                    config.get("instruction")
                    or config.get("system", {}).get("instruction")
                ),
            )

            logger.debug(
                "Spawning sub-session: agent=%s session_id=%s parent=%s",
                agent_name,
                sub_session_id,
                session_id,
            )

            return await prepared.spawn(
                child_bundle=child_bundle,
                instruction=instruction,
                session_id=sub_session_id,
                parent_session=parent_session,
                orchestrator_config=orchestrator_config,
                parent_messages=parent_messages,
                provider_preferences=provider_preferences,
                self_delegation_depth=self_delegation_depth,
            )

        coordinator.register_capability("session.spawn", spawn_fn)
        logger.info(
            "session.spawn re-registered with event forwarding for session %s",
            session_id,
        )

    def _cancel_subscription(self) -> None:
        """Cancel the EventBus subscription task."""
        if self._subscription_task is not None:
            self._subscription_task.cancel()
            self._subscription_task = None
