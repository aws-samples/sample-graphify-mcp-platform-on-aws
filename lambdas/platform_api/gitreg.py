"""Git-host helpers for in-Lambda repo registration.

Ported from scripts/common.py — keep the two in sync (make_repo_id especially:
it must match the webhook Lambda and the CLI or registrations collide apart).
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
PROVIDER_AUTH_USERS = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth"}


def detect_provider(git_url: str) -> str:
    host = urllib.parse.urlparse(git_url).netloc.lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "gitlab.com" or "gitlab" in host:
        return "gitlab"
    if host == "bitbucket.org":
        return "bitbucket"
    return "generic"


def http_get(url: str, headers: dict | None = None, timeout: int = 10) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "graphify-platform", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def github_repo_info(git_url: str, token: str = "") -> dict:
    m = GITHUB_RE.search(git_url)
    if not m:
        raise ValueError(f"not a GitHub URL: {git_url}")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = http_get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}", headers)
    if status != 200:
        raise RuntimeError(f"GitHub API {status} for {m.group(1)}/{m.group(2)}")
    return json.loads(body)


def github_head_sha(git_url: str, ref: str, token: str = "") -> str:
    m = GITHUB_RE.search(git_url)
    headers = {"Accept": "application/vnd.github.sha", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, body = http_get(
        f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/commits/{urllib.parse.quote(ref, safe='')}",
        headers,
    )
    if status != 200:
        raise RuntimeError(f"GitHub API {status} resolving {ref}")
    return body.decode().strip()


def smart_http_refs(git_url: str, token: str = "", auth_user: str = "git") -> tuple[str | None, dict[str, str]]:
    base = git_url.rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    headers = {}
    if token:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth_user}:{token}".encode()).decode()
    status, body_b = 0, b""
    for suffix in (".git", ""):
        status, body_b = http_get(f"{base}{suffix}/info/refs?service=git-upload-pack", headers)
        if status == 200:
            break
    if status != 200:
        raise RuntimeError(f"info/refs returned {status} for {git_url}")
    body = body_b.decode("utf-8", errors="replace")
    default = None
    m = re.search(r"symref=HEAD:refs/heads/([^\x00 \n]+)", body)
    if m:
        default = m.group(1)
    refs = {mm.group(2): mm.group(1) for mm in re.finditer(r"([0-9a-f]{40})\s+(refs/[^\x00\s]+)", body)}
    return default, refs


def make_repo_id(git_url: str, ref: str) -> str:
    m = GITHUB_RE.search(git_url)
    if m:
        parts = ["github", m.group(1), m.group(2), ref]
    else:
        p = urllib.parse.urlparse(git_url)
        parts = [p.netloc] + [seg for seg in p.path.strip("/").removesuffix(".git").split("/") if seg] + [ref]
    return "__".join(re.sub(r"[^A-Za-z0-9._-]", "_", part) for part in parts)


def make_url_repo_id(url: str) -> str:
    """Registry id for a url docs source, e.g. url__hatch.pypa.io__1.18.
    Keep in sync with scripts/common.py:make_url_repo_id."""
    p = urllib.parse.urlsplit(url)
    parts = ["url", p.netloc.lower()] + [seg for seg in p.path.strip("/").split("/") if seg]
    return "__".join(re.sub(r"[^A-Za-z0-9._-]", "_", part) for part in parts)[:100]


def resolve_ref_and_sha(git_url: str, ref: str, provider: str, token: str = "") -> tuple[str, str]:
    """Return (ref, head_sha), resolving the default branch when ref is empty."""
    auth_user = PROVIDER_AUTH_USERS.get(provider, "git")
    if not ref:
        if provider == "github":
            try:
                ref = github_repo_info(git_url, token)["default_branch"]
            except Exception:
                ref, _ = smart_http_refs(git_url, token, auth_user)
        else:
            ref, _ = smart_http_refs(git_url, token, auth_user)
        if not ref:
            raise RuntimeError("could not resolve default branch; pass ref explicitly")
    head_sha = ""
    if provider == "github":
        try:
            head_sha = github_head_sha(git_url, ref, token)
        except Exception:
            pass  # unauthenticated API quota exhausts easily; info/refs is not governed by it
    if not head_sha:
        _, refs = smart_http_refs(git_url, token, auth_user)
        head_sha = refs.get(f"refs/heads/{ref}", "") or refs.get(ref, "")
    if not head_sha:
        raise RuntimeError(f"ref refs/heads/{ref} not found on remote")
    return ref, head_sha
