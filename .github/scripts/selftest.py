#!/usr/bin/env python3
"""Check the build/publish logic that building alone cannot check.

These are the parts that decide whether an APK reaches Tachimanga: the gates, the
"should this overwrite the published version" rule, the index rewriting and the
minSdk rewrite. They are pure enough to test without a JDK, an Android SDK or
network access, so this runs before every build - a mistake in here would
otherwise only surface as a silently broken index, or an extension Tachimanga
refuses to install.

    python3 .github/scripts/selftest.py
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import build_extensions as be

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")
        FAILURES.append(label)


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def write_catalog(root: pathlib.Path, text: str) -> pathlib.Path:
    path = root / "gradle/kei.versions.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# Upstream's catalog as it stood when minSdk was raised to 26 (trimmed to the
# lines that matter), so the rewrite is checked against the real file shape.
CATALOG = """[versions]
# Used in [/gradle/build-logic/src/main/kotlin/AndroidBasePlugin.kt]
android-sdk-min = "26"
android-sdk-compile = "37"
android-sdk-target = "37"
java = "11"

[plugins]
android-base = { id = "kei.plugins.android.base" }
"""

EXPECTED_CATALOG = CATALOG.replace('android-sdk-min = "26"', 'android-sdk-min = "21"')


def test_lower_min_sdk(manifest: dict) -> None:
    cfg = manifest["targetMinSdk"]
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)

        path = write_catalog(root, CATALOG)
        check("lower_min_sdk reports the rewrite", be.lower_min_sdk(root, cfg),
              f"minSdk lowered to {cfg['value']}")
        check("lower_min_sdk rewrites only the minSdk line", path.read_text(encoding="utf-8"),
              EXPECTED_CATALOG)

        check("lower_min_sdk is idempotent", be.lower_min_sdk(root, cfg),
              f"minSdk already {cfg['value']}")
        check("lower_min_sdk leaves a lowered catalog alone",
              path.read_text(encoding="utf-8"), EXPECTED_CATALOG)

        # A revision that predates the version catalog is not an error: it already
        # builds at the minSdk we want, so the build simply proceeds.
        shutil.rmtree(root / "gradle")
        check_true("lower_min_sdk tolerates a missing catalog",
                   cfg["catalog"] in be.lower_min_sdk(root, cfg))

        # If upstream renames the key the rewrite becomes a no-op, which would make
        # every extension unloadable - so that must be reported, not ignored.
        write_catalog(root, CATALOG.replace("android-sdk-min", "android-min-sdk"))
        check_true("lower_min_sdk reports a renamed key",
                   cfg["key"] in be.lower_min_sdk(root, cfg))


def test_gates(manifest: dict) -> None:
    gates = be.effective_gates(manifest)
    target = manifest["targetMinSdk"]["value"]
    check("effective_gates caps minSdk at targetMinSdk", gates.get("maxMinSdk"), target)

    apk = REPO / "repo/apk/tachiyomi-zh.wnacg-v1.4.23.apk"
    pkg = "eu.kanade.tachiyomi.extension.zh.wnacg"
    info = {"path": str(apk), "pkg": pkg, "code": 23, "version": "1.4.23", "minSdk": target}
    check("a conforming APK passes every gate", be.gate_reasons(info, gates, pkg), [])

    # This is the failure that took the 7 track:main extensions out of the last run.
    over = be.gate_reasons({**info, "minSdk": target + 5}, gates, pkg)
    check_true("an APK above the minSdk cap is rejected", over)
    check("nothing is published for a wrong package",
          be.gate_reasons(info, gates, pkg + ".other"),
          [f"package is {pkg!r}, index expects {pkg + '.other'!r}"])


def test_module_mapping() -> None:
    check("module -> gradle task", be.gradle_task("src/zh/wnacg"), ":src:zh:wnacg:assembleRelease")
    check("module -> package", be.package_for("src/zh/wnacg"),
          "eu.kanade.tachiyomi.extension.zh.wnacg")
    try:
        be.package_for("nonsense")
        check("a malformed module is rejected", "no exception", "ValueError")
    except ValueError:
        check("a malformed module is rejected", "ValueError", "ValueError")


def test_publishing(manifest: dict) -> None:
    index_path = REPO / "repo/index.min.json"
    original = index_path.read_text(encoding="utf-8")

    # The index is committed, so re-serialising it unchanged must be byte-identical -
    # otherwise every run would produce a spurious diff (and a commit).
    entries = json.loads(original)
    check("index round-trips byte-exactly",
          json.dumps(entries, ensure_ascii=False, indent=2) + "\n", original)

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        shutil.copytree(REPO / "repo", repo / "repo")

        live = repo / "repo/apk/tachiyomi-zh.wnacg-v1.4.23.apk"
        running = be.apk_signing_fingerprint(live)
        check_true("the shipped APK has a readable signature", running)

        pkg = "eu.kanade.tachiyomi.extension.zh.wnacg"
        same = {"path": str(live), "pkg": pkg, "code": 23, "version": "1.4.23"}
        check("an unchanged build is not republished",
              be.decide_publish(repo, same, running),
              (False, "already published with this key"))

        # Rotating or losing the key would otherwise strand everyone on the old
        # signature, because Android won't update across a signing change.
        check("a build under a new key is republished",
              be.decide_publish(repo, same, "a" * 64),
              (True, "published APK was signed with a different key"))

        older = {**same, "code": 22, "version": "1.4.22"}
        check_true("an older build is never published",
                   be.decide_publish(repo, older, running)[0] is False)

        newer = {"path": str(live), "pkg": pkg, "code": 24, "version": "1.4.24"}
        check("a newer build is published",
              be.decide_publish(repo, newer, running)[1], "updates 1.4.23 -> 1.4.24")

        name = be.publish(repo, "src/zh/wnacg", newer, 3)
        check("the APK is named after the module and version",
              name, "tachiyomi-zh.wnacg-v1.4.24.apk")
        entry = next(e for e in json.loads((repo / "repo/index.min.json").read_text(encoding="utf-8"))
                     if e["pkg"] == pkg)
        check("the index entry follows the published file",
              (entry["apk"], entry["version"], entry["code"]), (name, "1.4.24", 24))
        check("the previous build is kept for rollback",
              (repo / "repo/apk/tachiyomi-zh.wnacg-v1.4.23.apk").exists(), True)
        check("nothing but the published version changes",
              sum(a != b for a, b in zip(original.splitlines(),
                                         (repo / "repo/index.min.json").read_text(encoding="utf-8").splitlines())),
              3)


def test_retention(manifest: dict) -> None:
    check("builds are ordered by version, not as text",
          be.version_key("tachiyomi-zh.x-v1.4.10.apk") > be.version_key("tachiyomi-zh.x-v1.4.9.apk"),
          True)

    cap = manifest["retention"]["keepPrevious"]
    check_true("keepPrevious leaves room to roll back", cap >= 1)

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        shutil.copytree(REPO / "repo", repo / "repo")
        current = "tachiyomi-zh.eighteenmh-v1.4.4.apk"
        before = sorted(p.name for p in (repo / "repo/apk").glob("*eighteenmh*"))

        # eighteenmh is frozen (no upstream source), so this is the one place its older
        # builds are the only rollback route - the cap must not eat the indexed build.
        removed = be.prune_old_apks(repo, "src/zh/eighteenmh", current, 3)
        check("a full history inside the cap is left alone", (removed, before), ([], before))

        removed = be.prune_old_apks(repo, "src/zh/eighteenmh", current, 1)
        check("beyond the cap the oldest builds go", removed,
              ["tachiyomi-zh.eighteenmh-v1.4.2.apk", "tachiyomi-zh.eighteenmh-v1.4.1.apk"])
        check("the published build and the cap survive",
              sorted(p.name for p in (repo / "repo/apk").glob("*eighteenmh*")),
              ["tachiyomi-zh.eighteenmh-v1.4.3.apk", current])

        check("a negative cap keeps everything",
              be.prune_old_apks(repo, "src/zh/eighteenmh", current, -1), [])

        # An unexpected filename must not make us guess which build is the oldest.
        odd = repo / "repo/apk/tachiyomi-zh.odd-vcustom.apk"
        odd.write_bytes(b"")
        check("an unreadable version stops pruning", be.prune_old_apks(repo, "src/zh/odd", "", 0), [])
        check_true("and leaves the file in place", odd.exists())


def test_manifest(manifest: dict) -> None:
    """The index and the manifest have to agree, or a build publishes nothing."""
    entries = json.loads((REPO / "repo/index.min.json").read_text(encoding="utf-8"))
    listed = {e["pkg"] for e in entries}
    for entry in manifest["extensions"]:
        check_true(f"{entry['module']} is listed in the index",
                   be.package_for(entry["module"]) in listed)
    for entry in manifest["extensions"]:
        if entry.get("patch"):
            check_true(f"{entry['module']}'s patch is committed",
                       (HERE / entry["patch"]).exists())

    # These lists are only useful while they name things that exist. Whether a path is
    # still upstream is checked by the watch pass every run (and would show up as
    # "dropped" in ci-report.json); here we only assert the offline half, which is that
    # each name is something we actually ship.
    for name in manifest["watch"].get("custom", []):
        check_true(f"{name} is one of the extensions we ship",
                   any(e["pkg"].rsplit(".", 1)[-1] == name for e in entries))


def main() -> int:
    manifest = json.loads((REPO / ".github/extensions.json").read_text(encoding="utf-8"))
    for name, fn in (("lower_min_sdk", test_lower_min_sdk),
                     ("gates", test_gates),
                     ("module mapping", lambda _m: test_module_mapping()),
                     ("publishing", test_publishing),
                     ("retention", test_retention),
                     ("manifest", test_manifest)):
        print(f"\n== {name}")
        fn(manifest)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
