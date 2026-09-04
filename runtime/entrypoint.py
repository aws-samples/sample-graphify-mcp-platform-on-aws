"""MCP service entrypoint (ECS Fargate): keep /graphs fresh from S3, serve graphify MCP.

Handoff design (see README):
  - The build plane (CodeBuild) publishes graphs to
      s3://$GRAPH_BUCKET/repos/<repo_id>/latest/graphify-out/graph.json
  - This process downloads each graph at boot, then re-checks ETags every
    SYNC_INTERVAL_SECONDS in a daemon thread. A changed object is downloaded to
    a temp file and os.replace()d into place: graphify's server re-stats the
    file on every tool call (cache keyed on (st_mtime_ns, st_size)), so the
    swap is picked up with no restart. os.replace is mandatory — a partial
    in-place write would present a valid-looking new cache key to a
    concurrent reader.
  - Multi-repo: clients pass project_path=/graphs/<repo_id> on any tool call.

Platform code-search tools (PER-REPO runtimes only, never the hub):
  - The build plane also publishes repos/<repo_id>/latest/src.tar.gz (the
    checked-out tree minus .git). When this runtime is pinned to exactly one
    repo, the sidecar syncs that snapshot too — safe extraction via tarfile's
    "data" filter, then an atomic symlink flip — and an ASGI wrapper adds two
    JSON-RPC tools in front of graphify's own MCP server:
      search_code(pattern, ...)  full-text search over the source tree
      read_source(file, lines)   read a file range (grounded by graph nodes'
                                 source_file / source_location)
  - The wrapper speaks the stable MCP JSON-RPC wire format (the server runs
    stateless + json_response) rather than reaching into graphify/mcp
    internals, so it survives graphify upgrades unchanged.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path, PurePosixPath

import boto3
from botocore.config import Config

GRAPH_BUCKET = os.environ["GRAPH_BUCKET"]
# /tmp is the writable default: the process is non-root and cannot mkdir at /
# (the container image sets GRAPHS_ROOT=/graphs explicitly).
GRAPHS_ROOT = Path(os.environ.get("GRAPHS_ROOT", "/tmp/graphs"))
SYNC_INTERVAL = max(30, int(os.environ.get("SYNC_INTERVAL_SECONDS", "180")))
DEFAULT_REPO_ID = os.environ.get("DEFAULT_REPO_ID", "").strip()
# Per-repo runtimes set REPO_IDS to pin the served set (usually one repo);
# the hub runtime leaves it empty and discovers every repo in the bucket
# (including the merged __all__ graph the build plane maintains).
REPO_IDS = [r.strip() for r in os.environ.get("REPO_IDS", "").split(",") if r.strip()]
PORT = int(os.environ.get("PORT", "8000"))

# Optional sidecars served next to graph.json (community labels, report).
SIDE_FILES = (".graphify_labels.json", "GRAPH_REPORT.md", "manifest.json")

# Code-search tools serve exactly one repo's source tree. The runtime is told
# EXPLICITLY which repo via CODE_SEARCH_REPO — set only on per-repo runtimes,
# never the hub. Inferring it from REPO_IDS was unsafe: `-c default_repo_id`
# pins the HUB's REPO_IDS to one repo too, which would arm the tools on
# serverId "all" (the one id the authorizer exempts from grant checks).
CODE_REPO = os.environ.get("CODE_SEARCH_REPO", "").strip()
if CODE_REPO in ("", "__all__"):
    CODE_REPO = ""
SRC_MEMBER_CAP = 20 * 1024 * 1024      # skip any single file bigger than this
SRC_TOTAL_CAP = 400 * 1024 * 1024      # abort extraction past this (tar bomb)
SRC_MEMBER_COUNT_CAP = 60_000          # bound inode use (many tiny files)
SEARCH_FILE_CAP = 1 * 1024 * 1024      # don't scan files bigger than this
SEARCH_LINE_CAP = 2000                 # only match against this many chars/line
SEARCH_FILE_WALK_CAP = 20_000          # stop walking the tree past this many files
READ_FILE_CAP = 2 * 1024 * 1024        # don't read files bigger than this
READ_LINE_CAP = 500                    # truncate each returned line to this
SEARCH_TIME_BUDGET = 5.0               # seconds per search_code call
SEARCH_MAX_RESULTS_CAP = 100
READ_MAX_LINES = 400

s3 = boto3.client("s3", config=Config(retries={"max_attempts": 3, "mode": "standard"}))
_etags: dict[str, str] = {}
# ETags whose snapshot extraction failed — skip re-attempting the SAME bytes
# every 180s forever. A new build (new ETag) is retried normally.
_src_failed: dict[str, str] = {}


def log(msg: str) -> None:
    print(f"[entrypoint] {msg}", file=sys.stderr, flush=True)


def discover_repo_ids() -> list[str]:
    """List repo ids to serve: the pinned REPO_IDS, or every prefix in the bucket."""
    if REPO_IDS:
        return sorted(REPO_IDS)
    ids: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=GRAPH_BUCKET, Prefix="repos/", Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            parts = cp["Prefix"].split("/")
            if len(parts) >= 2 and parts[1]:
                ids.append(parts[1])
    return sorted(ids)


def _download_atomic(key: str, dest: Path) -> bool:
    """Download s3://GRAPH_BUCKET/key to dest atomically if its ETag changed.

    Returns True when a new version landed on disk.
    """
    try:
        head = s3.head_object(Bucket=GRAPH_BUCKET, Key=key)
    except s3.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    etag = head.get("ETag", "")
    if etag and _etags.get(key) == etag and dest.exists():
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    resp = s3.get_object(Bucket=GRAPH_BUCKET, Key=key)
    with open(tmp, "wb") as f:
        for chunk in resp["Body"].iter_chunks(1024 * 1024):
            f.write(chunk)
    # os.replace gives the file a fresh (mtime_ns, size) key -> hot reload.
    os.replace(tmp, dest)
    _etags[key] = resp.get("ETag", etag)
    return True


def sync_repo(repo_id: str) -> bool:
    """Sync one repo's graph (+sidecars/src); returns True when a NEW graph landed."""
    prefix = f"repos/{repo_id}/latest/graphify-out"
    dest_dir = GRAPHS_ROOT / repo_id / "graphify-out"
    changed = _download_atomic(f"{prefix}/graph.json", dest_dir / "graph.json")
    if changed:
        log(f"synced new graph for {repo_id}")
    for name in SIDE_FILES:
        try:
            _download_atomic(f"{prefix}/{name}", dest_dir / name)
        except Exception:  # sidecars are best-effort
            pass
    if repo_id == CODE_REPO:
        try:
            sync_src(repo_id)
        except Exception as exc:  # tools degrade to "unavailable", never kill sync
            log(f"src sync failed for {repo_id}: {exc}")
    return changed


