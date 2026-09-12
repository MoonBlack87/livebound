"""Typed failures from the CivitAI surfaces.

The distinction that matters most is :class:`LeadTimeExpired`. CivitAI does not
reject a publishedAt that is too close to now - ``updatePostHandler`` silently
rewrites it to the current time and the post goes public immediately. This app
therefore raises that error *itself*, before the call, and treats it as a normal
recoverable outcome rather than a server error.
"""

from __future__ import annotations


class CivitaiError(RuntimeError):
    """Base class. ``detail`` carries the server's message when there was one."""

    def __init__(self, message: str, *, detail: str | None = None, status: int | None = None):
        super().__init__(message)
        self.detail = detail
        self.status = status


class AuthError(CivitaiError):
    """401/403 - missing, wrong or insufficiently scoped credential."""


class OAuthRegistrationRejected(AuthError):
    """The shipped application registration cannot grant this connection."""

    code = "oauth_registration_rejected"

    def __init__(self):
        super().__init__(
            "The Livebound OAuth registration was rejected by CivitAI. "
            "Update Livebound or report the connection problem."
        )


class NotFound(CivitaiError):
    """The post/image does not exist (any more). Drives the `remote_missing` state."""


class RateLimited(CivitaiError):
    """429, or our own budget check. A batch pauses on this instead of failing."""

    def __init__(self, message: str, *, retry_after: int | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class AccountNotReady(CivitaiError):
    """Muted or not onboarded. Every guarded write would fail, so we stop early."""


class LeadTimeExpired(CivitaiError):
    """The 60-minute window closed while we were uploading.

    Raised locally, never by the server. The post stays an unpublished draft.
    """


class UploadUrlRejected(CivitaiError):
    """A presigned upload destination does not use HTTPS."""

    code = "upload_url_requires_https"

    def __init__(self):
        super().__init__(
            "The upload requires an HTTPS URL. No file was sent to this destination. "
            "Retry the upload or report the connection problem."
        )


class TransportError(CivitaiError):
    """Network-level failure: timeout, DNS, connection reset.

    ``procedure`` is what was being attempted. It is kept apart from the message
    so the frontend can put it into a translated sentence rather than showing
    the English one.
    """

    def __init__(self, message: str, *, procedure: str | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.procedure = procedure
