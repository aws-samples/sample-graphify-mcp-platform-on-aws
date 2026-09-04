"""Fetch a files-source repo's uploaded documents from S3 (build plane).

Downloads every object under uploads/<REPO_ID>/ into /tmp/work/src and writes
the corpus manifest hash to /tmp/work/content_hash.

The hash recipe — sha256 over sorted "<rel>\\t<etag>\\t<size>" lines, folder
markers (keys ending "/") excluded — MUST stay byte-identical to
lambdas/poller/handler.py:files_manifest_hash: the poller compares its own
listing hash against last_built_sha (set by the completion Lambda from the
source_hash this build publishes), and any recipe drift makes every poll tick
look like a change and rebuild forever. Every listed object goes into the
hash even when its key is unsafe to materialize locally — only the DOWNLOAD
is skipped for those — so the two sides can never disagree.
"""

import hashlib
import os
import sys
import unicodedata
from pathlib import Path

import boto3

BUCKET = os.environ["GRAPH_BUCKET"]
REPO_ID = os.environ["REPO_ID"]
SRC = Path("/tmp/work/src")
HASH_OUT = Path("/tmp/work/content_hash")
MAX_FILES = 20_000
MAX_TOTAL_BYTES = 1 * 1024**3


def main() -> int:
    s3 = boto3.client("s3")
    prefix = f"uploads/{REPO_ID}/"
    entries: list[tuple[str, str, int]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel or rel.endswith("/"):
                continue  # folder markers
            entries.append((rel, obj.get("ETag", "").strip('"'), int(obj.get("Size", 0))))

    manifest = "\n".join(f"{rel}\t{etag}\t{size}" for rel, etag, size in sorted(entries))
    digest = hashlib.sha256(manifest.encode()).hexdigest()
    HASH_OUT.write_text(digest)

    if not entries:
        print(f"[fetch_uploads] no files under s3://{BUCKET}/{prefix}", file=sys.stderr)
        return 1
    if len(entries) > MAX_FILES:
        print(f"[fetch_uploads] too many files ({len(entries)} > {MAX_FILES})", file=sys.stderr)
        return 1
    total = sum(size for _, _, size in entries)
    if total > MAX_TOTAL_BYTES:
        print(f"[fetch_uploads] uploads exceed {MAX_TOTAL_BYTES} bytes ({total})", file=sys.stderr)
        return 1

    downloaded = 0
    for rel, _etag, _size in entries:
        # S3 keys are arbitrary strings; keep every materialized file inside
        # SRC (a hostile "../" key must never escape the work dir).
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            print(f"[fetch_uploads] skip unsafe key: {prefix}{rel}", file=sys.stderr)
            continue
        # Skip-on-error, never crash: keys S3 accepts but a POSIX filesystem
        # cannot materialize (a >NAME_MAX segment, a key that is both a file
        # and a directory prefix like "docs" + "docs/a.md") would otherwise
        # fail the build FOREVER — the manifest hash still includes the
        # skipped object (matching the poller), so no rebuild loop either way.
        # Materialize under NFC names. Keys synced from macOS arrive NFD
        # (decomposed Hangul); the LLM writes source_file paths in NFC, and
        # graphify drops every node whose path does not byte-match a
        # dispatched file — a 168-PDF corpus lost ALL 10,574 extracted items
        # that way. The manifest hash above still uses the raw keys, so this
        # changes nothing for change detection.
        parts = [unicodedata.normalize("NFC", p) for p in parts]
        try:
            dest = SRC.joinpath(*parts)
            if dest.exists():
                print(f"[fetch_uploads] skip: {prefix}{rel} collides with an already materialized key", file=sys.stderr)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(BUCKET, prefix + rel, str(dest))
        except Exception as exc:  # noqa: BLE001 — one bad key must not kill the corpus
            print(f"[fetch_uploads] skip unmaterializable key: {prefix}{rel} ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            continue
        downloaded += 1

    print(f"[fetch_uploads] {downloaded}/{len(entries)} file(s), {total} bytes, hash={digest[:16]}")
    if not downloaded:
        print("[fetch_uploads] nothing materialized (all keys unsafe?)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