def _extract_src_tar(tar_path: Path, dest: Path) -> None:
    """Extract a source snapshot with hostile-archive guards.

    The archive content is repo-author-controlled, so extraction uses the
    stdlib "data" filter (blocks absolute paths, .. traversal, symlinks and
    device nodes) plus size caps against tar bombs.
    """
    total = count = 0
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue  # data filter would also reject links; skip dirs early
            if member.size > SRC_MEMBER_CAP:
                continue
            count += 1
            if count > SRC_MEMBER_COUNT_CAP:
                raise RuntimeError(f"snapshot exceeds {SRC_MEMBER_COUNT_CAP} files")
            total += member.size
            if total > SRC_TOTAL_CAP:
                raise RuntimeError(f"snapshot exceeds {SRC_TOTAL_CAP} bytes uncompressed")
            tar.extract(member, path=dest, filter="data")


def sync_src(repo_id: str) -> None:
    """Sync repos/<id>/latest/src.tar.gz to a local tree, atomically.

    A directory can't be os.replace()d over, so each new snapshot extracts to
    its own .src-<n> dir and a relative symlink <repo>/src flips to it — the
    flip (os.replace on the symlink) is atomic for concurrent readers.
    """
    key = f"repos/{repo_id}/latest/src.tar.gz"
    try:
        head = s3.head_object(Bucket=GRAPH_BUCKET, Key=key)
    except s3.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return  # no snapshot published (yet) — tools report unavailable
        raise
    etag = head.get("ETag", "")
    root = GRAPHS_ROOT / repo_id
    link = root / "src"
    if etag and _etags.get(key) == etag and link.exists():
        return
    if etag and _src_failed.get(key) == etag:
        return  # this exact snapshot already failed to extract; wait for a new build

    root.mkdir(parents=True, exist_ok=True)
    tar_tmp = root / ".src.tar.gz.tmp"
    resp = s3.get_object(Bucket=GRAPH_BUCKET, Key=key)
    resp_etag = resp.get("ETag", etag)
    with open(tar_tmp, "wb") as f:
        for chunk in resp["Body"].iter_chunks(1024 * 1024):
            f.write(chunk)

    new_dir = root / f".src-{int(time.time_ns())}"
    try:
        _extract_src_tar(tar_tmp, new_dir)
    except Exception:
        shutil.rmtree(new_dir, ignore_errors=True)
        # Don't re-download/re-extract the same doomed bytes on every tick.
        _src_failed[key] = resp_etag
        raise
    finally:
        tar_tmp.unlink(missing_ok=True)

    link_tmp = root / ".src.lnk.tmp"
    link_tmp.unlink(missing_ok=True)
    os.symlink(new_dir.name, link_tmp)
    os.replace(link_tmp, link)
    _etags[key] = resp_etag
    _src_failed.pop(key, None)
    log(f"synced source snapshot for {repo_id} -> {new_dir.name}")

    # Reap superseded snapshot dirs (best-effort; a reader mid-request on the
    # old tree only risks a transient FileNotFoundError on one call).
    for old in root.glob(".src-*"):
        if old.name != new_dir.name:
            shutil.rmtree(old, ignore_errors=True)


