#!/usr/bin/env python3
"""Backport upstream's JWT base64url fix onto the pre-DSL Zaimanhua revision.

Run from the checked-out extensions-source root. Leave extVersionCode alone: the
publisher raises it above the currently indexed code when the recipe changes.
"""

import pathlib
import sys

root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path.cwd()
source = (root / "src/zh/zaimanhua/src/eu/kanade/tachiyomi/extension/zh/"
          "zaimanhua/Zaimanhua.kt")
old = "Base64.decode(parts[1], Base64.DEFAULT)"
new = "Base64.decode(parts[1], Base64.URL_SAFE or Base64.NO_WRAP)"
try:
    text = source.read_text(encoding="utf-8")
except FileNotFoundError:
    sys.exit(f"error: {source} not found; run from the upstream repo root")
if text.count(old) != 1:
    sys.exit(f"error: expected exactly one JWT decoder in {source}; found {text.count(old)}")
source.write_text(text.replace(old, new), encoding="utf-8")
print(f"patched {source.name} (JWT base64url decoding)")
