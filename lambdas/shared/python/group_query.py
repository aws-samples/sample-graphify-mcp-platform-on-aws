"""Read-only group graph engine shared by the MCP proxy and management API.

All AWS clients and resource names come from the caller. No client-supplied
bucket, object key, runtime, or source fallback is accepted. Every successful
request checks current ACLs twice, including requests served from the cache.

The producer writes manifest.json and graph.json under the active immutable
version prefix. ``graph_sha256`` in the manifest, when present, is the SHA-256
of graph.json's exact bytes. File ``sha256`` values hash exact UTF-8 bytes;
evidence ``sha256`` values hash the exact quote. Original node IDs and labels
need not be unique; graph node IDs and edge IDs must be unique.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import re
import threading
import time

from group_access import GroupError, require_access


GRAPH_MAX_BYTES = 32 * 1024 * 1024
MANIFEST_MAX_BYTES = 4 * 1024 * 1024
SOURCE_MAX_BYTES = 2 * 1024 * 1024
READ_MAX_LINES = 400
MAX_RESULTS = 100
MAX_PAGE_SIZE = 500
MAX_CACHE_ENTRIES = 2
SEARCH_MAX_FILES = 100
SEARCH_MAX_BYTES = 8 * 1024 * 1024
SEARCH_MAX_SECONDS = 5.0
MAX_NODES = 50_000
MAX_LINKS = 200_000
MAX_RELATIONS = 10_000
MAX_MANIFEST_FILES = 20_000
MAX_RESPONSE_BYTES = 1024 * 1024

_GROUP_ID = re.compile(r"grp_[0-9a-f]{32}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = threading.RLock()


def _fail(message: str, status: int = 409):
    raise GroupError(status, message)


def _component(value, name: str, status: int = 409) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value) or value in (".", ".."):
        _fail(f"Invalid {name}", status)
    return value


def _text(value, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(f"{name} must be a nonempty string of at most {maximum} characters", 400)
    return value


def _integer(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(f"{name} must be an integer between {low} and {high}", 400)
    return value


def _path(value, status: int = 400) -> str:
    # Do not normalize hostile paths into valid ones. Percent escapes are
    # rejected too: the manifest is an exact name map, never a URL resolver.
    if (
        not isinstance(value, str) or not value or len(value) > 2048
        or "\\" in value or re.match(r"^[A-Za-z]:", value)
        or re.search(r"%(?:2e|2f|5c)", value, re.I)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _fail("Invalid source file path", status)
    return value


def _digest(value, name: str = "checksum") -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _fail(f"Invalid {name}")
    return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(raw: bytes, name: str) -> dict:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("Non-finite JSON number")

    try:
        result = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        _fail(f"Invalid {name} JSON")
    if not isinstance(result, dict):
        _fail(f"Invalid {name}")
    return result


def _read_object(s3, bucket: str, key: str, cap: int) -> bytes:
    """Bound reads even when ContentLength is absent or incorrect."""
    body = None
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        length = response.get("ContentLength")
        if length is not None and (type(length) is not int or length < 0):
            _fail("Invalid artifact size")
        body = response["Body"]
        if length is not None and length > cap:
            _fail("Artifact exceeds the read size limit", 413)
        chunks, count = [], 0
        while count <= cap:
            chunk = body.read(min(64 * 1024, cap + 1 - count))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                _fail("Invalid artifact body")
            chunks.append(chunk)
            count += len(chunk)
        if count > cap:
            _fail("Artifact exceeds the read size limit", 413)
        if length is not None and count != length:
            _fail("Artifact length mismatch")
        return b"".join(chunks)
    except GroupError:
        raise
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "NoSuchVersion", "404", "NotFound"}:
            _fail("The pinned group artifact is unavailable; rebuild the group")
        _fail("Unable to read the pinned group artifact", 503)
    finally:
        if body is not None:
            try:
                body.close()
            except Exception:
                pass


def _snapshot(meta: dict, gid: str) -> tuple:
    if not isinstance(meta, dict) or meta.get("group_id", gid) != gid:
        _fail("Invalid group metadata")
    revision = meta.get("revision")
    if (
        isinstance(revision, bool) or not isinstance(revision, (int, Decimal))
        or revision < 0 or revision != int(revision)
        or revision != meta.get("active_revision")
    ):
        _fail("Group revision has changed; rebuild the group")
    revision = int(revision)
    version = _component(meta.get("active_version"), "active group version")
    sources = meta.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        _fail("Invalid group source membership")
    source_ids = []
    for source in sources:
        if not isinstance(source, dict):
            _fail("Invalid group source membership")
        source_ids.append(_component(source.get("source_id"), "source ID"))
    versions = meta.get("active_source_versions")
    if (
        len(set(source_ids)) != len(source_ids) or not isinstance(versions, dict)
        or set(versions) != set(source_ids)
        or any(not isinstance(value, str) or not value for value in versions.values())
    ):
        _fail("Invalid active source versions")
    descriptions = meta.get("active_source_descriptions")
    if descriptions is not None and (
        not isinstance(descriptions, dict) or set(descriptions) != set(source_ids)
    ):
        _fail("Invalid active source description versions")
    return (
        version, revision, tuple(sorted(versions.items())), deepcopy(sources),
        deepcopy(descriptions), meta.get("status"),
    )


class _Request:
    def __init__(self, ddb, platform_table, registry_table, s3, bucket, sub, gid,
                 group_version=""):
        if not isinstance(gid, str) or not _GROUP_ID.fullmatch(gid):
            _fail("Invalid group ID", 400)
        if not isinstance(group_version, str):
            _fail("group_version must be a string", 400)
        self.access_args = (ddb, platform_table, registry_table, sub, gid)
        self.meta = require_access(*self.access_args, check_versions=True)
        self.snapshot = _snapshot(self.meta, gid)
        self.version, self.revision = self.snapshot[:2]
        if group_version and group_version != self.version:
            _fail("group_version does not match the active group version")
        self.gid, self.s3, self.bucket = gid, s3, bucket
        self.prefix = f"groups/{gid}/versions/{self.version}/"
        for field, suffix in (("active_manifest_key", "manifest.json"), ("active_graph_key", "graph.json")):
            if field in self.meta and self.meta[field] != self.prefix + suffix:
                _fail("Active artifact key does not match the pinned group version")
        self.source_versions = dict(self.snapshot[2])
        self.manifest = None
        self.bundle = None

    def finish(self, data: dict) -> dict:
        # Copy before the last access check so expensive serialization/data
        # preparation cannot leave a stale ACL check behind it.
        result = deepcopy(data)
        if len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")) > MAX_RESPONSE_BYTES:
            _fail("Response exceeds 1 MiB; reduce the result count or source line range", 413)
        fresh = require_access(*self.access_args, check_versions=True)
        if _snapshot(fresh, self.gid) != self.snapshot:
            _fail("Group changed during the request; retry with the current version")
        return result

    def common(self) -> dict:
        result = {
            "group_id": self.gid, "version": self.version, "group_version": self.version,
            "revision": self.revision, "status": self.meta.get("status", ""),
        }
        if self.manifest is not None:
            result.update({
                "stats": self.manifest.get("stats", {}),
                "usage": self.manifest.get("usage", {}),
                "limits": self.manifest.get("limits", {}),
                "partial": self.manifest.get("partial", False),
                "partial_reasons": self.manifest.get("partial_reasons", []),
            })
        return result

    def load_manifest(self) -> dict:
        if self.manifest is not None:
            return self.manifest
        raw = _read_object(self.s3, self.bucket, self.prefix + "manifest.json",
                           MANIFEST_MAX_BYTES)
        manifest = _json(raw, "manifest")
        if (
            manifest.get("group_id") != self.gid
            or manifest.get("version") != self.version
            or manifest.get("revision") != self.revision
            or manifest.get("source_versions") != self.source_versions
            or manifest.get("build_id", self.version) != self.version
            or manifest.get("graph_key", self.prefix + "graph.json") != self.prefix + "graph.json"
            or (self.snapshot[4] is not None and manifest.get("source_descriptions") != self.snapshot[4])
        ):
            _fail("Manifest does not match the active group and source versions")
        files = manifest.get("files")
        if not isinstance(files, dict) or not set(files).issubset(self.source_versions):
            _fail("Invalid manifest source files")
        count = 0
        for sid, mapping in files.items():
            if not isinstance(mapping, dict):
                _fail("Invalid manifest file map")
            for path, entry in mapping.items():
                count += 1
                if count > MAX_MANIFEST_FILES:
                    _fail("Manifest exceeds the file limit", 413)
                _path(path, 409)
                if not isinstance(entry, dict):
                    _fail("Invalid manifest file entry")
                _digest(entry.get("sha256"), "source checksum")
                key = entry.get("key")
                expected = self.prefix + f"files/{sid}/"
                if (
                    not isinstance(key, str) or not key.startswith(expected)
                    or not re.fullmatch(r"[0-9a-f]{64}\.txt", key[len(expected):])
                ):
                    _fail("Manifest file key is outside the pinned source prefix")
                lines = entry.get("line_count")
                if type(lines) is not int or lines < 0:
                    _fail("Invalid manifest line count")
        if "graph_sha256" in manifest:
            _digest(manifest["graph_sha256"], "graph checksum")
        self.manifest_sha256 = _sha(raw)
        self.manifest = manifest
        return manifest

    def load_graph(self) -> dict:
        if self.bundle is not None:
            return self.bundle
        manifest = self.load_manifest()
        key = (self.bucket, self.gid, self.version)
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                if cached["manifest_sha256"] != self.manifest_sha256:
                    _fail("An immutable group manifest changed; rebuild the group")
                if manifest.get("graph_sha256", cached["graph_sha256"]) != cached["graph_sha256"]:
                    _fail("Graph checksum mismatch")
                _CACHE.move_to_end(key)
                self.bundle = cached
                return cached
        raw = _read_object(self.s3, self.bucket, self.prefix + "graph.json", GRAPH_MAX_BYTES)
        checksum = _sha(raw)
        if manifest.get("graph_sha256", checksum) != checksum:
            _fail("Graph checksum mismatch")
        graph = _json(raw, "graph")
        bundle = _index_graph(graph, self)
        bundle.update(graph_sha256=checksum, manifest_sha256=self.manifest_sha256)
        with _CACHE_LOCK:
            previous = _CACHE.get(key)
            if previous and (
                previous["manifest_sha256"] != self.manifest_sha256
                or previous["graph_sha256"] != checksum
            ):
                _fail("An immutable group graph changed; rebuild the group")
            _CACHE[key] = bundle
            _CACHE.move_to_end(key)
            while len(_CACHE) > MAX_CACHE_ENTRIES:
                _CACHE.popitem(last=False)
        self.bundle = bundle
        return bundle

    def source_text(self, sid: str, file: str) -> tuple[str, dict]:
        _component(sid, "source ID", 400)
        _path(file)
        manifest = self.load_manifest()
        if sid not in self.source_versions:
            _fail("Source is not a member of this group", 404)
        entry = manifest["files"].get(sid, {}).get(file)
        if entry is None:
            _fail("File is absent from this pinned group version", 404)
        raw = _read_object(self.s3, self.bucket, entry["key"], SOURCE_MAX_BYTES)
        if _sha(raw) != entry["sha256"]:
            _fail("Source checksum mismatch")
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            _fail("Pinned source is not valid UTF-8 text")
        if "\0" in text:
            _fail("Pinned source is not a text file")
        if len(text.splitlines()) != entry["line_count"]:
            _fail("Source line count mismatch")
        return text, entry


def _edge_id(edge: dict) -> str:
    value = edge.get("id", edge.get("edge_id"))
    if (
        not isinstance(value, str) or not value or len(value) > 4096
        or ("id" in edge and "edge_id" in edge and edge["id"] != edge["edge_id"])
    ):
        _fail("Invalid graph edge ID")
    return value


def _index_graph(graph: dict, request: _Request) -> dict:
    arrays = {}
    for name, cap in (("nodes", MAX_NODES), ("links", MAX_LINKS), ("relations", MAX_RELATIONS)):
        values = graph.get(name, [] if name == "relations" else None)
        if not isinstance(values, list) or len(values) > cap:
            _fail(f"Invalid or oversized graph {name}")
        if any(not isinstance(value, dict) for value in values):
            _fail(f"Invalid graph {name}")
        arrays[name] = values
    nodes, labels, originals = {}, {}, {}
    for node in arrays["nodes"]:
        nid, sid = node.get("id"), node.get("source_id")
        if not isinstance(nid, str) or not nid or len(nid) > 4096 or nid in nodes:
            _fail("Graph node IDs must be unique nonempty strings")
        if not isinstance(sid, str) or sid not in request.source_versions:
            _fail("Graph node refers to an unknown source")
        if node.get("source_version") != request.source_versions[sid]:
            _fail("Graph node source version mismatch")
        if node.get("source_file"):
            _path(node["source_file"], 409)
        original = node.get("original_id")
        if original is None and node.get("type") != "source_anchor":
            _fail("Graph node original_id is required")
        if original is not None and type(original) not in (str, int, float, bool):
            _fail("Invalid original node ID")
        label = node.get("label")
        if not isinstance(label, str):
            _fail("Graph node label must be a string")
        if original is not None:
            original_key = (sid, json.dumps(original, ensure_ascii=False, allow_nan=False))
            if original_key in originals:
                _fail("Duplicate original node ID within one source")
            originals[original_key] = nid
        nodes[nid] = node
        labels.setdefault(label.casefold(), []).append(nid)
    edges, relations = {}, {}
    adjacency = {nid: [] for nid in nodes}
    for edge in arrays["links"]:
        eid = _edge_id(edge)
        if eid in edges:
            _fail("Graph edge IDs must be unique")
        source, target = edge.get("source"), edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str) or source not in nodes or target not in nodes:
            _fail("Graph edge endpoint is missing")
        edges[eid] = edge
        adjacency[source].append((target, eid, "out"))
        adjacency[target].append((source, eid, "in"))
    for relation in arrays["relations"]:
        eid = _edge_id(relation)
        if eid in relations or eid not in edges:
            _fail("Relation must identify a unique graph edge")
        edge = edges[eid]
        for field in ("source", "target", "relation"):
            if field in relation and relation[field] != edge.get(field):
                _fail("Relation does not match its graph edge")
        evidence = relation.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) != 2:
            _fail("Invalid relation evidence")
        endpoint_sources = {nodes[edge["source"]]["source_id"], nodes[edge["target"]]["source_id"]}
        if len(endpoint_sources) != 2:
            _fail("Cross-source relation endpoints must belong to different sources")
        for item in evidence:
            if not isinstance(item, dict):
                _fail("Invalid relation evidence")
            sid = item.get("source_id")
            if not isinstance(sid, str) or sid not in request.source_versions:
                _fail("Evidence refers to an unknown source")
            path = _path(item.get("file"), 409)
            if path not in request.manifest["files"].get(sid, {}):
                _fail("Evidence file is absent from the manifest")
            if "source_version" in item and item["source_version"] != request.source_versions[sid]:
                _fail("Evidence source version mismatch")
            start, end = item.get("line_start"), item.get("line_end")
            if (
                type(start) is not int or type(end) is not int or start < 1 or end < start
                or end > request.manifest["files"][sid][path]["line_count"]
                or end - start + 1 > READ_MAX_LINES
            ):
                _fail("Invalid evidence line range")
            quote = item.get("quote")
            if not isinstance(quote, str) or not quote or len(quote.encode("utf-8")) > SOURCE_MAX_BYTES:
                _fail("Invalid evidence quote")
            if _digest(item.get("sha256"), "evidence checksum") != _sha(quote.encode("utf-8")):
                _fail("Evidence quote checksum mismatch")
        if {item["source_id"] for item in evidence} != endpoint_sources:
            _fail("Relation evidence must identify both endpoint sources")
        relations[eid] = relation
    return {
        **arrays, "node_index": nodes, "label_index": labels, "edge_index": edges,
        "relation_index": relations, "adjacency": adjacency,
    }


def get_graph_page(ddb, platform_table, registry_table, s3, bucket, sub, gid,
                   kind="nodes", offset=0, limit=500, group_version="") -> dict:
    """Return a bounded page; subsequent pages must pin the first page's version."""
    if kind not in ("nodes", "links", "relations"):
        _fail("kind must be nodes, links, or relations", 400)
    _integer(offset, "offset", 0, MAX_LINKS)
    _integer(limit, "limit", 1, MAX_PAGE_SIZE)
    if offset and not group_version:
        _fail("group_version is required for subsequent pages", 400)
    request = _Request(ddb, platform_table, registry_table, s3, bucket, sub, gid, group_version)
    values = request.load_graph()[kind]
    end = min(offset + limit, len(values))
    result = {
        **request.common(), "sources": request.meta["sources"], kind: values[offset:end],
        "next_offset": end if end < len(values) else None,
    }
    while end > offset + 1 and len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        end = offset + max(1, (end - offset) // 2)
        result[kind] = values[offset:end]
        result["next_offset"] = end
    return request.finish(result)


def _read_source(request: _Request, args: dict) -> dict:
    sid = _component(args.get("source_id"), "source ID", 400)
    file = _path(args.get("file"))
    start = _integer(args.get("start_line", 1), "start_line", 1, SOURCE_MAX_BYTES)
    end = args.get("end_line")
    if end is None:
        end = start + READ_MAX_LINES - 1
    else:
        _integer(end, "end_line", start, start + READ_MAX_LINES - 1)
    text, entry = request.source_text(sid, file)
    lines = text.splitlines(keepends=True)
    if not lines:
        if start != 1:
            _fail("start_line is beyond the end of the file", 400)
        start, end = 0, 0
    elif start > len(lines):
        _fail("start_line is beyond the end of the file", 400)
    else:
        end = min(end, len(lines))
    excerpt = "".join(lines[start - 1:end]) if lines else ""
    return {
        **request.common(), "source_id": sid, "source_version": request.source_versions[sid],
        "file": file, "start_line": start, "end_line": end,
        "line_count": len(lines), "text": excerpt, "sha256": entry["sha256"],
        "quote_sha256": _sha(excerpt.encode("utf-8")),
    }


def get_source(ddb, platform_table, registry_table, s3, bucket, sub, gid,
               source_id, file, start_line=1, end_line=None, group_version="") -> dict:
    """Read exact source text and provenance from one pinned manifest entry."""
    request = _Request(ddb, platform_table, registry_table, s3, bucket, sub, gid, group_version)
    return request.finish(_read_source(request, {
        "source_id": source_id, "file": file, "start_line": start_line, "end_line": end_line,
    }))


def _resolve(bundle: dict, value: str, by_name=False) -> list[dict]:
    _text(value, "node", 4096)
    if not by_name and value in bundle["node_index"]:
        return [bundle["node_index"][value]]
    return [bundle["node_index"][nid] for nid in bundle["label_index"].get(value.casefold(), [])]


def _ambiguous(candidates: list[dict], field="node") -> dict:
    return {
        "ambiguous": True, "field": field, "candidates": candidates[:MAX_RESULTS],
        "candidate_count": len(candidates), "partial": len(candidates) > MAX_RESULTS,
    }


def _selected(bundle: dict, value: str, field="node", by_name=False):
    candidates = _resolve(bundle, value, by_name)
    if not candidates:
        _fail(f"{field} was not found in the pinned group graph", 404)
    if len(candidates) > 1:
        return None, _ambiguous(candidates, field)
    return candidates[0], None


def _query_graph(request: _Request, args: dict) -> dict:
    query = _text(args.get("query"), "query", 256).casefold()
    limit = _integer(args.get("max_results", 20), "max_results", 1, MAX_RESULTS)
    bundle = request.load_graph()
    terms = query.split()
    matches = {}
    for node in bundle["nodes"]:
        haystack = " ".join(str(node.get(field, "")) for field in (
            "id", "original_id", "label", "type", "source_id", "source_file", "description",
        )).casefold()
        if all(term in haystack for term in terms):
            score = 2 if node["label"].casefold() == query else 1
            matches[node["id"]] = (score, node)
    # Requirements/contracts often occur only in cross-source evidence, not
    # in a heading or function name. Seed both endpoints of matching stored
    # relations so query_graph can discover those connections directly.
    relations = []
    for edge in bundle["relations"]:
        haystack = " ".join([
            *(str(edge.get(field, "")) for field in ("id", "source", "target", "relation", "signal")),
            *(str(e.get(field, "")) for e in edge.get("evidence", [])
              for field in ("source_id", "file", "quote")),
        ]).casefold()
        if all(term in haystack for term in terms):
            relations.append(edge)
            for nid in (edge["source"], edge["target"]):
                matches.setdefault(nid, (1, bundle["node_index"][nid]))
    ordered = sorted(matches.values(), key=lambda match: (-match[0], match[1]["id"]))
    chosen = [node for _score, node in ordered[:limit]]
    ids = {node["id"] for node in chosen}
    links = [edge for edge in bundle["links"] if edge["source"] in ids or edge["target"] in ids]
    return {
        "nodes": chosen, "links": links[:MAX_RESULTS], "total_matches": len(matches),
        "relations": relations[:MAX_RESULTS], "total_relation_matches": len(relations),
        "partial": len(matches) > limit or len(links) > MAX_RESULTS or len(relations) > MAX_RESULTS,
        "search_mode": "graph_node_and_relation_terms",
    }


def _get_node(request: _Request, args: dict) -> dict:
    if ("node_id" in args) == ("name" in args):
        _fail("Specify exactly one of node_id or name", 400)
    node, ambiguity = _selected(request.load_graph(), args.get("node_id", args.get("name")),
                                by_name="name" in args)
    return ambiguity or {"node": node}


def _get_neighbors(request: _Request, args: dict) -> dict:
    limit = _integer(args.get("max_results", 50), "max_results", 1, MAX_RESULTS)
    direction = args.get("direction", "both")
    if direction not in ("both", "in", "out"):
        _fail("direction must be both, in, or out", 400)
    bundle = request.load_graph()
    node, ambiguity = _selected(bundle, args.get("node_id"))
    if ambiguity:
        return ambiguity
    selected = [item for item in bundle["adjacency"][node["id"]]
                if direction == "both" or item[2] == direction]
    edges = list(dict.fromkeys(eid for _nid, eid, _direction in selected))
    links = [bundle["edge_index"][eid] for eid in edges[:limit]]
    neighbor_ids = dict.fromkeys(
        edge["target"] if edge["source"] == node["id"] else edge["source"] for edge in links
    )
    return {
        "node": node, "nodes": [bundle["node_index"][nid] for nid in neighbor_ids],
        "links": links, "partial": len(edges) > limit, "direction": direction,
    }


def _find_path(request: _Request, args: dict) -> dict:
    hops = _integer(args.get("max_hops", 8), "max_hops", 1, 8)
    bundle = request.load_graph()
    source, ambiguity = _selected(bundle, args.get("source"), "source")
    if ambiguity:
        return ambiguity
    target, ambiguity = _selected(bundle, args.get("target"), "target")
    if ambiguity:
        return ambiguity
    # Traversal is undirected for requirement -> code -> test traceability;
    # each returned edge preserves its actual source/target direction.
    start, goal = source["id"], target["id"]
    pending = deque([(start, 0)])
    previous = {start: None}
    while pending:
        nid, depth = pending.popleft()
        if nid == goal:
            break
        if depth >= hops:
            continue
        for neighbor, eid, _direction in bundle["adjacency"][nid]:
            if neighbor not in previous:
                previous[neighbor] = (nid, eid)
                pending.append((neighbor, depth + 1))
    if goal not in previous:
        return {"found": False, "path": [], "links": [], "max_hops": hops,
                "traversal": "undirected"}
    path, edges, current = [], [], goal
    while current is not None:
        path.append(bundle["node_index"][current])
        step = previous[current]
        if step is None:
            break
        current, eid = step
        edges.append(bundle["edge_index"][eid])
    return {"found": True, "path": path[::-1], "links": edges[::-1],
            "hops": len(edges), "traversal": "undirected"}


def _get_relation(request: _Request, args: dict) -> dict:
    eid = _text(args.get("edge_id"), "edge_id", 4096)
    bundle = request.load_graph()
    relation = bundle["relation_index"].get(eid, bundle["edge_index"].get(eid))
    if relation is None:
        _fail("Relation was not found in the pinned group graph", 404)
    files = {}
    for item in bundle["relation_index"].get(eid, {}).get("evidence", []):
        key = (item["source_id"], item["file"])
        if key not in files:
            text, _entry = request.source_text(*key)
            files[key] = text.splitlines()
        excerpt = "\n".join(files[key][item["line_start"] - 1:item["line_end"]])
        if item["quote"] != excerpt:
            _fail("Relation quote does not match the pinned source lines")
    return {"relation": relation}


def _glob_match(path: str, pattern: str) -> bool:
    """Linear-space wildcard matching, bounded by path * pattern length.

    Supports only '*' and '?'; unlike user regex or recursive backtracking this
    has a predictable operation bound. '*' includes directory separators.
    """
    previous = [True] + [False] * len(path)
    for token in pattern:
        current = [previous[0] and token == "*"]
        for index, char in enumerate(path, 1):
            if token == "*":
                current.append(current[-1] or previous[index])
            else:
                current.append(previous[index - 1] and (token == "?" or token == char))
        previous = current
    return previous[-1]


def _search_code(request: _Request, args: dict) -> dict:
    pattern = _text(args.get("pattern"), "pattern", 256)
    limit = _integer(args.get("max_results", 20), "max_results", 1, MAX_RESULTS)
    glob = args.get("glob", "*")
    _text(glob, "glob", 128)
    if any(char in glob for char in ("\\", "%", ":", "[", "]", "\0")) or glob.startswith("/") or ".." in glob:
        _fail("glob supports relative paths with only * and ? wildcards", 400)
    sid = args.get("source_id")
    if sid is not None:
        _component(sid, "source ID", 400)
        if sid not in request.source_versions:
            _fail("Source is not a member of this group", 404)
    manifest = request.load_manifest()
    deadline = time.monotonic() + SEARCH_MAX_SECONDS
    matches, scanned, scanned_bytes, visited, reasons = [], 0, 0, 0, []
    for source_id, files in manifest["files"].items():
        if sid is not None and sid != source_id:
            continue
        for file in files:
            visited += 1
            if time.monotonic() >= deadline or visited > MAX_MANIFEST_FILES:
                reasons.append("time_limit")
                break
            if not _glob_match(file, glob):
                continue
            # Reserve the worst-case read before fetching; the byte budget is
            # shared across the whole call, not restarted for each source.
            if scanned >= SEARCH_MAX_FILES or scanned_bytes + SOURCE_MAX_BYTES > SEARCH_MAX_BYTES:
                reasons.append("scan_limit")
                break
            text, entry = request.source_text(source_id, file)
            scanned += 1
            scanned_bytes += len(text.encode("utf-8"))
            for line_no, line in enumerate(text.splitlines(keepends=True), 1):
                if time.monotonic() >= deadline:
                    reasons.append("time_limit")
                    break
                if pattern in line:
                    matches.append({
                        "source_id": source_id, "source_version": request.source_versions[source_id],
                        "file": file, "line": line_no, "line_start": line_no, "line_end": line_no,
                        "quote": line, "sha256": _sha(line.encode("utf-8")),
                        "file_sha256": entry["sha256"], "group_version": request.version,
                    })
                    if len(matches) >= limit:
                        reasons.append("result_limit")
                        break
            if reasons:
                break
        if reasons:
            break
    return {
        "matches": matches, "partial": bool(reasons),
        "stop_reasons": reasons, "files_scanned": scanned, "bytes_scanned": scanned_bytes,
        "search_mode": "literal_case_sensitive",
    }


def _schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": {
        **properties,
        "group_version": {"type": "string", "description": "Pin a prior response's group_version; mismatch is rejected."},
    }, "required": list(required), "additionalProperties": False}


