#!/usr/bin/env python3
r"""Apply the 禁漫天堂 (Jinmantiantang) fixes to a pinned checkout of keiyoushi/extensions-source.

Run from the root of the checked-out upstream repo:

    python3 patch-jinmantiantang.py

Upstream migrated this module to the KeiSource API (libVersion 1.6) on 2026-09-22
(keiyoushi/extensions-source#19244), which Tachimanga cannot load, so the manifest pins
it to the last revision that still uses the classic API.

1. Default domain. The classic build opens on 18comic.vip, which is now behind a
   Cloudflare challenge, so the first entry of the built-in site list - the one the
   extension uses until the reader picks another - is moved to 18comic.ink. The list is
   also refreshed at runtime from https://stevenyomi.github.io/source-domains/jmcomic.txt,
   but that host is unreachable on some networks, so the built-in default has to be
   usable on its own. This matches the fix the sibling fork carries.

2. The version code is deliberately left alone. This module is pinned, so upstream never
   moves its version, and an APK only reaches an install under a version above the
   published one - a same-version rebuild replaces the file behind a version every app is
   already sitting on and changes nothing (that is how the classic-entry fix went out and
   never arrived). The pipeline owns that rewrite now (see `republish` in
   .github/extensions.json): it raises the version of any module whose recipe changed - a
   patch like this one counts as part of the recipe - so a build that only changes
   behaviour still lands. Hard-coding a bump here would collide with it and, once the
   published version caught up to it, quietly become a no-op.
"""

import pathlib
import sys

# Upstream repo root: defaults to the current directory, or pass one as argv[1].
ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path.cwd()
MODULE = ROOT / "src/zh/jinmantiantang"
PKG = MODULE / "src/eu/kanade/tachiyomi/extension/zh/jinmantiantang"

# 1a. make 18comic.ink the site the extension starts on
OLD_SITES = '''    "18comic.vip",
    "18comic.ink",'''
NEW_SITES = '''    "18comic.ink",
    "18comic.vip",'''

# 1b. the declared base URL has to agree with the site list it starts on
OLD_BASE_URL = '        baseUrl = "https://18comic.vip"'
NEW_BASE_URL = '        baseUrl = "https://18comic.ink"'


def patch(path: pathlib.Path, pairs) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        sys.exit(f"error: {path} not found - run this from the upstream repo root")

    for old, new in pairs:
        count = text.count(old)
        if count != 1:
            sys.exit(
                f"error: expected exactly 1 occurrence in {path}, found {count}:\n  {old!r}"
            )
        text = text.replace(old, new)

    path.write_text(text, encoding="utf-8")
    print(f"patched {path} ({len(pairs)} change(s))")


patch(PKG / "Preferences.kt", [(OLD_SITES, NEW_SITES)])
patch(MODULE / "build.gradle.kts", [(OLD_BASE_URL, NEW_BASE_URL)])