# ---------------------------------------------------------------------------
# Platform tools: search_code / read_source (per-repo runtimes only)
# ---------------------------------------------------------------------------

_src_ondemand_lock = threading.Lock()
_src_ondemand_last = 0.0


def _ensure_src_now() -> None:
    """On-demand snapshot fetch for the code-search tools.

    The sidecar syncs src.tar.gz every SYNC_INTERVAL (180s), so a tool call
    landing right after a repo's first successful build used to answer
    "snapshot not available" for up to 3 minutes — which reads as "search
    doesn't work" to a user who just watched their build turn READY. Pull it
    immediately instead (single-flight, 10s retry backoff; runs on the tool
    worker thread, never the event loop)."""
    global _src_ondemand_last
    if not CODE_REPO:
        return
    link = GRAPHS_ROOT / CODE_REPO / "src"
    if link.exists():
        return
    with _src_ondemand_lock:
        if link.exists() or time.monotonic() - _src_ondemand_last < 10:
            return
        _src_ondemand_last = time.monotonic()
        try:
            sync_src(CODE_REPO)
            log(f"on-demand source snapshot sync for {CODE_REPO}")
        except Exception as exc:  # tools degrade to the "not yet" message
            log(f"on-demand src sync failed for {CODE_REPO}: {exc}")


_SRC_UNAVAILABLE_MSG = (
    "source snapshot not yet available on this server. It is published by the "
    "repo's first SUCCESSFUL build and appears here within moments — if the "
    "build is still running or FAILED, fix/wait and rebuild, then retry. "
    "(Hub servers never serve code-search tools.)"
)


def _src_root() -> Path | None:
    if not CODE_REPO:
        return None
    link = GRAPHS_ROOT / CODE_REPO / "src"
    if not link.exists():
        _ensure_src_now()
    if not link.exists():
        return None
    return link.resolve()


def _resolve_normalized(root: Path, rel: str) -> Path | None:
    """Resolve rel against root tolerating Unicode-normalization mismatches.

    macOS browsers upload NFD (decomposed) Korean/accented filenames; the
    snapshot preserves those bytes on Linux, but a model re-typing the path
    from a tool result emits NFC — visually identical, different bytes, so the
    literal lookup misses. Walk the path segment-wise and accept the directory
    entry whose NFC form matches the requested segment's NFC form.
    """
    cur = root
    for part in PurePosixPath(rel).parts:
        if part in ("", ".", ".."):
            return None
        child = cur / part
        if child.exists():
            cur = child
            continue
        want = unicodedata.normalize("NFC", part)
        found = None
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if unicodedata.normalize("NFC", entry.name) == want:
                        found = cur / entry.name
                        break
        except OSError:
            return None
        if found is None:
            return None
        cur = found
    if not cur.resolve().is_relative_to(root):
        return None
    return cur if cur.is_file() else None