_STRING = {"type": "string", "minLength": 1}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}
TOOLS = [
    {
        "name": "query_graph",
        "description": "Search group node metadata and stored cross-source relation signals/evidence by all query terms. Use requirement IDs, API paths or relation names to find matching relations, both endpoints and graph neighbors; pin group_version on follow-up calls.",
        "inputSchema": _schema({"query": {**_STRING, "maxLength": 256}, "max_results": _LIMIT}, ("query",)),
    },
    {
        "name": "get_node",
        "description": "Get a source-qualified graph node ID or exact label. Ambiguous labels return candidates; use their IDs.",
        "inputSchema": _schema({"node_id": _STRING, "name": _STRING}),
    },
    {
        "name": "find_path",
        "description": "Find a shortest undirected trace through actual graph edges, at most eight hops. Returned links preserve their real direction and evidence kind.",
        "inputSchema": _schema({"source": _STRING, "target": _STRING,
                                "max_hops": {"type": "integer", "minimum": 1, "maximum": 8}}, ("source", "target")),
    },
    {
        "name": "get_neighbors",
        "description": "Read actual incoming/outgoing graph edges and adjacent nodes. Preserves parallel edges and provenance.",
        "inputSchema": _schema({"node_id": _STRING, "max_results": _LIMIT,
                                "direction": {"type": "string", "enum": ["both", "in", "out"]}}, ("node_id",)),
    },
    {
        "name": "get_relation",
        "description": "Read a graph edge and its cross-source relation evidence. Quotes are checked against the exact pinned source text.",
        "inputSchema": _schema({"edge_id": _STRING}, ("edge_id",)),
    },
    {
        "name": "read_source",
        "description": "Read exact UTF-8 text from the group's immutable source snapshot, with provenance and checksums. Maximum 2 MiB per file and 400 lines per call; preserves full lines.",
        "inputSchema": _schema({
            "source_id": _STRING, "file": _STRING,
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        }, ("source_id", "file")),
    },
    {
        "name": "search_code",
        "description": "Search pinned source texts for a case-sensitive literal (no regular expressions). Optional glob supports * and ?. Returns exact lines and provenance; bounded scans report partial results.",
        "inputSchema": _schema({"pattern": {**_STRING, "maxLength": 256},
                                "source_id": _STRING, "glob": {**_STRING, "maxLength": 128},
                                "max_results": _LIMIT}, ("pattern",)),
    },
]
_HANDLERS = {
    "query_graph": _query_graph, "get_node": _get_node, "find_path": _find_path,
    "get_neighbors": _get_neighbors, "get_relation": _get_relation,
    "read_source": _read_source, "search_code": _search_code,
}
_TOOL_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _rpc_error(req_id, code: int, message: str, status=None) -> dict:
    error = {"code": code, "message": message}
    if status is not None:
        error["data"] = {"status": status}
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def handle_rpc(ddb, platform_table, registry_table, s3, bucket, sub, gid, rpc):
    """Handle a single MCP JSON-RPC message. Notifications return None."""
    if (
        not isinstance(rpc, dict) or rpc.get("jsonrpc") != "2.0"
        or not isinstance(rpc.get("method"), str)
        or ("id" in rpc and (type(rpc["id"]) not in (str, int) and rpc["id"] is not None))
    ):
        return _rpc_error(None, -32600, "Invalid Request")
    req_id = rpc.get("id")
    notification = "id" not in rpc
    method, params = rpc["method"], rpc.get("params", {})
    if not isinstance(params, dict):
        return None if notification else _rpc_error(req_id, -32602, "params must be an object")
    # Genuine notifications neither execute tools nor return JSON-RPC replies.
    if notification:
        require_access(ddb, platform_table, registry_table, sub, gid)
        return None
    request = None
    try:
        if method in ("initialize", "ping", "tools/list"):
            meta = require_access(ddb, platform_table, registry_table, sub, gid)
            if method == "initialize":
                protocol = params.get("protocolVersion")
                result = {
                    "protocolVersion": protocol if protocol in _PROTOCOL_VERSIONS else _PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "graphify-group", "version": "1.0.0"},
                    "instructions": "Use source-qualified node IDs and pin group_version on follow-up calls. Graph relations and text share one immutable group snapshot.",
                }
            elif method == "tools/list":
                if params.get("cursor"):
                    return _rpc_error(req_id, -32602, "tools/list has no further pages")
                result = {"tools": deepcopy(TOOLS)}
            else:
                result = {}
            fresh = require_access(ddb, platform_table, registry_table, sub, gid)
            if any(meta.get(field) != fresh.get(field) for field in ("revision", "active_version", "sources")):
                _fail("Group changed during the request")
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        if method != "tools/call":
            require_access(ddb, platform_table, registry_table, sub, gid)
            return _rpc_error(req_id, -32601, "Method not found")
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or name not in _HANDLERS:
            _fail("Unknown group graph tool", 400)
        if not isinstance(args, dict):
            _fail("Tool arguments must be an object", 400)
        schema = _TOOL_SCHEMAS[name]
        if set(args) - set(schema["properties"]):
            _fail("Unknown tool argument; resource and runtime overrides are not accepted", 400)
        if set(schema["required"]) - set(args):
            _fail("Missing required tool argument", 400)
        request = _Request(ddb, platform_table, registry_table, s3, bucket, sub, gid,
                           args.get("group_version", ""))
        data = _HANDLERS[name](request, args)
        result = {**request.common(), **data}
        # A partial build remains partial even when this particular page/tool
        # did not hit an additional result limit.
        if request.manifest and request.manifest.get("partial"):
            result["partial"] = True
        result = deepcopy(result)
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False, default=str)
        request.finish({})
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "content": [{"type": "text", "text": serialized}],
            "structuredContent": result, "isError": False,
        }}
    except GroupError as exc:
        if method == "tools/call":
            # Do not include version, metadata, or a partially prepared result
            # when a final ACL check denied access.
            error_data = {"error": str(exc), "status": exc.status}
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(error_data)}],
                "structuredContent": error_data, "isError": True,
            }}
        return _rpc_error(req_id, -32000, str(exc), exc.status)
