"""
Agent Event Cache Client
========================

HTTP client for the agent-event-cache service. Reads Slack/Teams message
history (replacing the S3-backed _read_channel_mirror path when enabled) and
sends outbound messages on the agent's behalf, so the sandbox never needs a
Slack or Graph credential of its own.

Base URL:     https://agent-event-cache.public.<environment>.myninja.ai
Auth:         Bearer ANTHROPIC_AUTH_TOKEN (from /root/.claude/settings.json)
Feature flags: this module is flag-agnostic — the callers gate themselves.
    reads/tracking       "use_agent_event_cache" in /dev/shm/sandbox_metadata.json

Outbound endpoints (one shape per provider — see MessageProvider):
    POST /messages/send      text, or a threaded reply
    POST /messages/welcome   first-contact message, at most once per channel
    POST /messages/react     emoji reaction
    POST /messages/unreact   remove the agent's emoji reaction
    POST /messages/upload    multipart file share

Read endpoints:
    GET /db/messages         channel or thread history
    GET /db/channels         channels the caller may access
    GET /db/channel-info     one channel's metadata
    GET /db/channel-members  members with decrypted profiles
    GET /db/workspace        workspace name and domain
    GET /teams/service-url/{tenant_id}
    GET /tokens              provider token proxy

Pending-message tracking:
    POST /db/message-states  per-message triage decisions (will reply / won't)
    POST /db/message-replied a reply was sent, clear the pending row

Request and response schemas come from ``agent_event_cache_models``, a copy of
the generated client's models.py, so the wire contract is generated rather than
retyped. The copy is what reaches production — the agent ships as a source zip
to a host with no CodeArtifact access — and a CI test fails when it drifts from
the real package. This module is transport only: base URL, auth, timeouts and
retry policy. See the note on NinjaAgentEventCacheApiClient below.

The generated transport is deliberately not used. Every request it makes is
wrapped in a NinjaMetricsCollector, which (a) raises InvalidNamespaceError
unless SERVICE_NAME is set in the environment and (b) writes a CloudWatch EMF
JSON blob to stdout per call — which in a sandbox is the agent's own console.
It also has no notion of the fallback-to-provider policy the adapters rely on.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from clients.agent_event_cache_models import (
    AddCompletedActionRequest,
    AddCompletedActionResponse,
    CompleteActivityRequest,
    CompleteActivityResponse,
    GetChannelInfoResponse,
    GetChannelMembersResponse,
    GetChannelsResponse,
    GetMessagesRequest,
    GetMessagesResponse,
    GetProviderTokenResponse,
    GetTeamsServiceUrlResponse,
    GetWorkspaceResponse,
    MessageProvider,
    ReactRequest,
    ReactResponse,
    ReportMessageStatesRequest,
    ReportMessageStatesResponse,
    ReportRepliedRequest,
    ReportRepliedResponse,
    SendMessageRequest,
    SendMessageResponse,
    StartActivityRequest,
    StartActivityResponse,
    TickActivityRequest,
    TickActivityResponse,
    UploadFormData,
    UploadResponse,
    WelcomeRequest,
    WelcomeResponse,
)
from core.logging import get_logger
from core.metadata import load_sandbox_metadata, service_environment
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# core.logging, not logging.getLogger(__name__): the latter has no handler of
# its own and propagates to a root that has none and sits at WARNING, so every
# info() on this client was being dropped before it reached a handler. That made
# the whole service path invisible in the monitor log while the direct Slack
# path kept logging MSG SENT.
logger = get_logger("event_cache")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SETTINGS_PATHS = [
    Path("/root/.claude/settings.json"),
    Path(__file__).resolve().parent.parent / "settings.json",
]
_RETRIABLE_STATUS = {429, 500, 502, 503, 504}
# Uploads carry up to 25MB of bytes and then wait on the provider's own upload,
# so they need a far longer ceiling than the read endpoints.
UPLOAD_TIMEOUT_SECONDS = 120


def _is_retriable(exc: BaseException) -> bool:
    # Network-level errors
    if isinstance(
        exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
    ):
        return True
    # Transient HTTP status codes
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRIABLE_STATUS
    return False


def _as_payload(model) -> Dict[str, Any]:
    """Serialize a generated request model for the wire.

    The generated models are plain pydantic with no serialization helpers, so
    this lives here rather than being added to them. ``exclude_none`` matters:
    the service applies its own defaults to fields the caller left unset, so
    sending explicit nulls would override them.
    """
    return model.model_dump(mode="json", exclude_none=True)


def is_event_cache_enabled() -> bool:
    """True if sandbox_metadata.json has "use_agent_event_cache": true."""
    return load_sandbox_metadata().get("use_agent_event_cache", False) is True


def _get_base_url() -> str:
    """
    Build service base URL from sandbox_metadata environment field.

    Returns: https://agent-event-cache.public.<env>.myninja.ai
    Raises RuntimeError if environment is unavailable.
    """
    # An explicit override wins outright, matching docker/slack_authorize.py.
    override = os.environ.get("AGENT_EVENT_CACHE_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")

    environment = service_environment()
    if not environment:
        raise RuntimeError(
            "Cannot determine agent-event-cache URL: "
            "'environment' missing from /dev/shm/sandbox_metadata.json"
        )
    return f"https://agent-event-cache.public.{environment}.myninja.ai"


def _get_auth_token() -> str:
    """
    Read ANTHROPIC_AUTH_TOKEN from settings.json.
    Raises RuntimeError if no token found.
    """
    # Read ANTHROPIC_AUTH_TOKEN from settings files
    for path in SETTINGS_PATHS:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                token = data.get("env", {}).get("ANTHROPIC_AUTH_TOKEN", "")
                if token:
                    return token.strip().removeprefix("Bearer ").removeprefix("bearer ")
            except (json.JSONDecodeError, IOError):
                continue

    raise RuntimeError("ANTHROPIC_AUTH_TOKEN not found in settings.json")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AgentEventCacheClient:
    """
    Client for the agent-event-cache GET /db/messages endpoint.

    Usage:
        from clients.agent_event_cache_client import (
            AgentEventCacheClient, GetMessagesRequest, is_event_cache_enabled
        )

        if is_event_cache_enabled():
            client = AgentEventCacheClient()
            request = GetMessagesRequest(
                workspace_id="T0A9Q27KD1T",
                channel_id="C0AAAAMBR1R",
                limit=50,
            )
            response = client.get_messages(request)
            messages = response.messages
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: int = 15,
    ):
        self._base_url = (base_url or _get_base_url()).rstrip("/")
        self._auth_token = auth_token or _get_auth_token()
        self._timeout = timeout

    def _auth_headers(self) -> Dict[str, str]:
        """Authorization only, for multipart posts.

        A multipart body needs its boundary in the Content-Type header, and
        only ``requests`` knows the boundary it generated — setting the header
        ourselves would produce one the server cannot parse.
        """
        return {"Authorization": f"Bearer {self._auth_token}"}

    def _headers(self) -> Dict[str, str]:
        """Return standard request headers."""
        return {**self._auth_headers(), "Content-Type": "application/json"}

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retriable),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def get_messages(self, request: GetMessagesRequest) -> GetMessagesResponse:
        """
        Fetch messages from the agent-event-cache service.

        Args:
            request: Validated GetMessagesRequest with query parameters

        Returns:
            GetMessagesResponse with messages and pagination info

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            pydantic.ValidationError: If response doesn't match schema
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/messages",
            params=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetMessagesResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_messages succeeded",
            extra={
                "channel_id": request.channel_id,
                "workspace_id": request.workspace_id,
                "message_count": len(result.messages),
                "total": result.total,
                "has_next_page": result.next_token is not None,
            },
        )
        return result

    def get_channels(self) -> GetChannelsResponse:
        """
        List all channels the authenticated caller is authorized to access.

        Returns:
            GetChannelsResponse with list of channels and total count

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/channels",
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetChannelsResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_channels succeeded",
            extra={"total": result.total},
        )
        return result

    def get_channel_info(self, channel_id: str) -> GetChannelInfoResponse:
        """
        Get metadata for a single channel from the database.

        Args:
            channel_id: Slack channel ID (e.g. "C0AAAAMBR1R")

        Returns:
            GetChannelInfoResponse with channel metadata

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/channel-info",
            params={"channel_id": channel_id},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetChannelInfoResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_channel_info succeeded",
            extra={"channel_id": channel_id, "name": result.name},
        )
        return result

    def get_channel_members(self, channel_id: str) -> GetChannelMembersResponse:
        """
        List active members of a channel with decrypted user profiles.

        Args:
            channel_id: Slack channel ID (e.g. "C0AAAAMBR1R")

        Returns:
            GetChannelMembersResponse with member list and total count

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/channel-members",
            params={"channel_id": channel_id},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetChannelMembersResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_channel_members succeeded",
            extra={"channel_id": channel_id, "total": result.total},
        )
        return result

    def can_get_messages(self, workspace_id: str, channel_id: str) -> bool:
        """
        True if the caller is authorized to read this channel's history.

        Issues a single-message read and maps a 401/403 to False. Any other
        error (timeout, 5xx, etc.) propagates — those are retrieval failures,
        not authorization answers.
        """
        try:
            self.get_messages(
                GetMessagesRequest(
                    workspace_id=workspace_id, channel_id=channel_id, limit=1
                )
            )
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403):
                return False
            raise
        return True

    def get_workspace(self, channel_id: str) -> GetWorkspaceResponse:
        """
        Get workspace name and domain for the workspace that owns a channel.

        Args:
            channel_id: Slack channel ID — workspace_id is derived from
                        the channel_info row server-side.

        Returns:
            GetWorkspaceResponse with workspace_id, name, and domain

        Raises:
            requests.HTTPError: On 4xx/5xx responses
            RuntimeError: On configuration errors
        """
        resp = requests.get(
            f"{self._base_url}/db/workspace",
            params={"channel_id": channel_id},
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetWorkspaceResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_workspace succeeded",
            extra={
                "channel_id": channel_id,
                "workspace_id": result.workspace_id,
                "name": result.name,
            },
        )
        return result

    def get_teams_service_url(self, tenant_id: str) -> GetTeamsServiceUrlResponse:
        """
        Fetch the Teams bot's serviceUrl stored per Teams tenant/workspace id.
        pararm: tenant_id: Microsoft Entra (Azure AD) tenant id.
        """
        resp = requests.get(
            f"{self._base_url}/teams/service-url/{tenant_id}",
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = GetTeamsServiceUrlResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache get_teams_service_url succeeded",
            extra={
                "tenant_id": tenant_id,
                "has_service_url": result.service_url is not None,
            },
        )
        return result

    def get_token(
        self,
        provider_id: str,
        target: Optional[str] = None,
        repositories: Optional[List[str]] = None,
    ) -> GetProviderTokenResponse:
        """Fetch a provider token from the token proxy.

        GET /tokens?provider_id=Slack
        GET /tokens?provider_id=Github[&target=<owner>&repositories=<repo>]

        ``target``/``repositories`` scope a GitHub token to one account and its
        listed repos; token-service needs them for an org repo (OP-5919).
        """
        params: Dict[str, Any] = {"provider_id": provider_id}
        if target:
            params["target"] = target
        if repositories:
            params["repositories"] = repositories
        resp = requests.get(
            f"{self._base_url}/tokens",
            params=params,
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return GetProviderTokenResponse.model_validate(resp.json())

    # -----------------------------------------------------------------------
    # Outbound messaging
    #
    # None of these retry. The read endpoints can be replayed for free; a write
    # cannot — a 502 or a dropped connection does not tell us whether the
    # message reached Slack, and retrying a send that did land posts it twice.
    # Callers fall back to the provider API instead, which fails loudly rather
    # than duplicating.
    # -----------------------------------------------------------------------

    def send_message(self, request: SendMessageRequest) -> SendMessageResponse:
        """Post a message, or a threaded reply when ``thread_id`` is set.

        Raises:
            requests.HTTPError: On 4xx/5xx responses. The provider's own error
                code (``missing_scope``, ``channel_not_found``, Graph 403) is
                preserved in the response detail.
        """
        resp = requests.post(
            f"{self._base_url}/messages/send",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = SendMessageResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache send_message succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": result.channel_id,
                "message_id": result.message_id,
                "threaded": request.thread_id is not None,
            },
        )
        return result

    def send_welcome(self, request: WelcomeRequest) -> WelcomeResponse:
        """Post the agent's first-contact message, at most once per channel.

        The idempotency check runs server-side against cached history, so it
        survives a sandbox reclone — unlike the local flag Phantom used to
        keep, which was lost on every reclone and re-greeted the channel.

        ``posted=False`` is a success. Do not retry on it.
        """
        resp = requests.post(
            f"{self._base_url}/messages/welcome",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = WelcomeResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache send_welcome succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": result.channel_id,
                "posted": result.posted,
                "reason": result.reason,
            },
        )
        return result

    def react(self, request: ReactRequest) -> ReactResponse:
        """Add an emoji reaction. Idempotent — re-reacting is a success."""
        resp = requests.post(
            f"{self._base_url}/messages/react",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = ReactResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache react succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": request.channel_id,
                "message_id": result.message_id,
                "emoji": request.emoji,
            },
        )
        return result

    def unreact(self, request: ReactRequest) -> ReactResponse:
        """Remove the agent's emoji reaction. Takes the same body as react().

        Idempotent — removing a reaction that isn't there is a success.
        """
        resp = requests.post(
            f"{self._base_url}/messages/unreact",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = ReactResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache unreact succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": request.channel_id,
                "message_id": result.message_id,
                "emoji": request.emoji,
            },
        )
        return result

    def upload_file(
        self,
        *,
        provider: MessageProvider,
        workspace_id: str,
        channel_id: str,
        content: bytes,
        filename: str,
        title: Optional[str] = None,
        comment: Optional[str] = None,
        thread_id: Optional[str] = None,
        username: Optional[str] = None,
        icon_url: Optional[str] = None,
        icon_emoji: Optional[str] = None,
    ) -> UploadResponse:
        """Upload a file and share it in the conversation as one message.

        Multipart rather than JSON, because the payload is bytes. Each
        provider's two- or three-step upload dance happens server-side.

        Note the service PUTs the bytes as ``application/octet-stream``, so a
        caller that needs a specific content type on the stored file (Teams
        audio, which needs ``audio/*`` to render a player) must not use this.

        Raises:
            requests.HTTPError: 413 when the file exceeds the service's 25MB
                ceiling, otherwise as for send_message.
        """
        form_data = UploadFormData(
            provider=provider,
            workspace_id=workspace_id,
            channel_id=channel_id,
            title=title,
            comment=comment,
            thread_id=thread_id,
            username=username,
            icon_url=icon_url,
            icon_emoji=icon_emoji,
        )
        resp = requests.post(
            f"{self._base_url}/messages/upload",
            files={"file": (filename, content, "application/octet-stream")},
            data=_as_payload(form_data),
            headers=self._auth_headers(),
            timeout=max(self._timeout, UPLOAD_TIMEOUT_SECONDS),
        )
        resp.raise_for_status()
        result = UploadResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache upload_file succeeded",
            extra={
                "provider": str(provider),
                "channel_id": result.channel_id,
                "file_name": filename,
                "size_bytes": len(content),
                "file_id": result.file_id,
            },
        )
        return result

    # -----------------------------------------------------------------------
    # Pending-message tracking
    # -----------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retriable),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def report_message_states(
        self, request: ReportMessageStatesRequest
    ) -> ReportMessageStatesResponse:
        """POST /db/message-states — report per-message triage decisions.

        Fire-and-forget callers should wrap this in try/except. The method
        itself does raise on HTTP errors so that tests and direct callers
        can observe failures.
        """
        resp = requests.post(
            f"{self._base_url}/db/message-states",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = ReportMessageStatesResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache report_message_states succeeded",
            extra={
                "channel_id": request.channel_id,
                "workspace_id": request.workspace_id,
                "reported": len(request.states),
                "updated": result.updated,
                "deleted": result.deleted,
                "not_found": result.not_found,
            },
        )
        return result

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retriable),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def report_replied(self, request: ReportRepliedRequest) -> ReportRepliedResponse:
        """POST /db/message-replied — report that a reply was sent.

        Fire-and-forget callers should wrap this in try/except.
        """
        resp = requests.post(
            f"{self._base_url}/db/message-replied",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = ReportRepliedResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache report_replied succeeded",
            extra={
                "channel_id": request.channel_id,
                "workspace_id": request.workspace_id,
                "thread_id": request.thread_id,
                "deleted": result.deleted,
            },
        )
        return result

    # -----------------------------------------------------------------------
    # Activity progress
    #
    # Server-side progress bar. The service owns rendering and updating the
    # message; the sandbox just signals lifecycle events and completed steps.
    # Like other writes, these do not retry — a dropped tick or action is
    # cosmetic and the next timer cycle catches up.
    # -----------------------------------------------------------------------

    def start_activity(self, request: StartActivityRequest) -> StartActivityResponse:
        """POST /activity/start — begin a progress bar in the thread."""
        resp = requests.post(
            f"{self._base_url}/activity/start",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = StartActivityResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache start_activity succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": request.channel_id,
                "thread_id": request.thread_id,
                "activity_id": result.id,
            },
        )
        return result

    def add_completed_action(
        self, request: AddCompletedActionRequest
    ) -> AddCompletedActionResponse:
        """POST /activity/add-completed-action — report a finished step."""
        resp = requests.post(
            f"{self._base_url}/activity/add-completed-action",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = AddCompletedActionResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache add_completed_action succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": request.channel_id,
                "label": request.label,
                "action_count": result.action_count,
            },
        )
        return result

    def tick_activity(self, request: TickActivityRequest) -> TickActivityResponse:
        """POST /activity/tick — heartbeat to advance the progress bar."""
        resp = requests.post(
            f"{self._base_url}/activity/tick",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = TickActivityResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache tick_activity succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": request.channel_id,
                "activity_id": result.id,
                "action_count": result.action_count,
            },
        )
        return result

    def complete_activity(
        self, request: CompleteActivityRequest
    ) -> CompleteActivityResponse:
        """POST /activity/complete — remove the progress bar."""
        resp = requests.post(
            f"{self._base_url}/activity/complete",
            json=_as_payload(request),
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = CompleteActivityResponse.model_validate(resp.json())
        logger.info(
            "agent-event-cache complete_activity succeeded",
            extra={
                "provider": str(request.provider),
                "channel_id": request.channel_id,
                "status": result.status,
            },
        )
        return result

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retriable),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def download_file(
        self,
        event_id: str,
        file_id: str,
        timeout: int = 120,
    ) -> requests.Response:
        """GET /files/download — download a file attachment by event and file ID.

        Returns the raw ``requests.Response`` so callers can stream bytes,
        inspect ``Content-Type``, or save to disk.  On a 503 the retry
        decorator handles the back-off.

        Raises:
            requests.HTTPError: On 4xx/5xx responses (after retries for 5xx).
        """
        resp = requests.get(
            f"{self._base_url}/files/download",
            params={"event_id": event_id, "file_id": file_id},
            headers=self._auth_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        logger.info(
            "agent-event-cache download_file succeeded",
            extra={
                "event_id": event_id,
                "file_id": file_id,
                "size_bytes": len(resp.content),
                "content_type": resp.headers.get("Content-Type"),
            },
        )
        return resp


# ---------------------------------------------------------------------------
# Module-level file download helper
#
# Thin wrapper around AgentEventCacheClient.download_file() for standalone
# scripts (transcription, audio/vision tools) that don't have a pre-built
# client instance.  Constructs a throwaway client and delegates.
# ---------------------------------------------------------------------------


def download_file(event_id: str, file_id: str, timeout: int = 120) -> requests.Response:
    """Download a file attachment by *event_id* and *file_id*.

    Convenience entry point for CLI scripts and agent-callable utilities.
    Internally creates an :class:`AgentEventCacheClient` and calls its
    :meth:`download_file` method, which carries the retry policy and auth.

    Returns:
        A ``requests.Response`` whose ``.content`` contains the file bytes.

    Raises:
        requests.HTTPError: On 4xx/5xx after retries.
        RuntimeError: If auth or base-URL resolution fails.
    """
    return AgentEventCacheClient().download_file(event_id, file_id, timeout=timeout)
