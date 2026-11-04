"""Shared token resolution and keychain storage with provider-specific validation."""

from dataclasses import dataclass
import os
from typing import Literal
from urllib.parse import urlsplit

import aiohttp
import gidgethub
from gidgethub.aiohttp import GitHubAPI
import keyring
from keyring.errors import KeyringError
import typer

from mtng.forge.http import HttpTransport, RateLimitError
from mtng.forge.gitlab import GitLabForge, GitLabError
from mtng.spec import normalize_host

KEYRING_SERVICE = "mtng"


@dataclass(frozen=True)
class AuthTarget:
    forge: Literal["github", "gitlab"]
    host: str

    def __post_init__(self):
        object.__setattr__(self, "host", normalize_host(self.host))
        if self.forge not in ("github", "gitlab"):
            raise ValueError(f"Unsupported forge: {self.forge}")
        if self.forge == "github" and self.host != "github.com":
            raise ValueError("GitHub Enterprise is not supported")

    @classmethod
    def from_host(cls, host: str) -> "AuthTarget":
        # Preserve the CLI's existing --host convention.
        host = normalize_host(host)
        return cls("github" if host == "github.com" else "gitlab", host)

    @property
    def label(self) -> str:
        return "GitHub" if self.forge == "github" else f"GitLab ({self.host})"

    @property
    def env_var(self) -> str:
        return "GH_TOKEN" if self.forge == "github" else "GITLAB_TOKEN"

    @property
    def keyring_username(self) -> str:
        # Keep existing credentials usable without migration or another login.
        return "github-token" if self.forge == "github" else f"gitlab:{self.host}:token"


def resolve_token(
    token: str | None, target: AuthTarget, *, allow_env: bool = True
) -> tuple[str, str]:
    if token and token.strip():
        return token.strip(), "--token"
    if allow_env and (value := os.environ.get(target.env_var, "").strip()):
        return value, target.env_var
    try:
        value = keyring.get_password(KEYRING_SERVICE, target.keyring_username)
    except KeyringError as error:
        raise typer.BadParameter(
            f"Unable to access the system keychain: {error}", param_hint="--token"
        ) from error
    if value and value.strip():
        return value.strip(), "system keychain"
    raise typer.BadParameter(
        f"No {target.label} token provided. Use --token or run "
        f"`mtng auth login --host {target.host}`"
        + (f", or set {target.env_var}." if allow_env else "."),
        param_hint="--token",
    )


def store_token(token: str, target: AuthTarget) -> None:
    token = token.strip()
    if not token:
        raise typer.BadParameter("Token cannot be empty", param_hint="--token")
    try:
        keyring.set_password(KEYRING_SERVICE, target.keyring_username, token)
    except KeyringError as error:
        raise typer.BadParameter(
            f"Unable to store token in the system keychain: {error}",
            param_hint="--token",
        ) from error


def llm_keyring_username(base_url: str, identity: str | None = None) -> str:
    """Keychain username for an OpenAI-compatible gateway, keyed per host.

    An optional identity distinguishes several keys for the same gateway,
    for example one per LiteLLM team.
    """

    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("base_url must be an http(s) URL")
    port = parsed.port  # Also validates the port range.
    host = parsed.hostname + (f":{port}" if port and port not in (80, 443) else "")
    parts = ["llm", host]
    if identity:
        parts.append(identity)
    parts.append("token")
    return ":".join(parts)


def resolve_llm_api_key(
    api_key_env: str, base_url: str, identity: str | None = None
) -> str | None:
    """Resolve a gateway API key from the environment, then the keychain.

    The environment variable named by ``api_key_env`` wins, matching forge
    token resolution. Otherwise the keychain entry for the gateway (and
    optional identity, e.g. a LiteLLM team) is used. A broken keychain is
    treated like a missing entry so unauthenticated local gateways keep
    working; requests then fail visibly if the gateway actually requires
    a key.
    """

    if value := os.environ.get(api_key_env, "").strip():
        return value
    try:
        value = keyring.get_password(
            KEYRING_SERVICE, llm_keyring_username(base_url, identity)
        )
    except KeyringError:
        return None
    return value.strip() if value and value.strip() else None


def format_request_error(error: Exception) -> str:
    status_code = getattr(error, "status_code", None)
    detail = str(error).strip() or "Unknown error"
    return detail if status_code is None else f"{status_code} {detail}"


async def validate_token(token: str, target: AuthTarget) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            if target.forge == "github":
                data = await GitHubAPI(session, __name__, oauth_token=token).getitem(
                    "/user"
                )
                return data["login"]
            # Validation deliberately bypasses the cache.
            data, _ = await GitLabForge(
                session,
                target.host,
                token,
                transport=HttpTransport(session, enabled=False),
            ).request("/user")
            return data["username"]
    except (
        gidgethub.GitHubException,
        GitLabError,
        RateLimitError,
        aiohttp.ClientError,
        TimeoutError,
    ) as error:
        raise typer.BadParameter(
            f"{target.label} token validation failed: {format_request_error(error)}",
            param_hint="--token",
        ) from error
