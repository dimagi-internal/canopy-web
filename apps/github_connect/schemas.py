"""Schemas for a person's GitHub connection (`/api/tokens/github…`)."""
from __future__ import annotations

from pydantic import Field

from apps.common.schemas import StrictModel


class GitHubConnectionOut(StrictModel):
    """Response for GET /api/tokens/github.

    Deliberately says nothing about the token itself — not a masked form, not a
    length, not an expiry of the access token (there is no stored access token
    to have an expiry). The only questions the UI asks are "is this deployment
    set up", "am I connected", "as whom", and "do I need to press the button
    again".
    """

    configured: bool = Field(
        description=(
            "Whether this DEPLOYMENT has GitHub App credentials at all. False on a "
            "fresh checkout and before the client secret is set, which is a real "
            "state rather than an error — the UI says 'not set up' instead of "
            "offering a button that cannot work."
        )
    )
    connected: bool = Field(description="Whether the caller has a stored grant.")
    github_login: str = Field(
        default="",
        description="The GitHub account this grant belongs to, so a user can tell "
                    "they connected the one they meant to. Display only — never "
                    "used for authorization.",
    )
    needs_reconnect: bool = Field(
        default=False,
        description=(
            "The stored grant can no longer mint an access token: the refresh token "
            "is missing, expired, or was rejected (revoked by the user, or the app's "
            "permissions changed and the install has not re-approved). One flag "
            "because all three have the same remedy."
        ),
    )
    install_url: str = Field(
        default="",
        description="Where to install the app on another account or org. Empty when "
                    "the app slug is unset, and the UI then omits the link rather "
                    "than rendering one that 404s.",
    )


class GitHubInstallationOut(StrictModel):
    """One place the caller has installed the app — the owner dropdown's source.

    `installation_id` is included for display/debugging only. Nothing
    authorizes on it: GitHub warns that a spoofed `installation_id` can be
    posted to the callback, so the server always re-reads this list with the
    user's own token rather than trusting an id from a client.
    """

    installation_id: int
    account_login: str
    account_type: str
    is_org: bool
    repository_selection: str = Field(
        default="selected",
        description='"selected" or "all", from GitHub. Answers "did I actually scope '
                    'this?" — an "all" grant reaches every repository the installing '
                    'account can see, which is what the connect advice tells people to '
                    'avoid, so the UI flags it rather than listing thousands of names.',
    )
    repositories: list[str] = Field(
        default_factory=list,
        description='Full names ("owner/repo") this installation reaches. Empty when '
                    '`repository_selection` is "all" (enumerating is pointless there) '
                    'and when the per-installation lookup failed, which degrades to no '
                    'names rather than failing the whole list.',
    )
    repository_count: int = Field(
        default=0,
        description="GitHub's reported total, which can exceed len(repositories) when "
                    "the grant spans more than one page.",
    )
