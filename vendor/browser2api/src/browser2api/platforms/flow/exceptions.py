"""Flow-specific authentication exceptions."""


class FlowAuthError(Exception):
    """Base class for Flow authentication failures."""


class AccountDisabledError(FlowAuthError):
    """Google account is disabled / banned. Maps to setup_puppeteer.js exit 11."""

    exit_code = 11


class LoginFailedError(FlowAuthError):
    """Generic login failure (bad credentials, unexpected screen, etc.)."""


class TwoFactorRequiredError(FlowAuthError):
    """2FA prompt appeared but ``otp_secret`` is not configured for this account."""


class CaptchaRequiredError(FlowAuthError):
    """Google asked for CAPTCHA — automated solve is not implemented."""


class SessionTokenNotFoundError(FlowAuthError):
    """Login appeared to succeed but __Secure-next-auth.session-token cookie is missing."""
