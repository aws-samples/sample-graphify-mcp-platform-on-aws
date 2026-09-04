"""Run `graphify extract` with markdown routed through the AST quick-scan.

graphify's CLI classifies .md/.mdx/.qmd/.skill as DOCUMENT and extracts them
only with an LLM backend; under --code-only they are skipped entirely. The
AST dispatch table (extract._DISPATCH) already carries a no-LLM markdown
quick-scan (extract_markdown: page + heading nodes, cross-file link edges),
so widening detect.CODE_EXTENSIONS routes doc files down the AST lane and the
whole CLI pipeline (dedup, cluster, export, manifest) runs unchanged. The
update is IN PLACE because analyze.py imports the set by value — a rebind
would leave it looking at the old object. Pinned to graphifyy==0.9.51
internals (same versioned-bundle contract as runtime/entrypoint.py's
_build_http_app use); revisit on any graphify version bump.

Used only for non-git (files/url) source builds — git repos keep the plain
`graphify extract --code-only` behavior and their graphs unchanged.
"""
import sys

import graphify.detect as detect

detect.CODE_EXTENSIONS.update({".md", ".mdx", ".qmd", ".skill"})

from graphify.__main__ import main

sys.argv = ["graphify", "extract", *sys.argv[1:]]
sys.exit(main())
