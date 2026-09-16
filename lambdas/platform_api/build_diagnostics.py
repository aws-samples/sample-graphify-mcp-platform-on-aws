"""Bounded, on-demand CodeBuild diagnostics for an already authorized source.

Never accept a log group/stream or arbitrary historical build from the caller.
Only selected metadata is returned; environment variables are used to verify
source identity, never included in the response. No credentials are resolved.
"""

import re
from datetime import datetime, timezone


class DiagnosticsError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


PAGE_SIZE = 100
MESSAGE_LIMIT = 3000
# Best-effort known credential patterns, not a guarantee that arbitrary
# application secrets printed by a build can be recognized.
SENSITIVE_NAME = (
    r"(?:[\w-]{0,64}(?:token|password|passwd|secret|api[_-]?key|"
    r"access[_-]?key|authorization|credential)[\w-]{0,64})"
)
QUOTED_VALUE = r'''"(?:\\.|[^"\\])*(?:"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)'''


def _redact_url(match):
    url = match.group()
    path, query, _ = url.partition("?")
    scheme, _, rest = path.partition("://")
    authority, slash, suffix = rest.partition("/")
    if "@" in authority:
        authority = "[REDACTED]@" + authority.rsplit("@", 1)[1]
    return scheme + "://" + authority + slash + suffix + ("?[REDACTED]" if query else "")


def _strip_terminal_controls(text):
    """Single forward scan, including unterminated/stacked OSC sequences."""
    if "\x1b" not in text:
        return text
    kept, i, end = [], 0, len(text)
    while i < end:
        if text[i] != "\x1b":
            kept.append(text[i])
            i += 1
        elif text.startswith("\x1b]", i):
            i += 2
            while i < end and text[i] != "\x07" and not text.startswith("\x1b\\", i):
                i += 1
            i += 2 if text.startswith("\x1b\\", i) else 1
        elif text.startswith("\x1b[", i):
            i += 2
            while i < end and not ("@" <= text[i] <= "~"):
                i += 1
            i += 1
        else:
            i += 1
    return "".join(kept)


def redact(text, limit=MESSAGE_LIMIT):
    text = _strip_terminal_controls(str(text or ""))
    text = re.sub(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)",
        "[REDACTED PRIVATE KEY]", text, flags=re.S,
    )
    # Suppress URL query strings wholesale (presigns, SAS tokens, PAT query
    # parameters). One URL scan avoids repeated backtracking over long URLs.
    # Apostrophes are legal in URL userinfo. Keep them until credentials have
    # been removed, but stop at the NEXT scheme so compact JSON containing
    # adjacent URLs cannot hide a second authority inside the first match.
    text = re.sub(r"https?://(?:(?!https?://)[^\s<>])*", _redact_url, text, flags=re.I)
    text = re.sub(
        r"(?im)((?:authorization|proxy-authorization|x-graphify-key|x-api-key|"
        r"x-amz-security-token|cookie|set-cookie)\s*:\s*)[^\r\n]+",
        r"\1[REDACTED]", text,
    )
    # Quoted values may contain spaces; unquoted shell assignments end at
    # whitespace. Redact before truncating, including JSON-shaped output.
    text = re.sub(
        rf"""(?i)((?<![\w-])["']?{SENSITIVE_NAME}["']?\s*[:=]\s*)(?:{QUOTED_VALUE}|(?!["'])[^\s,;}}]+)""",
        r"\1[REDACTED]", text,
    )
    text = re.sub(
        rf"""(?i)(--{SENSITIVE_NAME}\s+)(?:{QUOTED_VALUE}|(?!["'])[^\s]+)""",
        r"\1[REDACTED]", text,
    )
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED AUTH]", text)
    text = re.sub(
        r"\b(?:gfy_(?:live|test)_[A-Za-z0-9_-]+|github_pat_[A-Za-z0-9_]+|"
        r"gh[pousr]_[A-Za-z0-9]+|(?:AKIA|ASIA)[A-Z0-9]{16}|"
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])",
        "[REDACTED]", text,
    )
    # PEM bodies can span separate CloudWatch events/pages; an isolated long
    # base64 line must not defeat the BEGIN/END block redaction above.
    text = re.sub(r"(?m)^[ \t]*[A-Za-z0-9+/=]{40,}[ \t]*$", "[REDACTED OPAQUE LINE]", text)
    # Keep newline/tab for readable logs, remove terminal control characters.
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text if len(text) <= limit else text[:limit] + "\n[…truncated]"


def _error_code(exc):
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,79}", str(code)) else "ServiceUnavailable"


