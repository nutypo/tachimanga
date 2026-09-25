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
import os
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


def raises(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the type is the point
        return type(exc).__name__
    return "no exception"


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

    # The newer build logic names the entry class absolutely; Tachimanga prefixes the
    # package name (it has no absolute-name branch), so such an APK must not be published.
    check("a classic APK declares a relative entry class", be.apk_entry_class(apk), ".WNACG")
    new_format = REPO / "repo/apk/tachiyomi-zh.jinmantiantang-v1.4.58.apk"
    if new_format.exists():
        check("the newer build logic names the entry class absolutely",
              be.apk_entry_class(new_format), "keiyoushi.source.Generated")
        absolute = {**info, "path": str(new_format),
                    "pkg": "eu.kanade.tachiyomi.extension.zh.jinmantiantang"}
        check_true("an absolute entry class is rejected",
                   any("entry class" in reason
                       for reason in be.gate_reasons(absolute, gates, absolute["pkg"])))

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

        # Read the published state off the index instead of writing it down: publishing a
        # new version in one run would otherwise turn the next run's gate red.
        pkg = "eu.kanade.tachiyomi.extension.zh.wnacg"
        published = next(e for e in json.loads((repo / "repo/index.min.json").read_text(encoding="utf-8"))
                         if e["pkg"] == pkg)
        code, version = published["code"], published["version"]
        live = repo / "repo/apk" / published["apk"]
        running = be.apk_signing_fingerprint(live)
        check_true("the shipped APK has a readable signature", running)
        gates = be.effective_gates(manifest)

        same = {"path": str(live), "pkg": pkg, "code": code, "version": version}
        check("an unchanged build is not republished",
              be.decide_publish(repo, same, running, gates),
              (False, "already published with this key"))

        # A stricter gate (e.g. the classic entry class) has to replace an already
        # published build, which cannot wait for a version bump to be rebuilt.
        check_true("a published APK that fails the current gates is republished",
                   be.decide_publish(repo, same, running,
                                     {"requireDexSymbols": ["no/such/symbol"]})[0])

        # The same question is asked *before* a build, to decide whether the rebuild has
        # to come out at a new version. It has to be answered by the publish rule itself,
        # or the two could disagree and a rebuild would silently overwrite what is there.
        current = next(e for e in json.loads((repo / "repo/index.min.json").read_text(encoding="utf-8"))
                       if e["pkg"] == pkg)
        check("a published APK that still stands needs no new version",
              be.published_reason(repo, pkg, current, running, gates), None)
        check_true("one that no longer passes the gates does",
                   be.published_reason(repo, pkg, current, running,
                                       {"requireDexSymbols": ["no/such/symbol"]}))
        check_true("and so does one signed with another key",
                   be.published_reason(repo, pkg, current, "a" * 64, gates))
        check("an unlisted module needs no new version at all",
              be.published_reason(repo, "eu.kanade.tachiyomi.extension.zh.ghost", None,
                                  running, gates), None)

        # Rotating or losing the key would otherwise strand everyone on the old
        # signature, because Android won't update across a signing change.
        check("a build under a new key is republished",
              be.decide_publish(repo, same, "a" * 64, gates),
              (True, "published APK was signed with a different key"))

        older = {**same, "code": code - 1}
        check_true("an older build is never published",
                   be.decide_publish(repo, older, running, gates)[0] is False)

        nxt = f"{version.rsplit('.', 1)[0]}.{be.version_patch(version) + 1}"
        newer = {"path": str(live), "pkg": pkg, "code": code + 1, "version": nxt}
        check("a newer build is published",
              be.decide_publish(repo, newer, running, gates)[1], f"updates {version} -> {nxt}")

        name = be.publish(repo, "src/zh/wnacg", newer, 3)
        check("the APK is named after the module and version",
              name, f"tachiyomi-zh.wnacg-v{nxt}.apk")
        entry = next(e for e in json.loads((repo / "repo/index.min.json").read_text(encoding="utf-8"))
                     if e["pkg"] == pkg)
        check("the index entry follows the published file",
              (entry["apk"], entry["version"], entry["code"]), (name, nxt, code + 1))
        check("the previous build is kept for rollback",
              (repo / "repo/apk" / published["apk"]).exists(), True)
        check("nothing but the published version changes",
              sum(a != b for a, b in zip(original.splitlines(),
                                         (repo / "repo/index.min.json").read_text(encoding="utf-8").splitlines())),
              3)

        # A source adopted from upstream has no index entry yet, so publishing has to
        # create one from upstream's own description rather than refuse to list it.
        record = {
            "pkg": "eu.kanade.tachiyomi.extension.zh.mangaxiaosi", "name": "Manga Xiao Si",
            "libVersion": "1.4", "nsfw": True,
            "sources": [{"id": "8072816679633628898", "name": "Manga Xiao Si",
                         "lang": "zh", "url": "https://www.jjmhw2.top"}],
        }
        adopted = {"path": str(live), "pkg": record["pkg"], "code": 100001, "version": "1.4.1"}
        name = be.publish(repo, "src/zh/mangaxiaosi", adopted, 3, meta=record)
        fresh = next((e for e in json.loads((repo / "repo/index.min.json").read_text(encoding="utf-8"))
                      if e["pkg"] == record["pkg"]), {})
        check("an adopted source is appended to the index",
              (name, fresh.get("apk"), fresh.get("code"), fresh.get("version"),
               fresh.get("sources", [{}])[0].get("baseUrl")),
              ("tachiyomi-zh.mangaxiaosi-v1.4.1.apk", name, 100001, "1.4.1",
               "https://www.jjmhw2.top"))
        check("an unlisted source with no upstream metadata still stops the publish",
              raises(be.publish, repo, "src/zh/ghost",
                     {"path": str(live), "pkg": "eu.kanade.tachiyomi.extension.zh.ghost",
                      "code": 1, "version": "1.0.0"}, 3),
              "RuntimeError")


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


def test_signing(manifest: dict) -> None:
    """repo.json's fingerprint has to describe the APKs this pipeline produces.

    This deliberately does not assert that *every* indexed APK carries our key. Twelve of
    them do not until the build re-signs them, and asserting it here would fail the run
    that performs the re-signing - there is no earlier run that could fix it. The build
    audits the whole index after re-signing instead, and goes red if anything is left.
    """
    path = REPO / "repo/repo.json"
    original = path.read_text(encoding="utf-8")
    doc = json.loads(original)
    check("repo.json round-trips byte-exactly",
          json.dumps(doc, ensure_ascii=False, indent=2) + "\n", original)

    declared = doc["meta"]["signingKeyFingerprint"]
    entries = json.loads((REPO / "repo/index.min.json").read_text(encoding="utf-8"))
    buildable = {be.package_for(e["module"]) for e in manifest["extensions"]}
    keys = {be.apk_signing_fingerprint(REPO / "repo/apk" / e["apk"])
            for e in entries if e["pkg"] in buildable}
    check("everything this pipeline builds shares one key", len(keys), 1)
    check("repo.json advertises the key this pipeline signs with", declared, keys.pop())

    check("an APK carrying another key is a re-sign candidate",
          be.needs_resigning("a" * 64, declared), True)
    check("our own APK is not", be.needs_resigning(declared, declared), False)
    check("an unreadable signature is left alone", be.needs_resigning("", declared), False)

    # The keystore must come from a secret and must never live in the repository: a key
    # anyone can clone cannot back the fingerprint that identifies this repository.
    cfg = manifest["signing"]
    check("the signing config names no committed keystore", "keystoreFile" in cfg, False)
    check("no keystore is committed",
          sorted(str(p.relative_to(REPO)) for p in REPO.glob("signing*/**/*") if p.is_file()), [])
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    check("signingkey.jks could not be committed by accident",
          [line for line in ("signing/", "signingkey.jks", "signingkey.jks.b64") if line not in ignored],
          [])

    saved = os.environ.pop("SIGNING_KEY", None)
    try:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                be.ensure_keystore(cfg, be.Report())
                check("a missing SIGNING_KEY stops the run", "no error", "RuntimeError")
        except RuntimeError as exc:
            check_true("a missing SIGNING_KEY stops the run, and says how to fix it",
                       "SIGNING_KEY" in str(exc) and "new-signing-key.sh" in str(exc))
    finally:
        if saved is not None:
            os.environ["SIGNING_KEY"] = saved

    # sync_repo_fingerprint is what stops the two drifting apart again; try it on a copy.
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        shutil.copytree(REPO / "repo", repo / "repo")
        stale = repo / "repo/repo.json"
        stale.write_text(json.dumps({"meta": {"name": "x", "website": "y",
                                               "signingKeyFingerprint": "0" * 64}},
                                    indent=2) + "\n", encoding="utf-8")
        check("a stale fingerprint is corrected", be.sync_repo_fingerprint(repo, declared), True)
        check("and the correction lands in repo.json",
              json.loads(stale.read_text(encoding="utf-8"))["meta"]["signingKeyFingerprint"],
              declared)
        check("an already-correct fingerprint is left alone",
              be.sync_repo_fingerprint(repo, declared), False)



def test_discovery(manifest: dict) -> None:
    """Wanted sources are resolved against upstream's published index, not guessed at.

    The fixtures mirror what keiyoushi shipped on 2026-09-25: 漫小肆 (jjmhw) and
    肉漫屋 (rouman5) exist, the first still on the classic API, the second moved to
    KeiSource; the rest of the wanted list upstream does not carry at all.
    """
    records = [
        {"pkg": "eu.kanade.tachiyomi.extension.zh.mangaxiaosi", "name": "Manga Xiao Si",
         "libVersion": "1.4", "nsfw": True,
         "sources": [{"id": "8072816679633628898", "name": "Manga Xiao Si",
                      "lang": "zh", "url": "https://www.jjmhw2.top"}]},
        {"pkg": "eu.kanade.tachiyomi.extension.zh.roumanwu", "name": "Roumanwu",
         "libVersion": "1.6", "nsfw": True,
         "sources": [{"id": "3647420805839021718", "name": "肉漫屋",
                      "lang": "zh", "url": "https://rouman5.com"}]},
    ]
    wanted = {w["name"]: w for w in manifest["sourceDiscovery"]["wanted"]}

    status, record, _ = be.resolve_wanted(wanted["jjmhw"], records)
    assert record is not None
    check("a classic upstream module is adopted",
          (status, be.module_for_pkg(record["pkg"])),
          ("in-upstream-buildable", "src/zh/mangaxiaosi"))

    check("a module upstream moved to KeiSource is only reported",
          be.resolve_wanted(wanted["rouman5"], records)[0], "in-upstream-incompatible")
    _, by_domain, _ = be.resolve_wanted(
        {"name": "x", "url": "https://rouman5.com", "match": []}, records)
    assert by_domain is not None
    check("the domain decides the match when aliases also fit",
          by_domain["pkg"], "eu.kanade.tachiyomi.extension.zh.roumanwu")
    check("a source upstream does not carry is reported",
          be.resolve_wanted(wanted["newxtoon"], records)[0], "no-upstream-module")

    info = {"pkg": "eu.kanade.tachiyomi.extension.zh.mangaxiaosi", "code": 100001,
            "version": "1.4.1"}
    entry = be.index_entry(info, record)
    check("an adopted source gets a complete index entry",
          (entry["name"], entry["lang"], entry["code"], entry["version"], entry["nsfw"],
           entry["sources"][0]["baseUrl"], entry["sources"][0]["id"]),
          ("Tachiyomi: Manga Xiao Si", "zh", 100001, "1.4.1", 1,
           "https://www.jjmhw2.top", "8072816679633628898"))

    check("module paths round-trip through packages",
          be.module_for_pkg(be.package_for("src/ko/newxtoon")), "src/ko/newxtoon")


def test_republish(manifest: dict) -> None:
    """A rebuild has to come out at a version the apps will actually take."""
    check("the published version's last component is the floor",
          be.version_patch("1.4.59"), 59)
    check("a version with no digits has no floor", be.version_patch(""), 0)

    modern = "keiyoushi {\n    name = \"Demo\"\n    versionCode = 59\n}\n"
    legacy_gradle = "ext {\n    extVersionCode = 1\n}\n"
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        module = "src/zh/demo"
        path = root / module / "build.gradle.kts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(modern, encoding="utf-8")

        published = {"code": 104059, "version": "1.4.59"}
        check("a recipe change raises the version",
              be.bump_version_code(root, module, published),
              "versionCode 59 -> 60 in build.gradle.kts")
        check_true("and the module's build file follows",
                   "versionCode = 60" in path.read_text(encoding="utf-8"))

        # Upstream moving past the published version already makes a rebuild an update,
        # so there is nothing to rewrite.
        path.write_text(modern.replace("= 59", "= 61"), encoding="utf-8")
        check("an upstream bump is left alone", be.bump_version_code(root, module, published), None)
        check_true("and is not rewritten anyway",
                   "versionCode = 61" in path.read_text(encoding="utf-8"))

        # The legacy plugin names the same thing differently, and puts it straight into the
        # version code - so the floor is the published code, not the published version.
        legacy = root / "src/zh/legacy/build.gradle"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(legacy_gradle, encoding="utf-8")
        check("the legacy plugin's field is found too",
              be.bump_version_code(root, "src/zh/legacy", {"code": 1, "version": "1.4.1"}),
              "extVersionCode 1 -> 2 in build.gradle")

        # A module re-pinned between the two plugins is the case this exists for: its
        # published code is far above the value the legacy plugin's own history would
        # produce, and only beating that code makes the rebuild an install the apps take.
        check("a re-pinned module has to clear the published code",
              be.bump_version_code(root, "src/zh/legacy", {"code": 104060, "version": "1.4.60"}),
              "extVersionCode 2 -> 104061 in build.gradle")

        # The legacy value *is* the code, so there are no lib digits to collide with.
        big = root / "src/zh/big/build.gradle"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text("ext {\n    extVersionCode = 999\n}\n", encoding="utf-8")
        check("the legacy value is the code, so it may pass 999",
              be.bump_version_code(root, "src/zh/big", {"code": 999, "version": "1.4.999"}),
              "extVersionCode 999 -> 1000 in build.gradle")

        # Only the packed DSL value shares digits with the lib version (1.4.999 -> 104999),
        # so growing into them would collide with it.
        path.write_text(modern.replace("= 61", "= 999"), encoding="utf-8")
        check("a version that cannot rise without overflowing is refused",
              be.bump_version_code(root, module, {"code": 104999, "version": "1.4.999"}), None)
        check("a module with no version field is not guessed at",
              be.bump_version_code(root, "src/zh/absent", {"code": 1, "version": "1.4.1"}), None)

    gates = be.effective_gates(manifest)
    min_sdk = manifest["targetMinSdk"]
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        scripts = repo / ".github/scripts"
        scripts.mkdir(parents=True)
        patch = scripts / "patch-demo.py"
        patch.write_text("one", encoding="utf-8")
        (scripts / "prep.py").write_text("prep", encoding="utf-8")
        entry = {"module": "src/zh/demo", "patch": "patch-demo.py"}
        shaped = {"prep": {"patches": ["prep.py"]}}

        first = be.recipe_fingerprint(repo, shaped, entry, gates, min_sdk)
        check("the same recipe fingerprints the same",
              be.recipe_fingerprint(repo, shaped, entry, gates, min_sdk), first)
        patch.write_text("two", encoding="utf-8")
        check_true("a changed module patch changes it",
                   be.recipe_fingerprint(repo, shaped, entry, gates, min_sdk) != first)
        patch.write_text("one", encoding="utf-8")
        check("and reverting it comes back",
              be.recipe_fingerprint(repo, shaped, entry, gates, min_sdk), first)
        (scripts / "prep.py").write_text("changed", encoding="utf-8")
        check_true("a changed prep patch changes it for every module",
                   be.recipe_fingerprint(repo, shaped, entry, gates, min_sdk) != first)

        check("no recipe state yet reads as empty", be.load_recipes(repo), {})
        check_true("recording a recipe writes it", be.save_recipes(repo, {"pkg": first}))
        check("and it reads back", be.load_recipes(repo).get("pkg"), first)
        check("writing the same state again is a no-op",
              be.save_recipes(repo, {"pkg": first}), False)

    # The real case these pieces exist for: jinmantiantang is pinned to a pre-DSL revision,
    # whose build file declares the legacy `extVersionCode` - a value that *is* the version
    # code - while the index still advertises the DSL-era code. Exercise the rewrite against
    # the real index entry, reading the expectation off it rather than writing the version
    # down: publishing a new one in a run must not turn the next run's gate red.
    _, indexed = be.load_index(REPO)
    entry = next(e for e in manifest["extensions"] if e["module"] == "src/zh/jinmantiantang")
    pkg = be.package_for(entry["module"])
    current = next(e for e in indexed if e["pkg"] == pkg)
    running = be.apk_signing_fingerprint(REPO / "repo/apk" / current["apk"])
    check("jinmantiantang's published build passes the gates",
          be.published_reason(REPO, pkg, current, running, gates), None)

    with tempfile.TemporaryDirectory() as tmp:
        # The pinned revision as it arrives from upstream. Its patch leaves the version
        # alone, so the rewrite has to reach past the published code on its own.
        upstream = pathlib.Path(tmp)
        module_dir = upstream / entry["module"]
        module_dir.mkdir(parents=True)
        (module_dir / "build.gradle").write_text(
            "ext {\n    extName = 'Jinman Tiantang'\n    extVersionCode = 57\n}\n",
            encoding="utf-8")
        check("the rebuild lands above the published code",
              be.bump_version_code(upstream, entry["module"], current),
              f"extVersionCode 57 -> {current['code'] + 1} in build.gradle")


def test_readme() -> None:
    """The README tables are generated from the index, so they cannot drift from it."""
    import readme

    entries = json.loads((REPO / "repo/index.min.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "repo").mkdir()
        shutil.copyfile(REPO / "repo/index.min.json", root / "repo/index.min.json")
        # Start from deliberately stale tables: a generator that merely accepted the
        # committed file would pass here without doing anything.
        (root / "README.md").write_text(
            "# Demo\n\nprose above\n\n"
            f"{readme.ENGLISH_HEADER}\n{readme.SEPARATOR}\n| stale | x | No | http://stale |\n\n"
            f"{readme.CHINESE_HEADER}\n{readme.SEPARATOR}\n| stale | x | 否 | http://stale |\n\n"
            "prose below\n",
            encoding="utf-8",
        )

        check("regenerating rewrites the tables", readme.regenerate(root), True)
        check("regenerating again is a no-op", readme.regenerate(root), False)

        text = (root / "README.md").read_text(encoding="utf-8")
        check_true("the prose around the tables is left alone",
                   "prose above" in text and "prose below" in text and "http://stale" not in text)
        check_true("an English source is listed with its URL",
                   "| Manga18fx | English | Yes | https://manga18fx.com |" in text)
        check_true("a non-Latin source keeps its module in brackets",
                   "| 巴卡漫画 (bakamh) | 中文 | Yes | https://bakamh.com |" in text)
        check_true("the 中文 table uses 是/否",
                   "| 巴卡漫画 (bakamh) | 中文 | 是 | https://bakamh.com |" in text)

        for header in (readme.ENGLISH_HEADER, readme.CHINESE_HEADER):
            lines = text.splitlines()
            first = lines.index(header) + 2  # skip the header and the separator
            count = 0
            while first + count < len(lines) and lines[first + count].startswith("|"):
                count += 1
            check(f"every published source is listed under {header}", count, len(entries))


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

    # Wanted sources are matched by URL host and by alias, so both have to be present for
    # the resolver to be able to find a source upstream later adopts.
    for want in manifest.get("sourceDiscovery", {}).get("wanted", []):
        check_true(f"{want['name']} is a fully described wanted source",
                   bool(want.get("name") and want.get("url") and want.get("match")))

    # The global prep patches run on every checkout, so they have to be committed.
    for name in manifest.get("prep", {}).get("patches", []):
        check_true(f"the prep patch {name} is committed", (HERE / name).exists())


def main() -> int:
    manifest = json.loads((REPO / ".github/extensions.json").read_text(encoding="utf-8"))
    for name, fn in (("lower_min_sdk", test_lower_min_sdk),
                     ("gates", test_gates),
                     ("module mapping", lambda _m: test_module_mapping()),
                     ("publishing", test_publishing),
                     ("retention", test_retention),
                     ("republish", test_republish),
                     ("signing", test_signing),
                     ("discovery", test_discovery),
                     ("README", lambda _m: test_readme()),
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
