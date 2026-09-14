#!/usr/bin/env python3
"""Reviewed-build candidate: patch only the pinned graphifyy 0.9.51 serve.py.

No Graphify module import and no third-party dependencies. Running this script
with --apply-installed mutates that interpreter's installed package. The audit
uses plan_patch and patch_distribution only against temporary local fixtures.
"""
import argparse
import ast
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import stat
import tempfile

PACKAGE = 'graphifyy'
VERSION = '0.9.51'
OLD_SHA256 = '830df558a4b6843c9ad7a253cde3431379e1ecebb280a8028ba2bd3da3d56e54'
PATCHED_SHA256 = 'fc86bcfa3d014bb6f42dbed053be3c82e35ede769c20c891c3f6e0738b63e21f'
OLD_SNIPPET = "        if not matches:\n            return f\"No node matching '{label}' found.\"\n        nid, d = matches[0]\n".encode("utf-8")
NEW_SNIPPET = "        if not matches:\n            return f\"No node matching '{label}' found.\"\n        # Prefer an exact ID, then a unique exact label.\n        # Ambiguous labels retain the original substring/graph-order fallback.\n        id_matches = [item for item in matches if item[0].lower() == label]\n        exact_labels = [\n            item for item in matches\n            if (item[1].get(\"label\") or \"\").lower() == label\n        ]\n        if id_matches:\n            nid, d = id_matches[0]\n        elif label and len(exact_labels) == 1:\n            nid, d = exact_labels[0]\n        else:\n            nid, d = matches[0]\n".encode("utf-8")

class PatchRefused(RuntimeError):
    """The installed artifact is not the specifically reviewed patch target."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def plan_patch(data, version):
    """Pure byte transformation; unknown version/content is always rejected."""
    if version != VERSION:
        raise PatchRefused(f'Expected {PACKAGE}=={VERSION}; found {version!r}')
    current = sha256(data)
    if current == PATCHED_SHA256:
        if data.count(NEW_SNIPPET) != 1 or OLD_SNIPPET in data:
            raise PatchRefused('Patched artifact has unexpected snippet structure')
        return data, 'already_patched'
    if current != OLD_SHA256:
        raise PatchRefused(f'Unrecognized serve.py SHA-256: {current}')
    if data.count(OLD_SNIPPET) != 1:
        raise PatchRefused('Expected exactly one original get_node snippet')
    patched = data.replace(OLD_SNIPPET, NEW_SNIPPET, 1)
    if sha256(patched) != PATCHED_SHA256:
        raise PatchRefused('Candidate does not match the reviewed patched SHA-256')
    ast.parse(patched.decode('utf-8'), filename='graphify/serve.py')
    return patched, 'patched'


def patch_distribution(dist):
    """Patch one metadata-resolved distribution; tests supply a fake install."""
    if dist.metadata.get('Name') != PACKAGE:
        raise PatchRefused('Unexpected distribution name')
    if dist.version != VERSION:
        raise PatchRefused(f'Expected {PACKAGE}=={VERSION}; found {dist.version!r}')
    records = [p for p in (dist.files or []) if str(p) == 'graphify/serve.py']
    if len(records) != 1:
        raise PatchRefused('Distribution must own exactly one graphify/serve.py')
    root = Path(dist.locate_file('')).resolve()
    source = Path(dist.locate_file(records[0]))
    if source.is_symlink() or not source.resolve().is_relative_to(root):
        raise PatchRefused('Refusing a symlink or source outside the distribution root')
    original = source.read_bytes()
    patched, status = plan_patch(original, dist.version)
    before = source.stat()
    if status == 'already_patched':
        return {'status': status, 'path': str(source), 'version': VERSION,
                'sha256': PATCHED_SHA256, 'bytes_written': 0}
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', prefix='.get-node-',
                                         dir=source.parent, delete=False) as handle:
            pending = Path(handle.name)
            handle.write(patched)
            os.fchmod(handle.fileno(), stat.S_IMODE(before.st_mode))
            handle.flush()
            os.fsync(handle.fileno())
        current = source.stat()
        if ((current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)
                != (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                or source.read_bytes() != original):
            raise PatchRefused('Source changed during patch preparation')
        os.replace(pending, source)
        pending = None
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    if sha256(source.read_bytes()) != PATCHED_SHA256:
        raise PatchRefused('Post-write SHA-256 verification failed')
    return {'status': status, 'path': str(source), 'version': VERSION,
            'sha256': PATCHED_SHA256, 'bytes_written': len(patched)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply-installed', action='store_true',
                        help='explicitly patch this interpreter\'s installed graphifyy')
    args = parser.parse_args()
    if not args.apply_installed:
        parser.error('--apply-installed is required; no installed files were changed')
    try:
        result = patch_distribution(metadata.distribution(PACKAGE))
    except (PatchRefused, metadata.PackageNotFoundError, OSError) as exc:
        parser.exit(1, f'get_node patch refused: {exc}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
