#!/usr/bin/env python3
"""Copy a freshly built WNACG APK into repo/ and update repo/index.min.json.

    python3 publish-wnacg.py <upstream-root> <this-repo-root>

versionCode/versionName are read straight from the built APK when the Android
SDK's `aapt` is available (as it is on GitHub's runners); otherwise they fall
back to `extVersionCode` in the module's build.gradle plus the legacy
"1.4.<code>" version naming.

repo/index.min.json is rewritten with the same formatting it already uses
(2-space indent, literal UTF-8, trailing newline), so the commit diff stays
limited to the WNACG entry.
"""

from __future__ import annotations

import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

PKG = "eu.kanade.tachiyomi.extension.zh.wnacg"


def read_version(upstream: pathlib.Path, apk: str) -> tuple[int, str]:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        aapts = sorted(glob.glob(os.path.join(sdk, "build-tools", "*", "aapt")))
        if aapts:
            out = subprocess.run(
                [aapts[-1], "dump", "badging", apk],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            code = re.search(r"versionCode='(\d+)'", out)
            version = re.search(r"versionName='([^']+)'", out)
            if code and version:
                print(f"from APK: versionCode={code.group(1)} versionName={version.group(1)}")
                return int(code.group(1)), version.group(1)
            print("warning: aapt could not read the APK; falling back to build.gradle")

    gradle_path = upstream / "src/zh/wnacg/build.gradle"
    gradle = gradle_path.read_text(encoding="utf-8")
    match = re.search(r"extVersionCode\s*=\s*(\d+)", gradle)
    if not match:
        sys.exit(f"error: could not find extVersionCode in {gradle_path}")
    code = int(match.group(1))
    print(f"from build.gradle: extVersionCode={code} (aapt unavailable)")
    return code, f"1.4.{code}"


def main() -> None:
    upstream = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "upstream")
    repo = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "self")

    built = glob.glob(str(upstream / "src/zh/wnacg/build/outputs/apk/release/*.apk"))
    if len(built) != 1:
        sys.exit(f"error: expected exactly 1 release APK, found {built}")

    code, version = read_version(upstream, built[0])
    name = f"tachiyomi-zh.wnacg-v{version}.apk"

    apk_dir = repo / "repo/apk"
    apk_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(built[0], apk_dir / name)
    print(f"copied {built[0]} -> repo/apk/{name}")

    index_path = repo / "repo/index.min.json"
    entries = json.loads(index_path.read_text(encoding="utf-8"))
    matches = [e for e in entries if e["pkg"] == PKG]
    if len(matches) != 1:
        sys.exit(f"error: expected 1 {PKG} entry in the index, found {len(matches)}")
    entry = matches[0]
    entry["apk"] = name
    entry["code"] = code
    entry["version"] = version
    index_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"index updated: apk={name} code={code} version={version}")

    for stale in apk_dir.glob("tachiyomi-zh.wnacg-*.apk"):
        if stale.name != name:
            print(f"removed superseded repo/apk/{stale.name}")
            stale.unlink()

    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as fh:
            fh.write(f"WNACG_VERSION={version}\n")


main()
