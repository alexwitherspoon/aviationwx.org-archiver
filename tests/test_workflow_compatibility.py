"""
Compatibility guards for GitHub Actions workflows (pinned third-party actions).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CODEQL_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "codeql.yml"
_CODEQL_USES = re.compile(
    r"github/codeql-action/(?P<step>init|analyze)@(?P<sha>[0-9a-f]{40})\s*#\s*v(?P<ver>\d+\.\d+\.\d+)"
)


def _codeql_workflow_pins() -> list[re.Match[str]]:
    text = _CODEQL_WORKFLOW.read_text(encoding="utf-8")
    return list(_CODEQL_USES.finditer(text))


def test_codeql_init_and_analyze_pins_match():
    """init and analyze must share one CodeQL Action commit (upstream requirement)."""
    matches = _codeql_workflow_pins()
    assert len(matches) == 2, (
        f"expected exactly init + analyze codeql-action pins in {_CODEQL_WORKFLOW}, "
        f"found {len(matches)}"
    )
    steps = {m.group("step") for m in matches}
    assert steps == {"analyze", "init"}
    shas = {m.group("sha") for m in matches}
    assert len(shas) == 1, f"init/analyze must share one SHA, got {shas}"
    versions = {m.group("ver") for m in matches}
    assert len(versions) == 1, f"version comments must agree, got {versions}"


def _github_get_json(url: str, timeout_s: float = 30.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _peel_codeql_tag_commit(tag_name: str) -> str:
    """
    Resolve refs/tags/vX.Y.Z on github/codeql-action to the underlying commit SHA
    (annotated tags point at a tag object, not the commit).
    """
    base = "https://api.github.com/repos/github/codeql-action"
    ref = _github_get_json(f"{base}/git/refs/tags/{tag_name}")
    obj = ref["object"]
    if obj["type"] == "commit":
        return obj["sha"]
    if obj["type"] == "tag":
        tag = _github_get_json(obj["url"])
        return tag["object"]["sha"]
    raise AssertionError(f"unexpected ref object type {obj['type']!r} for {tag_name}")


def test_codeql_pinned_sha_matches_github_release_tag():
    """
    Pinned full SHA must match the peeled official v-tag on github/codeql-action.

    In GitHub Actions this must not skip: a wrong pin breaks CodeQL and supply-chain
    review. Locally, the test is skipped when the GitHub API is unreachable.
    """
    matches = _codeql_workflow_pins()
    assert len(matches) == 2
    pinned = matches[0].group("sha").lower()
    ver = matches[0].group("ver")
    tag = f"v{ver}"

    try:
        remote = _peel_codeql_tag_commit(tag).lower()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(
                "CodeQL tag compatibility check requires GitHub API access in CI: "
                f"{type(e).__name__}: {e}"
            )
        pytest.skip(f"Skipping remote CodeQL tag check (no GitHub API): {e}")

    assert pinned == remote, (
        f"Workflow pins {pinned[:12]}… but peeled {tag} on github/codeql-action is "
        f"{remote[:12]}… — update the workflow or the version comment."
    )
