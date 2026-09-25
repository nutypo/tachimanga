#!/usr/bin/env python3
r"""Apply the 禁漫天堂 (Jinmantiantang) fixes to a pinned checkout of keiyoushi/extensions-source.

Run from the root of the checked-out upstream repo:

    python3 patch-jinmantiantang.py

This module is pinned to the last revision that predates upstream's newer build-logic DSL
(2026-07-04, #17274) - the same revision the sibling fork's working 1.4.57 build of it
comes from. Tachimanga loads the extensions this pipeline builds from a pre-DSL revision
(wnacg, and that fork's build of this one) and refuses this module's DSL build, whose
manifest declares a generated `.Generated` entry class, an absolute
`keiyoushi.source.UrlActivity` and `tachiyomix.*` meta-data - none of which a classic
build has. The pin also predates the libVersion 1.6 / KeiSource migration (2026-09-22,
#19244), which Tachimanga cannot load either.

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

# 1a. make 18comic.ink the site the extension starts on. On the pinned (pre-DSL) revision
# this array is also the only place the base URL is declared: SharedPreferences.baseUrl
# reads it by index.
OLD_SITES = '''    "18comic.vip",
    "18comic.ink",'''
NEW_SITES = '''    "18comic.ink",
    "18comic.vip",'''

# 1b. the newer DSL declares the base URL in the build file as well, and the two have to
# agree. The pinned revision predates that and has no such line, so this half is optional -
# it only keeps the patch correct if this module is ever re-pinned to a DSL revision.
OLD_BASE_URL = '        baseUrl = "https://18comic.vip"'
NEW_BASE_URL = '        baseUrl = "https://18comic.ink"'


def patch(path: pathlib.Path, pairs, required: bool = True) -> None:
    """Rewrite `pairs` in `path`, insisting each anchor appears exactly once.

    A required anchor that has moved is an error rather than a silent no-op: the point of
    the patch is that the extension comes out on a site that answers. An optional one - a
    line only the newer DSL carries - is skipped when it is not there, so this stays
    correct whether the module is pinned to a pre-DSL revision or a DSL one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            sys.exit(f"error: {path} not found - run this from the upstream repo root")
        print(f"{path.name}: absent on this revision, nothing to patch")
        return

    changed = 0
    for old, new in pairs:
        count = text.count(old)
        if count == 0 and not required:
            continue
        if count != 1:
            sys.exit(
                f"error: expected exactly 1 occurrence in {path}, found {count}:\n  {old!r}"
            )
        text = text.replace(old, new)
        changed += 1

    if not changed:
        print(f"{path.name}: nothing to patch")
        return
    path.write_text(text, encoding="utf-8")
    print(f"patched {path.name} ({changed} change(s))")


patch(PKG / "Preferences.kt", [(OLD_SITES, NEW_SITES)])
patch(MODULE / "build.gradle.kts", [(OLD_BASE_URL, NEW_BASE_URL)], required=False)
