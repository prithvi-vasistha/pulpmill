"""YouTube Shorts publishing via the Data API v3.

Implemented against the documented resumable upload protocol, using the HTTP
client the rest of the pipeline uses -- so it inherits the retry policy, the
rate limiter and the credential-redacting logger rather than reimplementing
them. No Google SDK dependency.

**The quota is the real constraint, not the code.** `videos.insert` costs 1600
units against a default daily allowance of 10 000. That is six uploads a day per
project, regardless of how well anything here works. Reaching a meaningful
publishing rate requires an approved quota increase, which is an application
with a review, not a setting. `health()` says so rather than letting the pipeline
discover it at upload 7.

**Uploads land private until the project is verified.** An unverified OAuth
project can upload, but YouTube locks the result to private and ignores the
requested privacy status. That is a platform rule, not a bug here, and is
reported in the result rather than silently accepted as success.

Secrets (see docs/CREDENTIALS.md):
    PULPMILL_YOUTUBE_CLIENT_ID
    PULPMILL_YOUTUBE_CLIENT_SECRET
    PULPMILL_YOUTUBE_REFRESH_TOKEN
"""

from __future__ import annotations

from typing import Any

from pulpmill.domain.errors import PublishError, PublisherUnavailableError, PublishRejectedError
from pulpmill.domain.publishing import (
    PublisherHealth,
    PublishRequest,
    PublishResult,
    PublishState,
    VideoMetadata,
)
from pulpmill.infrastructure.clock import utc_now
from pulpmill.infrastructure.http import HttpClient
from pulpmill.infrastructure.logging import get_logger
from pulpmill.publishing.base import PublisherContext, register_publisher

PLATFORM = "youtube"

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

#: Quota cost of one `videos.update`. Cheap next to the 1600 an upload spends,
#: which is what makes relinking a back catalogue affordable.
UPDATE_QUOTA_COST = 50

#: Quota cost of one `videos.insert`, against a default 10 000/day allowance.
UPLOAD_QUOTA_COST = 1600
DEFAULT_DAILY_QUOTA = 10_000

_REQUIRED_SECRETS = (
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
)

_REMEDIATION = (
    "create a Google Cloud project, enable the YouTube Data API v3, configure an "
    "OAuth consent screen and PUBLISH it (tokens from a 'testing' app expire after "
    "7 days), then complete the OAuth flow once to obtain a refresh token. "
    "See docs/PUBLISHING.md."
)

_log = get_logger("publishing.youtube")


