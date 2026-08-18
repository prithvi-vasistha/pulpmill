"""Instagram Reels publishing via the Instagram Graph API.

**Meta does not accept an upload.** The publishing flow hands Instagram a URL
and Instagram fetches the file itself:

    1. POST /{ig-user-id}/media with media_type=REELS and video_url -> container
    2. poll GET /{container-id}?fields=status_code until FINISHED
    3. POST /{ig-user-id}/media_publish with creation_id

That first step is the architectural consequence worth knowing about: publishing
to Instagram requires the rendered file to be reachable over HTTPS from Meta's
servers. A local-first pipeline therefore needs somewhere to host the file, and
`options.public_base_url` is where that gets configured. `health()` refuses
rather than letting the run fail three steps in.

Publishing also requires `instagram_content_publish`, which requires App Review
of a Business or Creator account -- an application with a human reviewer, not a
setting.

Secrets (see docs/CREDENTIALS.md):
    PULPMILL_INSTAGRAM_ACCESS_TOKEN
    PULPMILL_INSTAGRAM_USER_ID
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from pulpmill.domain.errors import PublishError, PublisherUnavailableError, PublishRejectedError
from pulpmill.domain.publishing import (
    PublisherHealth,
    PublishRequest,
    PublishResult,
    PublishState,
)
from pulpmill.infrastructure.http import HttpClient
from pulpmill.infrastructure.logging import get_logger
from pulpmill.publishing.base import PublisherContext, register_publisher

PLATFORM = "instagram"

GRAPH_BASE_URL = "https://graph.instagram.com/v23.0"

#: Meta transcodes before the container is publishable. Reels of this length
#: usually finish inside a minute; the ceiling stops a stuck container from
#: blocking a worker indefinitely.
CONTAINER_POLL_SECONDS = 5.0
CONTAINER_TIMEOUT_SECONDS = 300.0

_REQUIRED_SECRETS = ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID")

_REMEDIATION = (
    "convert the account to Business or Creator, create a Meta app, request the "
    "instagram_content_publish permission and pass App Review, then set "
    "publishing.targets.instagram.options.public_base_url to somewhere the rendered "
    "file is reachable over HTTPS. See docs/PUBLISHING.md."
)

_log = get_logger("publishing.instagram")


class InstagramPublisher:
    """Container-then-publish flow for Reels."""

    def __init__(self, context: PublisherContext) -> None:
        self._context = context
        self._target = context.target
        self._secrets = context.secrets
        self._client: HttpClient | None = None

    @property
    def name(self) -> str:
        return self._context.name

    @property
    def platform(self) -> str:
        return PLATFORM

    def health(self) -> PublisherHealth:
        if not self._target.enabled:
            return PublisherHealth(False, "target is disabled in configuration")

        missing = [key for key in _REQUIRED_SECRETS if not self._secrets.has(key)]
        if missing:
            return PublisherHealth(
                available=False,
                detail=f"missing credentials: {', '.join(missing)}",
                remediation=_REMEDIATION,
            )
        if not self._public_base_url():
            return PublisherHealth(
                available=False,
                detail="options.public_base_url is not set, and Meta fetches the file by URL",
                remediation=_REMEDIATION,
            )
        return PublisherHealth(
            available=True,
            detail="credentials and public base URL present",
            remediation="the file must remain reachable until Meta finishes fetching it",
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        # Credentials are required to transmit, not to rehearse -- see the note
        # in the YouTube adapter.
        if not request.dry_run:
            health = self.health()
            if not health.available:
                raise PublisherUnavailableError(
                    health.detail, target=self.name, remediation=health.remediation
                )

        video_url = f"{self._public_base_url().rstrip('/')}/{quote(request.video_path.name)}"
        params = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": self._caption(request),
            "share_to_feed": "true",
        }

        if request.dry_run:
            _log.info(
                "instagram_dry_run",
                target=self.name,
                story_id=request.story_id,
                video_url=video_url,
            )
            return PublishResult(
                target=self.name,
                state=PublishState.SKIPPED,
                detail="dry run: container request built, nothing transmitted",
                response={"container_params": {**params, "caption": "<omitted>"}},
            )

        container_id = self._create_container(params)
        self._await_container(container_id)
        media_id = self._publish_container(container_id)

        _log.info(
            "instagram_published", target=self.name, story_id=request.story_id, media_id=media_id
        )
        return PublishResult(
            target=self.name,
            state=PublishState.PUBLISHED,
            remote_id=media_id,
            remote_url=f"https://www.instagram.com/reel/{media_id}/",
            detail="published",
            response={"container_id": container_id, "media_id": media_id},
        )

    # --- API steps -----------------------------------------------------------

    def _create_container(self, params: dict[str, str]) -> str:
        payload = self._post(f"/{self._user_id()}/media", params)
        container_id = str(payload.get("id") or "")
        if not container_id:
            raise PublishRejectedError(
                "instagram did not return a media container id", target=self.name
            )
        return container_id

    def _await_container(self, container_id: str) -> None:
        """Poll until Meta has finished fetching and transcoding the file."""
        deadline = time.monotonic() + CONTAINER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            payload = self._get(f"/{container_id}", {"fields": "status_code,status"})
            status = str(payload.get("status_code") or "")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise PublishRejectedError(
                    "instagram could not process the video",
                    target=self.name,
                    detail=str(payload.get("status") or "no detail supplied"),
                )
            self._context.clock.sleep(CONTAINER_POLL_SECONDS)

        raise PublishError(
            "instagram container did not become ready in time",
            target=self.name,
            container_id=container_id,
            timeout_seconds=CONTAINER_TIMEOUT_SECONDS,
        )

    def _publish_container(self, container_id: str) -> str:
        payload = self._post(f"/{self._user_id()}/media_publish", {"creation_id": container_id})
        media_id = str(payload.get("id") or "")
        if not media_id:
            raise PublishRejectedError(
                "instagram did not return a media id after publish", target=self.name
            )
        return media_id

    # --- plumbing ------------------------------------------------------------

    def _caption(self, request: PublishRequest) -> str:
        metadata = request.metadata
        hashtags = " ".join(f"#{tag}" for tag in metadata.tags)
        body = f"{metadata.title}\n\n{metadata.description}"
        return f"{body}\n\n{hashtags}".strip() if hashtags else body

    def _user_id(self) -> str:
        return self._secrets.require("INSTAGRAM_USER_ID")

    def _public_base_url(self) -> str:
        return str(self._target.options.get("public_base_url") or "").strip()

    def _post(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        # The token travels as a header rather than a query parameter so it
        # cannot end up in a URL that gets logged.
        response = self._http().request(
            "POST",
            f"{GRAPH_BASE_URL}{path}",
            data=params,
            headers=self._auth_headers(),
            expected_status=(200,),
        )
        return _as_dict(response.json(), target=self.name)

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = self._http().request(
            "GET",
            f"{GRAPH_BASE_URL}{path}",
            params=params,
            headers=self._auth_headers(),
            expected_status=(200,),
        )
        return _as_dict(response.json(), target=self.name)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secrets.require('INSTAGRAM_ACCESS_TOKEN')}"}

    def _http(self) -> HttpClient:
        if self._client is None:
            self._client = HttpClient(
                name=f"publish.{self.name}",
                config=self._context.config.http,
                rate_limit=self._target.rate_limit,
                clock=self._context.clock,
                transport=self._context.transport,  # type: ignore[arg-type]
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _as_dict(payload: Any, *, target: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PublishError("instagram returned an unexpected payload shape", target=target)
    return payload


register_publisher(PLATFORM, InstagramPublisher)
