"""Throwaway scratch file - deliberately bad code to verify the new advisory
Claude Code PR security review workflow (ADR-026) actually posts a finding.

This PR is never meant to merge - it exists only to confirm the Action
fires and comments. Will be closed and the branch deleted right after.
"""
import aiohttp


async def fetch_whatever_url_the_caller_gives(url: str) -> str:
    # Deliberately bad: no SSRF guard at all (no assert_host_is_safe, no
    # pinned resolver, no scheme check) on a caller-supplied URL - exactly
    # the class of bug app/shared/web/ exists to prevent elsewhere in this
    # codebase.
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.text()
