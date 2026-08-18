"""TikTok publishing via the Content Posting API.

Three steps, all documented and all implemented here:

    1. POST /v2/post/publish/video/init/ with post_info and a FILE_UPLOAD
       source -> publish_id plus a signed upload URL
    2. PUT the bytes to that URL with a Content-Range header
    3. poll /v2/post/publish/status/fetch/ until the post leaves PROCESSING

**Unaudited apps can only post privately.** TikTok restricts the Content Posting
API to `SELF_ONLY` visibility until the app passes an audit, and it enforces that
server-side regardless of what `privacy_level` the request asks for. That is
reported in the result rather than being mistaken for a successful public post.

Secrets (see docs/CREDENTIALS.md):
    PULPMILL_TIKTOK_ACCESS_TOKEN
"""

from __future__ import annotations

import time
from typing import Any

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

PLATFORM = "tiktok"

API_BASE_URL = "https://open.tiktokapis.com/v2"

STATUS_POLL_SECONDS = 5.0
STATUS_TIMEOUT_SECONDS = 420.0

#: Visibility values the API accepts. `SELF_ONLY` is the only one an unaudited
#: app can actually achieve, whatever it asks for.
_PRIVACY = {
    "private": "SELF_ONLY",
    "unlisted": "MUTUAL_FOLLOW_FRIENDS",
    "public": "PUBLIC_TO_EVERYONE",
}

_REMEDIATION = (
    "register a TikTok developer app, add the Content Posting API product, complete "
    "the OAuth flow for video.publish scope, and pass the content-posting audit "
    "before expecting anything other than SELF_ONLY. See docs/PUBLISHING.md."
)

_log = get_logger("publishing.tiktok")


class TikTokPublisher:
    """Init, upload, poll."""

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
        if not self._secrets.has("TIKTOK_ACCESS_TOKEN"):
            return PublisherHealth(
                available=False,
                detail="missing credentials: TIKTOK_ACCESS_TOKEN",
                remediation=_REMEDIATION,
            )
        return PublisherHealth(
            available=True,
            detail="access token present",
            remediation=(
                "unaudited apps are forced to SELF_ONLY visibility regardless of the "
                "requested privacy level"
            ),
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
        if not request.video_path.is_file():
            raise PublishError("video file is missing", path=str(request.video_path))

        size = request.video_path.stat().st_size
        body = {
            "post_info": {
                "title": request.metadata.title,
                "privacy_level": _PRIVACY[request.metadata.privacy],
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                # One chunk: these files are tens of megabytes, well inside the
                # single-chunk limit, and chunking would add a resume protocol
                # for no benefit at this size.
                "chunk_size": size,
                "total_chunk_count": 1,
            },
        }

        if request.dry_run:
            _log.info("tiktok_dry_run", target=self.name, story_id=request.story_id, bytes=size)
            return PublishResult(
                target=self.name,
                state=PublishState.SKIPPED,
                detail="dry run: init payload built, nothing transmitted",
                response={"init_body": body},
            )

        publish_id, upload_url = self._init(body)
        self._upload(upload_url, request, size)
        status = self._await_status(publish_id)

        detail = "published"
        if request.metadata.privacy != "private":
            detail = (
                f"uploaded; requested {request.metadata.privacy!r} but an unaudited app "
                "is restricted to SELF_ONLY by TikTok"
            )

        _log.info(
            "tiktok_published", target=self.name, story_id=request.story_id, publish_id=publish_id
        )
        return PublishResult(
            target=self.name,
            state=PublishState.PUBLISHED,
            remote_id=publish_id,
            remote_url=None,
            detail=detail,
            response={"publish_id": publish_id, "status": status},
        )

    # --- API steps -----------------------------------------------------------

    def _init(self, body: dict[str, Any]) -> tuple[str, str]:
        payload = self._post("/post/publish/video/init/", body)
        data = payload.get("data") or {}
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            raise PublishRejectedError(
                "tiktok did not return an upload session",
                target=self.name,
                detail=str(payload.get("error") or "no detail supplied"),
            )
        return publish_id, upload_url

    def _upload(self, upload_url: str, request: PublishRequest, size: int) -> None:
        content = request.video_path.read_bytes()
        self._http().request(
            "PUT",
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            },
            content=content,
            expected_status=(200, 201, 204),
        )

    def _await_status(self, publish_id: str) -> str:
        deadline = time.monotonic() + STATUS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            payload = self._post("/post/publish/status/fetch/", {"publish_id": publish_id})
            data = payload.get("data") or {}
            status = str(data.get("status") or "")
            if status in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}:
                return status
            if status == "FAILED":
                raise PublishRejectedError(
                    "tiktok rejected the upload",
                    target=self.name,
                    detail=str(data.get("fail_reason") or "no reason supplied"),
                )
            self._context.clock.sleep(STATUS_POLL_SECONDS)

        raise PublishError(
            "tiktok did not finish processing in time",
            target=self.name,
            publish_id=publish_id,
            timeout_seconds=STATUS_TIMEOUT_SECONDS,
        )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._http().request(
            "POST",
            f"{API_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {self._secrets.require('TIKTOK_ACCESS_TOKEN')}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json_body=body,
            expected_status=(200,),
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise PublishError("tiktok returned an unexpected payload shape", target=self.name)
        error = payload.get("error") or {}
        code = str(error.get("code") or "ok")
        if code not in {"ok", ""}:
            raise PublishRejectedError(
                "tiktok returned an error",
                target=self.name,
                code=code,
                detail=str(error.get("message") or ""),
            )
        return payload

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


register_publisher(PLATFORM, TikTokPublisher)
