#!/usr/bin/env python3
"""Regenerate the source tables in README.md from repo/index.min.json.

    python3 readme.py [repo-root]

The index is what the pipeline actually publishes, so the README's source tables are
derived from it rather than maintained by hand - otherwise the two drift apart the
moment a source is added, dropped or renamed. Only the two tables are rewritten;
everything else in the README is prose and is left exactly as written.

Run without arguments from the repository root, or pass the root as argv[1].
"""

from __future__ import annotations

import json
import pathlib
import sys

ENGLISH_HEADER = "| Source | Language | NSFW | URL |"
CHINESE_HEADER = "| 图源 | 语言 | NSFW | 网址 |"
SEPARATOR = "|---|---|---|---|"

# The label for a language directory. Mirrors how the README already described sources;
# the directory (not the entry's own lang, which is often "all" for visibility) is what
# decides the column.
LANG_LABELS = {
    "en": "English", "es": "Español", "zh": "中文", "ko": "한국어", "ja": "日本語",
    "fr": "Français", "pt": "Português", "id": "Indonesia", "vi": "Tiếng Việt",
    "tr": "Türkçe", "ar": "العربية", "ru": "Русский", "th": "ไทย", "all": "All",
}


def module_lang(pkg: str) -> str:
    """eu.kanade.tachiyomi.extension.zh.mangaxiaosi -> zh"""
    parts = pkg.split(".")
    return parts[-2] if len(parts) >= 2 else "all"


def is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def rows(entries: list[dict], chinese: bool) -> list[str]:
    """One markdown row per published source, ordered by package for stability."""
    out = []
    for entry in sorted(entries, key=lambda e: e["pkg"]):
        sources = entry.get("sources") or [{}]
        name = ", ".join(source.get("name") or "" for source in sources).strip(", ")
        name = name or entry.get("name", "").removeprefix("Tachiyomi: ")
        slug = entry["pkg"].rsplit(".", 1)[-1]
        # A slug in brackets is how the README told otherwise-identical-looking
        # non-Latin sources apart, so keep it for them and drop it for Latin names.
        label = name if is_ascii(name) else f"{name} ({slug})"
        lang = module_lang(entry["pkg"])
        lang = LANG_LABELS.get(lang, lang)
        nsfw = ("是" if entry.get("nsfw") else "否") if chinese else \
               ("Yes" if entry.get("nsfw") else "No")
        out.append(f"| {label} | {lang} | {nsfw} | {sources[0].get('baseUrl', '')} |")
    return out


def replace_table(lines: list[str], header: str, new_rows: list[str]) -> list[str]:
    """Swap the contiguous table that starts at `header` for a freshly generated one."""
    if header not in lines:
        raise SystemExit(f"error: the {header!r} table is not in README.md")
    start = lines.index(header)
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return lines[:start] + [header, SEPARATOR, *new_rows] + lines[end:]


def regenerate(repo: pathlib.Path) -> bool:
    """Rewrite README.md's tables from the index. True if the file changed."""
    readme = repo / "README.md"
    entries = json.loads((repo / "repo/index.min.json").read_text(encoding="utf-8"))
    text = readme.read_text(encoding="utf-8")
    lines = replace_table(text.splitlines(), ENGLISH_HEADER, rows(entries, chinese=False))
    lines = replace_table(lines, CHINESE_HEADER, rows(entries, chinese=True))
    updated = "\n".join(lines) + "\n"
    if updated == text:
        return False
    readme.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    repo = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path.cwd()
    print("README.md updated" if regenerate(repo) else "README.md already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
