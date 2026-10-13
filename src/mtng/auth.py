"""Shared token resolution and keychain storage with provider-specific validation."""

from dataclasses import dataclass
import os
from typing import Literal

import aiohttp
import gidgethub
from gidgethub.aiohttp import GitHubAPI
import keyring
from keyring.errors import KeyringError
import typer

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
            data, _ = await GitLabForge(session, target.host, token).request("/user")
            return data["username"]
    except (
        gidgethub.GitHubException,
        GitLabError,
        aiohttp.ClientError,
        TimeoutError,
    ) as error:
        raise typer.BadParameter(
            f"{target.label} token validation failed: {format_request_error(error)}",
            param_hint="--token",
        ) from error
