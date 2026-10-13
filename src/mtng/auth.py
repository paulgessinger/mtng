"""Host-scoped GitLab credentials; GitHub keeps its existing keychain entry."""

import os

import aiohttp
import keyring
from keyring.errors import KeyringError
import typer

from mtng.forge.gitlab import GitLabForge, GitLabError
from mtng.spec import normalize_host


def keyring_username(host: str) -> str:
    return f"gitlab:{normalize_host(host)}:token"


def resolve_gitlab_token(
    token: str | None, host: str, *, allow_env=True
) -> tuple[str, str]:
    if token and token.strip():
        return token.strip(), "--token"
    if allow_env and (value := os.environ.get("GITLAB_TOKEN", "").strip()):
        return value, "GITLAB_TOKEN"
    try:
        value = keyring.get_password("mtng", keyring_username(host))
    except KeyringError as error:
        raise typer.BadParameter(
            f"Unable to access the system keychain: {error}"
        ) from error
    if value and value.strip():
        return value.strip(), "system keychain"
    raise typer.BadParameter(
        f"No GitLab token for {host}. Run `mtng auth login --host {host}`"
        + (", or set GITLAB_TOKEN." if allow_env else ".")
    )


async def validate_gitlab_token(token: str, host: str) -> str:
    async with aiohttp.ClientSession() as session:
        # Validation deliberately bypasses the cache.
        try:
            data, _ = await GitLabForge(session, host, token).request("/user")
        except (GitLabError, aiohttp.ClientError, TimeoutError) as error:
            raise typer.BadParameter(
                f"GitLab token validation failed: {error}"
            ) from error
        return data["username"]
