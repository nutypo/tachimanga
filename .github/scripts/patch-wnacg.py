#!/usr/bin/env python3
r"""Apply the WNACG fixes to a pinned checkout of keiyoushi/extensions-source.

Run from the root of the checked-out upstream repo:

    python3 patch-wnacg.py

Two changes are applied:

1. Image parsing. The pinned revision extracts image URLs from the gallery
   response with ``//\S*(jpeg|jpg|png|webp|gif)``. Because ``\S*`` is greedy
   and the alternation must end the match, this stops at the file extension and
   therefore drops the ``?verify=<timestamp>-<hmac>`` query. The wnimg CDN
   rejects unsigned URLs, so every page failed to load:

       .../0001.webp?verify=1789603200-e_pMvS7d2SXhL...  ->  200 image/webp
       .../0001.webp                                     ->  403

   This is the same problem upstream fixed in keiyoushi/extensions-source#18896
   (2026-09-11, "WNACG: ... and fix image parsing"); the replacement below is
   that fix, backported onto the last revision that still uses the classic
   (pre KeiSource-1.6) extension API, which is what Tachimanga runs.

2. Default domain list. The pinned revision defaults to wn05.ru / wn04.ru /
   wnacg05.cc, and the first two now only 301-redirect (wn05.ru -> wn07.ru ->
   wn10.cfd). The extension refreshes this list at runtime from
   https://stevenyomi.github.io/source-domains/wnacg.txt, but that host is a
   GitHub Pages address and is unreachable on some networks, so the built-in
   defaults point at domains that were live when this was written. The list
   rotates often; change DOMAINS_NEW if these stop responding.
"""

import pathlib
import sys

# Upstream repo root: defaults to the current directory, or pass one as argv[1].
ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path.cwd()
MODULE = ROOT / "src/zh/wnacg/src/eu/kanade/tachiyomi/extension/zh/wnacg"

# 1. image URL regex: keep the signed ?verify= query
OLD_REGEX = 'Regex("""//\\S*(jpeg|jpg|png|webp|gif)""")'
NEW_REGEX = (
    'Regex('
    '"""//[^\\s"\'\\\\]+\\.(?:jpeg|jpg|png|webp|gif)(?:\\?[^\\s"\'\\\\]*)?""", '
    'RegexOption.IGNORE_CASE)'
)

# 2. default domain list: use domains that currently resolve to the live site
OLD_DOMAINS = (
    'private const val DEFAULT_LIST = '
    '"https://www.wn05.ru,https://www.wn04.ru,https://www.wnacg05.cc"'
)
DOMAINS_NEW = (
    'private const val DEFAULT_LIST = "https://www.wn10.cfd,https://www.wn10.shop"'
)


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


patch(MODULE / "WNACG.kt", [(OLD_REGEX, NEW_REGEX)])
patch(MODULE / "Preferences.kt", [(OLD_DOMAINS, DOMAINS_NEW)])