def _tool_read_source(args: dict) -> str:
    root = _src_root()
    if root is None:
        return _SRC_UNAVAILABLE_MSG
    rel = str(args.get("file", "")).strip().lstrip("/")
    if not rel:
        return "error: 'file' is required (use source_file from a graph node)"
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        return "error: path escapes the repository root"
    if not target.is_file():
        normalized = _resolve_normalized(root, rel)
        if normalized is None:
            return f"error: no such file in snapshot: {rel}"
        target = normalized
    if target.stat().st_size > READ_FILE_CAP:
        return f"error: file exceeds the {READ_FILE_CAP // 1024 // 1024}MB read cap"
    raw = target.read_bytes()
    if b"\0" in raw[:8192]:
        return f"error: {rel} looks binary; read_source serves text files only"
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if not lines:
        return f"{rel} is empty (0 lines)"
    start = max(1, int(args.get("start_line") or 1))
    if start > len(lines):
        return f"error: start_line {start} beyond end of file ({len(lines)} lines)"
    end_arg = int(args.get("end_line") or 0)
    # end_line omitted / <=0 / before start -> a full window from start.
    if end_arg < start:
        end = start + READ_MAX_LINES - 1
    else:
        end = end_arg
    end = min(end, len(lines), start + READ_MAX_LINES - 1)
    body = "\n".join(f"{n:6d}| {lines[n - 1][:READ_LINE_CAP]}" for n in range(start, end + 1))
    return f"{rel} lines {start}-{end} of {len(lines)}:\n{body}"


def _tool_search_code(args: dict) -> str:
    root = _src_root()
    if root is None:
        return _SRC_UNAVAILABLE_MSG
    pattern = str(args.get("pattern", ""))
    if not pattern or len(pattern) > 256:
        return "error: 'pattern' is required (max 256 chars)"
    glob = str(args.get("glob", "") or "")
    use_regex = bool(args.get("regex", False))
    ignore_case = bool(args.get("ignore_case", False))
    limit = min(int(args.get("max_results") or 30), SEARCH_MAX_RESULTS_CAP)

    rx = None
    if use_regex:
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            return f"error: invalid regex: {exc}"
    needle = pattern.lower() if ignore_case else pattern

    # Compare NFC-to-NFC: snapshot paths keep the uploader's bytes (macOS
    # emits NFD Korean/accented names) while a model-typed glob is NFC.
    glob_nfc = unicodedata.normalize("NFC", glob)

    def _match(rel: str) -> bool:
        # A bare pattern (no '/') matches on the basename; a path pattern
        # matches the full relative path. PurePosixPath.match handles '**'.
        rel = unicodedata.normalize("NFC", rel)
        if "/" in glob_nfc:
            return fnmatch.fnmatch(rel, glob_nfc) or PurePosixPath(rel).match(glob_nfc)
        return fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob_nfc)

    deadline = time.monotonic() + SEARCH_TIME_BUDGET
    matches: list[str] = []
    scanned = walked = 0
    timed_out = capped_walk = False
    # Lazy walk (no whole-tree sort/materialize); deadline + walk cap bound it.
    for path in root.rglob("*"):
        if time.monotonic() > deadline:
            timed_out = True
            break
        walked += 1
        if walked > SEARCH_FILE_WALK_CAP:
            capped_walk = True
            break
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if glob and not _match(rel):
            continue
        try:
            if path.stat().st_size > SEARCH_FILE_CAP:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        if b"\0" in blob[:8192]:
            continue  # binary
        scanned += 1
        text = blob.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            # Re-check the deadline inside the per-line loop and only ever match
            # against a capped slice — a catastrophic-backtracking regex on a
            # long line otherwise runs uninterruptibly and blocks the runtime.
            if lineno % 500 == 0 and time.monotonic() > deadline:
                timed_out = True
                break
            probe = line[:SEARCH_LINE_CAP]
            if rx is not None:
                hit = rx.search(probe) is not None
            else:
                hit = needle in (probe.lower() if ignore_case else probe)
            if hit:
                matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                if len(matches) >= limit:
                    break
        if len(matches) >= limit or timed_out:
            break
    header = f"{len(matches)} match(es) for {pattern!r} in {scanned} file(s) scanned"
    if len(matches) >= limit:
        header += f" (capped at {limit} — narrow with glob or a longer pattern)"
    if timed_out:
        header += " (time budget hit — results are partial)"
    if capped_walk:
        header += f" (walk stopped at {SEARCH_FILE_WALK_CAP} paths — use glob to narrow)"
    return header + ("\n" + "\n".join(matches) if matches else "")