class YouTubePublisher:
    """Resumable upload to YouTube, one video per call."""

    def __init__(self, context: PublisherContext) -> None:
        self._context = context
        self._target = context.target
        self._secrets = context.secrets
        self._client: HttpClient | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def name(self) -> str:
        return self._context.name

    @property
    def platform(self) -> str:
        return PLATFORM

    # --- health --------------------------------------------------------------

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

        uploads_per_day = DEFAULT_DAILY_QUOTA // UPLOAD_QUOTA_COST
        return PublisherHealth(
            available=True,
            detail=(
                f"credentials present; default quota permits ~{uploads_per_day} uploads/day "
                f"({UPLOAD_QUOTA_COST} units each)"
            ),
            remediation=(
                "request a YouTube API quota increase before raising daily_limit above "
                f"{uploads_per_day}; uploads stay private until the OAuth project is verified"
            ),
        )

    # --- publishing ----------------------------------------------------------

    def publish(self, request: PublishRequest) -> PublishResult:
        if not request.video_path.is_file():
            raise PublishError("video file is missing", path=str(request.video_path))

        # Credentials are required to transmit, not to rehearse. A dry run is
        # exactly what you want *before* the approval process is finished, so
        # it builds and validates the whole request without them.
        if not request.dry_run:
            health = self.health()
            if not health.available:
                raise PublisherUnavailableError(
                    health.detail, target=self.name, remediation=health.remediation
                )

        body = self._body(request)

        if request.dry_run:
            _log.info(
                "youtube_dry_run",
                target=self.name,
                story_id=request.story_id,
                title=request.metadata.title,
                bytes=request.video_path.stat().st_size,
            )
            return PublishResult(
                target=self.name,
                state=PublishState.SKIPPED,
                detail="dry run: request built and validated, nothing transmitted",
                response={"request_body": body},
            )

        session_url = self._begin_session(body, request)
        payload = self._upload(session_url, request)

        video_id = str(payload.get("id") or "")
        if not video_id:
            raise PublishRejectedError(
                "youtube accepted the upload but returned no video id", target=self.name
            )

        actual_privacy = str((payload.get("status") or {}).get("privacyStatus") or "unknown")
        detail = "published"
        if actual_privacy != request.metadata.privacy:
            # Expected on an unverified project. Reported, never papered over.
            detail = (
                f"uploaded, but YouTube set privacy to {actual_privacy!r} rather than "
                f"{request.metadata.privacy!r} (usual cause: the API project is unverified)"
            )

        _log.info(
            "youtube_published",
            target=self.name,
            story_id=request.story_id,
            video_id=video_id,
            privacy=actual_privacy,
        )
        return PublishResult(
            target=self.name,
            state=PublishState.PUBLISHED,
            remote_id=video_id,
            remote_url=f"https://www.youtube.com/shorts/{video_id}",
            detail=detail,
            response={"id": video_id, "privacyStatus": actual_privacy},
        )

    def _body(self, request: PublishRequest) -> dict[str, Any]:
        metadata = request.metadata
        options = self._target.options
        return {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": list(metadata.tags),
                "categoryId": str(options.get("category_id", "24")),
            },
            "status": {
                "privacyStatus": metadata.privacy,
                "selfDeclaredMadeForKids": bool(options.get("made_for_kids", False)),
                "license": "youtube",
            },
        }

    def _begin_session(self, body: dict[str, Any], request: PublishRequest) -> str:
        size = request.video_path.stat().st_size
        response = self._http().request(
            "POST",
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {self._token()}",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json_body=body,
            expected_status=(200,),
        )
        location = response.headers.get("location")
        if not location:
            raise PublishError("youtube did not return a resumable upload URL", target=self.name)
        return str(location)

    def _upload(self, session_url: str, request: PublishRequest) -> dict[str, Any]:
        # Read fully rather than streaming: a retry must be able to resend the
        # same body, and these files are tens of megabytes, not gigabytes.
        content = request.video_path.read_bytes()
        response = self._http().request(
            "PUT",
            session_url,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "video/mp4",
                "Content-Length": str(len(content)),
            },
            content=content,
            expected_status=(200, 201),
        )
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PublishError(
                "youtube returned a non-JSON upload response", target=self.name
            ) from exc
        return payload

    # --- auth ----------------------------------------------------------------

    def _token(self) -> str:
        """Exchange the refresh token for an access token, cached until expiry."""
        now = utc_now().timestamp()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        response = self._http().request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self._secrets.require("YOUTUBE_CLIENT_ID"),
                "client_secret": self._secrets.require("YOUTUBE_CLIENT_SECRET"),
                "refresh_token": self._secrets.require("YOUTUBE_REFRESH_TOKEN"),
                "grant_type": "refresh_token",
            },
            expected_status=(200,),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublisherUnavailableError(
                "google token endpoint returned a non-JSON body", target=self.name
            ) from exc

        token = payload.get("access_token")
        if not token:
            # Never log the payload: it is a credential exchange.
            raise PublisherUnavailableError(
                "google did not return an access token",
                target=self.name,
                remediation="the refresh token is likely expired or revoked; re-run the "
                "OAuth flow, and publish the consent screen so tokens stop expiring weekly",
            )
        self._access_token = str(token)
        self._token_expires_at = now + float(payload.get("expires_in", 3600)) - 60
        _log.info("youtube_token_acquired", target=self.name)
        return self._access_token

    def update_metadata(self, remote_id: str, metadata: VideoMetadata) -> bool:
        """Rewrite a published video's snippet.

        `videos.update` costs 50 quota units against the same daily allowance an
        upload spends 1600 of, so relinking a back catalogue is cheap next to
        publishing it. The whole snippet must be sent -- the API replaces rather
        than merges -- which is why the category id is resent unchanged.
        """
        health = self.health()
        if not health.available:
            raise PublisherUnavailableError(
                health.detail, target=self.name, remediation=health.remediation
            )

        body = {
            "id": remote_id,
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": list(metadata.tags),
                "categoryId": str(self._target.options.get("category_id", "24")),
            },
        }
        self._http().request(
            "PUT",
            VIDEOS_URL,
            params={"part": "snippet"},
            headers={"Authorization": f"Bearer {self._token()}"},
            json_body=body,
            expected_status=(200,),
        )
        _log.info("youtube_metadata_updated", target=self.name, remote_id=remote_id)
        return True

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


register_publisher(PLATFORM, YouTubePublisher)