def _iso(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return ""


def _duration(start, end):
    if not isinstance(start, datetime):
        return None
    end = end if isinstance(end, datetime) else datetime.now(timezone.utc)
    return max(0, int(end.timestamp() - start.timestamp()))


def _hints(text):
    # Bounded proximity rules, never `.*` over the whole log page: repeated
    # model/error words in a large build log otherwise create quadratic work.
    rules = (
        ("git_auth", r"authentication failed|could not read username|repository not found|"
         r"git[^\n]{0,120}(?:403|401)|invalid username or (?:password|token)"),
        ("throttled", r"throttl|too many requests|rate.?limit|quota[^\n]{0,120}exceed"),
        ("timeout", r"timed.?out|timeout|deadline"),
        ("memory", r"out of memory|oom|killed|exit (?:status|code) 137"),
        ("document_conversion", r"document conversion|document[_ -]ocr|"
         r"ocr[^\n]{0,120}(?:fail|limit|budget|exceed)|conversion incomplete|password.required|"
         r"encrypted pdf|pdf[^\n]{0,120}(?:invalid|corrupt)"),
        ("source_download", r"nosuchkey|no such (?:file|bucket)|failed to (?:fetch|download)|"
         r"robots\.txt|crawl[^\n]{0,120}(?:fail|error)|upload[^\n]{0,120}(?:empty|missing)"),
        ("artifact_publish", r"graph missing|snapshot[^\n]{0,120}(?:fail|exceed)|"
         r"failed to (?:upload|publish)|graph[^\n]{0,120}too large|graph[^\n]{0,120}cap"),
    )
    found = [code for code, pattern in rules if re.search(pattern, text, re.I)]
    if re.search(r"bedrock|model|invoke|inference profile", text, re.I) and re.search(
        r"accessdenied|access denied|not authorized|not supported|validationexception|model access|provider_data_share",
        text, re.I,
    ):
        found.insert(0, "bedrock_access")
    return found or ["check_logs"]


def describe_build(item, codebuild, logs, project_name, *, next_token="", requested_build_id=""):
    repo_id = item["repo_id"]
    # The registry stores both; CodeBuild accepts an ID, so normalize its ARN.
    build_id = str(item.get("build_id") or item.get("build_arn") or "").split(":build/")[-1]
    if requested_build_id and requested_build_id != build_id:
        raise DiagnosticsError(409, "the source has a newer build; refresh build details")
    if next_token and (
        not requested_build_id or len(next_token) > 2048
        or re.search(r"[\x00-\x20\x7f]", next_token)
    ):
        raise DiagnosticsError(400, "invalid log cursor; refresh build details")
    source_status = str(item.get("status", ""))
    last_error = redact(item.get("last_error"), 4000) if source_status != "BUILDING" else ""
    out = {
        "repo_id": repo_id, "source_status": source_status, "last_error": last_error,
        "build": None,
        "logs": {"state": "not_started", "events": [], "next_token": None, "truncated": False},
        "hints": _hints(last_error) if last_error else [],
        "can_rebuild": source_status != "BUILDING",
    }
    if not build_id:
        return out
    if not build_id.startswith(project_name + ":") or not re.fullmatch(r"[\w:.-]+", build_id):
        raise DiagnosticsError(403, "build does not belong to this project")
    try:
        builds = codebuild.batch_get_builds(ids=[build_id]).get("builds", [])
    except Exception as exc:
        out["logs"].update(state="unavailable", error_code=_error_code(exc))
        return out
    if not builds:
        out["logs"]["state"] = "missing"
        return out
    build = builds[0]
    repo_vars = [
        v.get("value") for v in build.get("environment", {}).get("environmentVariables", [])
        if v.get("name") == "REPO_ID"
    ]
    if build.get("id") != build_id or build.get("projectName") != project_name or repo_vars != [repo_id]:
        raise DiagnosticsError(403, "build does not belong to this source")
    status = str(build.get("buildStatus", ""))
    phases = []
    failure_text = []
    for phase in build.get("phases", [])[:30]:
        messages = [
            {"code": redact(c.get("statusCode"), 100), "message": redact(c.get("message"))}
            for c in phase.get("contexts", [])[:10]
        ]
        phases.append({
            "name": redact(phase.get("phaseType"), 80),
            "status": redact(phase.get("phaseStatus"), 80),
            "duration_seconds": phase.get("durationInSeconds"),
            "messages": messages,
        })
        if phase.get("phaseStatus") in ("FAILED", "FAULT", "TIMED_OUT", "STOPPED"):
            failure_text.extend(m["message"] for m in messages)
    out["build"] = {
        "id": build_id, "status": status,
        "current_phase": str(build.get("currentPhase", "")),
        "started_at": _iso(build.get("startTime")), "ended_at": _iso(build.get("endTime")),
        "duration_seconds": _duration(build.get("startTime"), build.get("endTime")),
        "phases": phases,
    }
    running = status == "IN_PROGRESS"
    out["can_rebuild"] = not running
    if running:
        out["last_error"] = ""
        out["hints"] = []
    log_ref = build.get("logs", {})
    group, stream = log_ref.get("groupName"), log_ref.get("streamName")
    out["logs"]["state"] = "pending" if running else "missing"
    if group and group != f"/aws/codebuild/{project_name}":
        raise DiagnosticsError(403, "build log group does not belong to this project")
    if group and stream:
        args = {"logGroupName": group, "logStreamName": stream,
                "startFromHead": False, "limit": PAGE_SIZE}
        if next_token:
            args["nextToken"] = next_token
        try:
            page = logs.get_log_events(**args)
            events = []
            for event in page.get("events", [])[:PAGE_SIZE]:
                raw = str(event.get("message", ""))
                message = redact(raw)
                out["logs"]["truncated"] |= len(raw) > MESSAGE_LIMIT
                events.append({"timestamp": event.get("timestamp"), "message": message})
            older = page.get("nextBackwardToken")
            out["logs"].update(
                state="available", events=events,
                next_token=older if older and older != next_token else None,
            )
            # CloudWatch can return an empty page with an advancing cursor.
            # Retain that cursor: otherwise older error lines become unreachable.
        except Exception as exc:
            code = _error_code(exc)
            if code == "InvalidParameterException" and next_token:
                raise DiagnosticsError(400, "log cursor expired or invalid; refresh build details") from None
            out["logs"].update(
                state=("pending" if running else "missing") if code == "ResourceNotFoundException" else "unavailable",
                error_code=code,
            )
    if not running and (status != "SUCCEEDED" or source_status in ("FAILED", "TOO_LARGE")):
        text = "\n".join([status, out["last_error"], *failure_text,
                          *(e["message"] for e in out["logs"]["events"])])
        out["hints"] = _hints(text)
    return out