PLATFORM_TOOLS = {
    "search_code": _tool_search_code,
    "read_source": _tool_read_source,
}

PLATFORM_TOOL_SCHEMAS = [
    {
        "name": "search_code",
        "description": (
            "Full-text search over this repository's source code (literal substring by default; "
            "set regex=true for a regular expression). Returns file:line matches. Complements the "
            "graph tools: query_graph finds symbols and relationships; search_code finds arbitrary "
            "strings, comments, and literals the graph does not index."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "text or regex to find (max 256 chars)"},
                "glob": {"type": "string", "description": "fnmatch-style path filter, e.g. *.py or src/*"},
                "regex": {"type": "boolean", "default": False},
                "ignore_case": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "default": 30, "maximum": SEARCH_MAX_RESULTS_CAP},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_source",
        "description": (
            "Read a file (or line range) from this repository's source snapshot. Ground the "
            "location with a graph node's source_file / source_location, then read the real code. "
            f"Returns at most {READ_MAX_LINES} numbered lines per call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "path relative to the repo root"},
                "start_line": {"type": "integer", "default": 1},
                "end_line": {"type": "integer"},
            },
            "required": ["file"],
        },
    },
]


def _rpc_result(rpc_id, text: str, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


class PlatformToolsWrapper:
    """ASGI wrapper adding platform tools in front of graphify's MCP app.

    Speaks the JSON-RPC wire format only (server runs stateless+json_response,
    so request and response bodies are plain JSON):
      - tools/call for a platform tool -> handled here, never forwarded
      - tools/list -> forwarded, then the platform tool schemas are appended
      - everything else -> passed through byte-for-byte
    Fail-open: any parse problem forwards the original traffic untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        chunks: list[dict] = []
        while True:
            message = await receive()
            chunks.append(message)
            if not message.get("more_body", False):
                break
        body = b"".join(m.get("body", b"") for m in chunks)

        replayed = 0

        async def replay():
            nonlocal replayed
            if replayed < len(chunks):
                replayed += 1
                return chunks[replayed - 1]
            return {"type": "http.request", "body": b"", "more_body": False}

        try:
            rpc = json.loads(body)
        except Exception:
            rpc = None

        # Handle a platform tools/call locally — but only a well-formed REQUEST
        # (dict params, and an id present: a notification has no id and must
        # get no response, so let it fall through to the downstream server).
        params = rpc.get("params") if isinstance(rpc, dict) else None
        if (
            isinstance(rpc, dict)
            and rpc.get("method") == "tools/call"
            and rpc.get("id") is not None
            and isinstance(params, dict)
            and params.get("name") in PLATFORM_TOOLS
        ):
            handler = PLATFORM_TOOLS[params["name"]]
            arguments = params.get("arguments") or {}
            import asyncio

            try:
                # Off the event loop: a heavy search must not stall health
                # checks or other requests on this single-loop uvicorn worker.
                text = await asyncio.to_thread(handler, arguments if isinstance(arguments, dict) else {})
                is_error = text.startswith("error:")
            except Exception as exc:  # noqa: BLE001 — tool bug must not kill the server
                text, is_error = f"error: {type(exc).__name__}: {exc}", True
            payload = json.dumps(_rpc_result(rpc.get("id"), text, is_error)).encode()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode())],
            })
            await send({"type": "http.response.body", "body": payload})
            return

        if isinstance(rpc, dict) and rpc.get("method") == "tools/list":
            captured = {"status": 200, "headers": [], "body": b""}

            async def capture(message):
                if message["type"] == "http.response.start":
                    captured["status"] = message["status"]
                    captured["headers"] = list(message.get("headers", []))
                elif message["type"] == "http.response.body":
                    captured["body"] += message.get("body", b"")

            await self.app(scope, replay, capture)
            out_body = captured["body"]
            if captured["status"] == 200:
                try:
                    out = json.loads(out_body)
                    out["result"]["tools"].extend(PLATFORM_TOOL_SCHEMAS)
                    out_body = json.dumps(out).encode()
                except Exception:
                    pass  # fail-open: serve graphify's original list
            headers = [(k, v) for (k, v) in captured["headers"] if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(out_body)).encode()))
            await send({"type": "http.response.start", "status": captured["status"], "headers": headers})
            await send({"type": "http.response.body", "body": out_body})
            return

        return await self.app(scope, replay, send)


def sync_all() -> tuple[list[str], list[str]]:
    """Returns (all repo ids, repo ids whose graph CHANGED this pass)."""
    try:
        repo_ids = discover_repo_ids()
    except Exception as exc:
        log(f"discover failed: {exc}")
        return [], []
    changed_ids: list[str] = []
    for repo_id in repo_ids:
        try:
            if sync_repo(repo_id):
                changed_ids.append(repo_id)
        except Exception as exc:  # never let one repo break the rest
            log(f"sync failed for {repo_id}: {exc}")
    return repo_ids, changed_ids


# graphify rebuilds its in-memory graph context on the first tool call after
# the file swap (cache keyed on (mtime_ns, size)). Left to a USER request,
# that rebuild is paid inside the request — a large graph exceeds the data
# plane's timeout budget and every post-sync first call 504s. So the sync
# thread pays it here instead: one throwaway local tool call right after each
# swap (and once at boot) keeps user-facing calls on the warm path.
WARM_TIMEOUT = 600  # a big merged graph on 1 vCPU can take minutes


def _warm_default_graph() -> None:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": "warm", "method": "tools/call",
        "params": {"name": "graph_stats", "arguments": {}},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/mcp",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=WARM_TIMEOUT) as resp:
        resp.read()
    log(f"graph warmed in {time.monotonic() - t0:.1f}s")


def _warm_at_boot() -> None:
    """Wait for uvicorn to listen, then pay the initial graph load."""
    for _ in range(120):
        try:
            _warm_default_graph()
            return
        except (ConnectionError, urllib.error.URLError, OSError):
            time.sleep(2)
    log("boot warm gave up (server never came up?)")


def sync_loop(default_repo: str) -> None:
    if default_repo:
        try:
            _warm_at_boot()
        except Exception as exc:  # warming is an optimization, never fatal
            log(f"boot warm failed: {exc}")
    while True:
        time.sleep(SYNC_INTERVAL)
        try:
            _, changed_ids = sync_all()
            if default_repo and default_repo in changed_ids:
                _warm_default_graph()
        except Exception as exc:  # never kill the server
            log(f"sync loop error: {exc}")


def main() -> None:
    # Defense in depth: an inherited GRAPHIFY_API_KEY would arm graphify's
    # api-key middleware and 401 every proxied request.
    os.environ.pop("GRAPHIFY_API_KEY", None)

    GRAPHS_ROOT.mkdir(parents=True, exist_ok=True)
    repo_ids, _ = sync_all()
    log(f"initial sync complete: {len(repo_ids)} repo(s): {repo_ids}")

    if DEFAULT_REPO_ID:
        default_repo = DEFAULT_REPO_ID
    elif len(repo_ids) == 1:
        default_repo = repo_ids[0]
    else:
        default_repo = ""

    default_graph = str(GRAPHS_ROOT / (default_repo or "__default__") / "graphify-out" / "graph.json")
    if default_repo:
        log(f"default graph: {default_graph}")
    else:
        log(f"no default graph — pure multi-project mode; tools require project_path={GRAPHS_ROOT}/<repo_id>")

    threading.Thread(target=sync_loop, args=(default_repo,), daemon=True).start()

    import graphify.serve as gserve

    serve_kwargs = dict(
        host="0.0.0.0",
        port=PORT,
        path="/mcp",
        stateless=True,
        json_response=True,
        api_key=None,
    )

    # Per-repo runtimes get the platform code-search tools; the hub and the
    # unpinned multi-project mode do not. _build_http_app is graphify's own
    # serve_http internals (the bundle pins the graphify version); if a future
    # version renames it, fall back to plain serving — graph tools keep
    # working, only code search is lost.
    if CODE_REPO and hasattr(gserve, "_build_http_app"):
        import uvicorn

        log(f"platform code tools enabled for {CODE_REPO}: {sorted(PLATFORM_TOOLS)}")
        app = gserve._build_http_app(default_graph, session_timeout=3600.0, **serve_kwargs)
        uvicorn.run(PlatformToolsWrapper(app), host="0.0.0.0", port=PORT)
    else:
        if CODE_REPO:
            log("graphify.serve._build_http_app missing — code tools disabled, serving plain")
        gserve.serve_http(default_graph, **serve_kwargs)


if __name__ == "__main__":
    main()
