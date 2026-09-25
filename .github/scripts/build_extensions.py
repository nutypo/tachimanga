#!/usr/bin/env python3
"""Build the extensions listed in .github/extensions.json and publish the changed ones.

Needs a JDK, the Android SDK's `aapt` and network access, so it is meant to run in
GitHub Actions. The helpers are importable so the publish/gate logic can be
exercised without the toolchain:

    python3 build_extensions.py --repo <repo-root>

Per entry it checks out its pinned ref, applies its patch, builds, and only
publishes when every gate passes and the version is not a downgrade. It also
republishes when the published APK was signed with a different key than the one
we sign with now, so switching to (or rotating) the CI keystore heals itself.

A rebuild that would come out at the version already published is raised to the
next one instead. Clients key an install off the version and hang on to what they
already downloaded, so a same-version rebuild is invisible - it only replaces the
file behind a version everyone already has. That happens whenever our own recipe
for a module changes (a patch, the minSdk, a gate: none of which move upstream's
version), and repo/recipe.json is what records it.

ci-report.json is always written at the repo root - including the tail of the
Gradle log on failure - because GitHub job logs need authentication to read, so
without it a scheduled run that breaks is a black box.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import difflib
import glob
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field

UPSTREAM_URL = "https://github.com/keiyoushi/extensions-source.git"
UPSTREAM_REPO = "keiyoushi/extensions-source"
# Upstream's published, machine-readable source list: one entry per built extension,
# with the package name and the base URL of every source it contains. Used to resolve
# the "wanted" sources in the manifest without cloning or crawling the source tree.
KEIYOUSHI_INDEX_URL = "https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.json"


@dataclass
class Result:
    module: str
    ref: str
    status: str = "skipped"  # published | up-to-date | skipped | failed
    reason: str = ""
    prep: str = ""
    pkg: str = ""
    version: str = ""
    code: int = 0
    apk: str = ""
    bumped: str = ""
    log_tail: str = ""


@dataclass
class Report:
    generatedAt: str = ""
    runUrl: str = ""
    keystore: dict = field(default_factory=dict)
    fingerprint: dict = field(default_factory=dict)
    resigned: list = field(default_factory=list)
    results: list = field(default_factory=list)
    watch: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def run(cmd: list[str], cwd: pathlib.Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{tail(proc)}")
    return proc


def tail(proc: subprocess.CompletedProcess, lines: int = 40) -> str:
    out = (proc.stdout or "") + (proc.stderr or "")
    return "\n".join(out.strip().splitlines()[-lines:])


def module_parts(module: str) -> tuple[str, str]:
    """'src/zh/wnacg' -> ('zh', 'wnacg')"""
    parts = module.split("/")
    if len(parts) != 3 or parts[0] != "src":
        raise ValueError(f"unexpected module path: {module}")
    return parts[1], parts[2]


def package_for(module: str) -> str:
    lang, name = module_parts(module)
    return f"eu.kanade.tachiyomi.extension.{lang}.{name}"


def gradle_task(module: str) -> str:
    return ":" + module.replace("/", ":") + ":assembleRelease"


def find_build_tool(tool: str) -> str | None:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        return None
    found = sorted(glob.glob(os.path.join(sdk, "build-tools", "*", tool)))
    return found[-1] if found else None


def find_aapt() -> str | None:
    return find_build_tool("aapt")


# --------------------------------------------------------------------------- #
# APK inspection - the gates run on the artifact, not on the build config
# --------------------------------------------------------------------------- #

def apk_info(apk: pathlib.Path) -> dict:
    aapt = find_aapt()
    if not aapt:
        raise RuntimeError("aapt not found (set ANDROID_HOME); cannot verify the APK")
    badging = run([aapt, "dump", "badging", str(apk)]).stdout
    info: dict = {"path": str(apk)}
    for key, pattern, cast in (
        ("pkg", r"package: name='([^']+)'", str),
        ("code", r"versionCode='(\d+)'", int),
        ("version", r"versionName='([^']+)'", str),
        ("minSdk", r"sdkVersion:'(\d+)'", int),
    ):
        match = re.search(pattern, badging)
        if match:
            info[key] = cast(match.group(1))
    return info


def apk_dex_symbols(apk: pathlib.Path, symbols: list[str]) -> list[str]:
    """Which of `symbols` appear anywhere in the APK's DEX files."""
    with zipfile.ZipFile(apk) as z:
        blob = b"".join(z.read(n) for n in z.namelist() if n.endswith(".dex"))
    return [s for s in symbols if s.encode() in blob]


def apk_signing_fingerprint(apk: pathlib.Path) -> str:
    """SHA-256 of the APK's v1 signing certificate, matching keytool's own digest."""
    try:
        with zipfile.ZipFile(apk) as z:
            cert = next(z.read(n) for n in z.namelist()
                        if n.upper().startswith("META-INF/") and n.upper().endswith(".RSA"))
    except StopIteration:
        return ""
    try:
        pem = subprocess.run(["openssl", "pkcs7", "-inform", "DER", "-print_certs"],
                             input=cert, capture_output=True, check=True).stdout
        # no text=True here: openssl gets binary DER/PEM on stdin
        result = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha256"],
                                input=pem, capture_output=True, check=True)
        out = result.stdout.decode()
    except (subprocess.CalledProcessError, OSError, ValueError):
        return ""
    return out.split("=")[-1].replace(":", "").strip().lower()


def apk_entry_class(apk: pathlib.Path) -> str:
    """The entry class from the manifest's tachiyomi.extension.class meta-data.

    Classic builds name it relative to the extension's package ('.ClassName'); keiyoushi's
    newer build logic names it absolutely ('keiyoushi.source.Generated'). Only the first
    loads in Tachimanga, which - unlike current Mihon - prefixes the package name and has
    no branch for absolute names.
    """
    aapt = find_aapt()
    if not aapt:
        return ""
    try:
        out = run([aapt, "dump", "xmltree", str(apk), "AndroidManifest.xml"]).stdout
    except (RuntimeError, OSError):
        return ""
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if "tachiyomi.extension.class" in line and index + 1 < len(lines):
            match = re.search(r'Raw: "([^"]*)"', lines[index + 1]) \
                or re.search(r'="([^"]*)"', lines[index + 1])
            if match:
                return match.group(1)
    return ""


def gate_reasons(info: dict, gates: dict, expected_pkg: str) -> list[str]:
    problems = []
    if info.get("pkg") != expected_pkg:
        problems.append(f"package is {info.get('pkg')!r}, index expects {expected_pkg!r}")
    apk = pathlib.Path(info["path"])
    for symbol in apk_dex_symbols(apk, gates.get("rejectDexSymbols", [])):
        problems.append(f"references {symbol} (needs Mihon 0.20.1+, not Tachimanga)")
    for symbol in gates.get("requireDexSymbols", []):
        if symbol not in apk_dex_symbols(apk, [symbol]):
            problems.append(f"does not reference {symbol} (not a classic-API extension?)")
    if gates.get("requireRelativeEntryClass"):
        entry = apk_entry_class(apk)
        if not entry:
            problems.append("the manifest declares no tachiyomi.extension.class")
        elif not entry.startswith("."):
            problems.append(f"entry class {entry!r} is absolute; Tachimanga resolves it "
                            "relative to the package, so only '.Class' loads")
    max_min_sdk = gates.get("maxMinSdk")
    if max_min_sdk and info.get("minSdk", 0) > max_min_sdk:
        problems.append(f"minSdk {info['minSdk']} exceeds {max_min_sdk}")
    return problems


# --------------------------------------------------------------------------- #
# repo mutation
# --------------------------------------------------------------------------- #

def load_index(repo: pathlib.Path) -> tuple[pathlib.Path, list]:
    path = repo / "repo/index.min.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def apk_pattern(module: str) -> str:
    """Filenames published for a module, e.g. tachiyomi-zh.wnacg-v1.4.23.apk.

    The `-v` is what keeps a module from matching a longer-named sibling.
    """
    lang, name = module_parts(module)
    return f"tachiyomi-{lang}.{name}-v*.apk"


def publish(repo: pathlib.Path, module: str, info: dict, keep_previous: int,
            meta: dict | None = None) -> str:
    """Copy the built APK into repo/apk/ and point the index at it.

    `meta` is only needed for a source adopted from upstream on the fly: it has no
    index entry yet, so one is created from upstream's own description of the
    extension. Everything else already has an entry the manifest and index agree on.
    """
    lang, name = module_parts(module)
    apk_dir = repo / "repo/apk"
    apk_dir.mkdir(parents=True, exist_ok=True)
    filename = f"tachiyomi-{lang}.{name}-v{info['version']}.apk"
    shutil.copyfile(info["path"], apk_dir / filename)

    index_path, entries = load_index(repo)
    matches = [e for e in entries if e["pkg"] == info["pkg"]]
    if not matches and meta:
        matches = [index_entry(info, meta)]
        entries.append(matches[0])
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly 1 {info['pkg']} entry in the index, found {len(matches)}")
    matches[0]["apk"] = filename
    matches[0]["code"] = info["code"]
    matches[0]["version"] = info["version"]
    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pattern = apk_pattern(module)
    previous = sorted(p.name for p in apk_dir.glob(pattern) if p.name != filename)
    for removed in prune_old_apks(repo, module, filename, keep_previous):
        previous.remove(removed)
        print(f"   dropped old build {removed}")
    if previous:
        # Kept deliberately: rolling back to one of these is a one-line index change,
        # which matters most for the extensions this pipeline cannot rebuild.
        print(f"   keeping previous build(s) {', '.join(previous)}")
    return filename


def index_entry(info: dict, meta: dict) -> dict:
    """A brand-new index entry for a source adopted from upstream's published index.

    The shape matches the entries the repository already ships, so the app cannot tell
    an adopted source from a hand-listed one. Fields that come from our build (apk,
    code, version) are overwritten by publish().
    """
    sources = [
        {
            "name": source.get("name") or meta.get("name", ""),
            "lang": source.get("lang") or meta.get("lang", "all"),
            "id": str(source.get("id", "")),
            "baseUrl": source.get("url") or "",
        }
        for source in meta.get("sources", [])
    ]
    lang = meta.get("lang") or (sources[0]["lang"] if sources else "all")
    return {
        "name": f"Tachiyomi: {meta.get('name', '')}",
        "pkg": info["pkg"],
        "apk": "",
        "lang": lang,
        "code": info["code"],
        "version": info["version"],
        "nsfw": 1 if meta.get("nsfw") else 0,
        "sources": sources,
    }


def version_key(filename: str) -> tuple:
    """Order builds by the version in their filename, so v1.4.10 outranks v1.4.9."""
    match = re.search(r"-v([0-9][0-9A-Za-z.]*)\.apk$", filename)
    if not match:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", match.group(1)))


def prune_old_apks(repo: pathlib.Path, module: str, current: str, keep: int) -> list[str]:
    """Delete previous builds beyond the rollback depth we promise to keep.

    A negative `keep` keeps everything. Nothing is ever deleted unless every
    candidate's version parses and the currently published build is excluded, so an
    unexpected filename leaves the directory alone rather than guessing at the order.
    """
    if keep < 0:
        return []
    builds = [p for p in (repo / "repo/apk").glob(apk_pattern(module))
              if p.name != current]
    keys = {p.name: version_key(p.name) for p in builds}
    if any(not key for key in keys.values()):
        print(f"   keeping {len(builds)} old build(s): version could not be compared")
        return []
    builds.sort(key=lambda p: keys[p.name], reverse=True)
    removed = []
    for stale in builds[keep:]:
        stale.unlink()
        removed.append(stale.name)
    return removed


def decide_publish(repo: pathlib.Path, info: dict, keystore_fp: str,
                   gates: dict | None = None) -> tuple[bool, str]:
    """Publish unless the index already advertises this build, signed with our key.

    `gates` lets a stricter gate replace a build that is already out there: a policy
    change (like requiring a classic entry class) invalidates published APKs without
    changing their version, so they would otherwise never be rebuilt.
    """
    _, entries = load_index(repo)
    current = next((e for e in entries if e["pkg"] == info["pkg"]), None)
    if current is None:
        return True, "not listed in the index yet"
    if info["code"] < current.get("code", 0):
        return False, (f"built {info['version']} (code {info['code']}) is older than the "
                       f"published {current.get('version')} (code {current.get('code')})")
    if info["code"] > current.get("code", 0):
        return True, f"updates {current.get('version')} -> {info['version']}"
    published = repo / "repo/apk" / str(current.get("apk", ""))
    if not published.exists():
        return True, "published APK is missing"
    ours = apk_signing_fingerprint(published)
    if not keystore_fp or not ours:
        return False, "already published (signatures could not be compared)"
    if ours != keystore_fp:
        return True, "published APK was signed with a different key"
    if gates is not None and find_aapt():
        try:
            probe = {**apk_info(published), "path": str(published), "pkg": info["pkg"]}
            stale = gate_reasons(probe, gates, info["pkg"])
        except (RuntimeError, OSError):
            stale = []
        if stale:
            return True, f"published APK no longer passes the gates ({stale[0]})"
    return False, "already published with this key"


def published_reason(repo: pathlib.Path, pkg: str, current: dict | None, keystore_fp: str,
                     gates: dict | None) -> str | None:
    """Why the published APK cannot simply stand, or None if it can.

    Answered by the publish rule itself - the APK that is already out there is put
    through decide_publish as if it were a fresh build at its own version - so this
    can never disagree with what publishing later concludes.
    """
    if current is None:
        return None
    same = {"path": str(repo / "repo/apk" / str(current.get("apk", ""))), "pkg": pkg,
            "code": current.get("code", 0), "version": current.get("version", "")}
    should, why = decide_publish(repo, same, keystore_fp, gates)
    return why if should else None


# --------------------------------------------------------------------------- #
# republishing at a new version
# --------------------------------------------------------------------------- #


def version_patch(version: str) -> int:
    """The last component of a version name - the floor a rebuilt version must beat."""
    parts = re.findall(r"\d+", version or "")
    return int(parts[-1]) if parts else 0


def recipe_fingerprint(repo: pathlib.Path, manifest: dict, entry: dict, gates: dict,
                       min_sdk: dict) -> str:
    """Everything about *how* we build a module that can change what comes out.

    Upstream's version only moves when upstream edits the module. Our own recipe - the
    patches we apply to it, the minSdk it is built at, the rules it has to satisfy -
    changes far more often, and none of that appears in the version. Hashing it is what
    lets a run tell that a published APK was built some other way and has to be replaced
    under a version clients will actually take.
    """
    parts = [entry["module"], str(entry.get("ref") or entry.get("track", "main")),
             json.dumps(gates, sort_keys=True), str(min_sdk["value"])]
    for name in [entry.get("patch")] + list(manifest.get("prep", {}).get("patches", [])):
        if not name:
            continue
        path = repo / ".github/scripts" / name
        parts.append(f"{name}:{path.read_text(encoding='utf-8') if path.exists() else 'missing'}")
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


def load_recipes(repo: pathlib.Path) -> dict:
    """pkg -> the recipe its published APK was built with. Empty if there is no state yet."""
    try:
        doc = json.loads((repo / "repo/recipe.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_recipes(repo: pathlib.Path, recipes: dict) -> bool:
    path = repo / "repo/recipe.json"
    text = json.dumps(recipes, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def bump_version_code(upstream: pathlib.Path, module: str, floor: int) -> str | None:
    """Raise a module's version so a rebuild is a version clients will take.

    The last component of a version is declared in the module's own build file - as
    `versionCode` under the current plugin and `extVersionCode` under the legacy one -
    and the build turns it into both the name and the (much larger) code. `floor` is
    that component as published, so the new value always lands above it; upstream having
    already moved past the published version needs no help and is left alone.

    Returns a note describing the rewrite, or None when there is nothing to change.
    """
    for name in ("build.gradle.kts", "build.gradle"):
        path = upstream / module / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        match = re.search(r"^(?P<indent>\s*)(?P<key>extVersionCode|versionCode)\s*=\s*"
                          r"(?P<num>\d+)\s*$", text, flags=re.MULTILINE)
        if not match:
            return None
        current = int(match.group("num"))
        if current > floor:
            return None
        # The component shares the version code with the lib version (1.4.59 -> 104059),
        # so it cannot grow into those digits - upstream simply has to have moved on.
        if floor + 1 >= 1000:
            return None
        want = floor + 1
        updated = (text[:match.start()]
                   + f'{match.group("indent")}{match.group("key")} = {want}'
                   + text[match.end():])
        path.write_text(updated, encoding="utf-8")
        return f"{match.group('key')} {current} -> {want} in {name}"
    return None


# --------------------------------------------------------------------------- #
# keystore
# --------------------------------------------------------------------------- #

def ensure_keystore(cfg: dict, report: Report) -> tuple[pathlib.Path, dict]:
    """The signing keystore, which has to come from the SIGNING_KEY secret.

    It is deliberately never read from, or written to, the repository. A keystore
    committed to a public repository is readable by anyone who can clone it, which makes
    the signingKeyFingerprint it is meant to back worthless - anyone could sign an APK
    that matches it. Generating one here would be worse still: it would silently change
    the signature, and Android will not update an installed extension across a change of
    signing key.
    """
    alias = os.environ.get("ALIAS") or cfg["alias"]
    store_pw = os.environ.get("KEY_STORE_PASSWORD") or cfg["storePassword"]
    key_pw = os.environ.get("KEY_PASSWORD") or cfg["keyPassword"]

    secret = (os.environ.get("SIGNING_KEY") or "").strip()
    if not secret:
        raise RuntimeError(
            "SIGNING_KEY is not set, so there is no keystore to sign with.\n"
            "  It must be a repository secret holding the base64 of a keystore, e.g.\n"
            "    .github/scripts/new-signing-key.sh          # prints it, and a fingerprint\n"
            "    gh secret set SIGNING_KEY --repo <owner>/<repo> < signingkey.b64\n"
            "  The build refuses to run without it: upstream falls back to the debug key\n"
            "  when signingkey.jks is missing, which would publish extensions signed with\n"
            "  a key nobody controls and that nobody could update later."
        )
    path = pathlib.Path("/tmp/signingkey.jks")
    path.write_bytes(base64.b64decode(secret))

    report.keystore = {"source": "SIGNING_KEY secret", "alias": alias}
    return path, {"ALIAS": alias, "KEY_STORE_PASSWORD": store_pw, "KEY_PASSWORD": key_pw}


def keystore_fingerprint(path: pathlib.Path, alias: str, store_pw: str) -> str:
    try:
        out = run(["keytool", "-list", "-v", "-keystore", str(path), "-alias", alias,
                   "-storepass", store_pw]).stdout
    except (RuntimeError, OSError):
        return ""
    match = re.search(r"SHA256: ([0-9A-Fa-f:]+)", out)
    return match.group(1).replace(":", "").lower() if match else ""


def needs_resigning(current: str, ours: str) -> bool:
    """Whether an APK carries a signature we can read that is not the one we sign with.

    An unreadable signature is left alone rather than guessed at: it means we could
    not compare it, not that it is wrong.
    """
    return bool(current) and bool(ours) and current != ours


def fingerprint_audit(repo: pathlib.Path, ours: str) -> tuple[list[str], list[str]]:
    """Split the indexed APKs into those signed with another key, and those we cannot read."""
    _, entries = load_index(repo)
    mismatched, unreadable = [], []
    for entry in entries:
        apk = repo / "repo/apk" / entry["apk"]
        if not apk.exists():
            continue
        fingerprint = apk_signing_fingerprint(apk)
        if not fingerprint:
            unreadable.append(apk.name)
        elif needs_resigning(fingerprint, ours):
            mismatched.append(apk.name)
    return mismatched, unreadable


def resign_apks(repo: pathlib.Path, keystore: pathlib.Path, signing_env: dict,
                ours: str, report: Report) -> int:
    """Re-sign published APKs that carry another key, leaving everything else untouched.

    These are the extensions whose upstream moved to the incompatible API (or vanished),
    so they can never be rebuilt - but repo.json advertises a single
    signingKeyFingerprint, and that claim should hold for every APK in the index.
    Re-signing does not recompile anything: only the signature changes, and Tachimanga
    loads extensions out of the APK rather than installing it as a package, so an
    extension that loaded before still loads.
    """
    apksigner = find_build_tool("apksigner")
    zipalign = find_build_tool("zipalign")
    if not (apksigner and zipalign):
        print("   apksigner/zipalign not in the SDK (set ANDROID_HOME); cannot re-sign")
        return 0

    _, entries = load_index(repo)
    signed = 0
    for entry in entries:
        apk = repo / "repo/apk" / entry["apk"]
        if not apk.exists():
            continue
        current = apk_signing_fingerprint(apk)
        if not needs_resigning(current, ours):
            continue
        name = entry["pkg"].rsplit(".", 1)[-1]
        # Sign into a staging file and only put it in place once it verifies, so a
        # failure can never leave a half-written APK in the index.
        staged = pathlib.Path(f"/tmp/resigned-{name}.apk")
        try:
            run([zipalign, "-f", "-p", "4", str(apk), str(staged)])
            run([apksigner, "sign", "--ks", str(keystore),
                 "--ks-key-alias", signing_env["ALIAS"],
                 "--ks-pass", f"pass:{signing_env['KEY_STORE_PASSWORD']}",
                 "--key-pass", f"pass:{signing_env['KEY_PASSWORD']}",
                 str(staged)])
        except (RuntimeError, OSError) as exc:
            report.resigned.append({"apk": apk.name, "status": "failed", "detail": str(exc)})
            print(f"   {name}: could not re-sign, left as it was")
            continue
        if apk_signing_fingerprint(staged) != ours or not zipfile.is_zipfile(staged):
            report.resigned.append({"apk": apk.name, "status": "failed",
                                    "detail": "re-signed APK did not verify"})
            print(f"   {name}: re-signed APK did not verify, left as it was")
            continue
        shutil.copyfile(staged, apk)
        staged.unlink()
        signed += 1
        report.resigned.append({"apk": apk.name, "status": "signed", "was": current[:12]})
        print(f"   {name}: re-signed ({current[:12]} -> {ours[:12]})")
    return signed


def sync_repo_fingerprint(repo: pathlib.Path, ours: str) -> bool:
    """Point repo.json at the key we sign with, so the two cannot drift apart."""
    path = repo / "repo/repo.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc["meta"].get("signingKeyFingerprint") == ours:
        return False
    doc["meta"]["signingKeyFingerprint"] = ours
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


# --------------------------------------------------------------------------- #
# upstream
# --------------------------------------------------------------------------- #

def clone_upstream(ref: str, workdir: pathlib.Path) -> pathlib.Path:
    slug = "main" if ref == "main" else ref[:12]
    dest = workdir / f"upstream-{slug}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--filter=blob:none", "--quiet", UPSTREAM_URL, str(dest)])
    run(["git", "-C", str(dest), "checkout", "--force", "--quiet", ref])
    return dest


def ensure_upstream_tree(workdir: pathlib.Path) -> pathlib.Path:
    """Upstream main as a browsable tree, reusing the build loop's clone if there is one.

    The watch pass only needs paths, so a --no-checkout clone is enough: it fetches
    trees without any of the extension sources.
    """
    dest = workdir / "upstream-main"  # same path clone_upstream uses for ref main
    if (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "1",
         "--quiet", UPSTREAM_URL, str(dest)])
    return dest


def upstream_modules(upstream: pathlib.Path) -> list[str]:
    """Every extension module in upstream's tree, e.g. src/zh/wnacg."""
    listed = run(["git", "-C", str(upstream), "ls-tree", "-r", "--name-only", "HEAD", "src"]).stdout
    build_files = [p for p in listed.splitlines()
                   if p.endswith(("build.gradle.kts", "build.gradle"))]
    return sorted({p.rsplit("/", 1)[0] for p in build_files})


def reset_module(upstream: pathlib.Path, ref: str, module: str) -> None:
    """Drop a previous entry's patch and build outputs before building again."""
    subprocess.run(["git", "-C", str(upstream), "checkout", "--force", "--quiet", ref, "--", module],
                   capture_output=True, check=False)
    subprocess.run(["git", "-C", str(upstream), "clean", "-fdxq", "--", module],
                   capture_output=True, check=False)


def lower_min_sdk(upstream: pathlib.Path, cfg: dict) -> str:
    """Rewrite the build's minSdk to the one Tachimanga supports.

    Upstream raised it to 26, which Tachimanga cannot load, so this is the
    "lower minSdk to 21 and rebuild" step its own error message asks for.
    """
    path = upstream / cfg["catalog"]
    if not path.exists():
        return f"{cfg['catalog']} not found"
    text = path.read_text(encoding="utf-8")
    pattern = rf'^({re.escape(cfg["key"])}\s*=\s*)"[^"]*"'
    updated, count = re.subn(pattern, rf'\g<1>"{cfg["value"]}"', text, flags=re.MULTILINE)
    if count == 0:
        return f"{cfg['key']} not present in {cfg['catalog']}"
    if updated == text:
        return f"minSdk already {cfg['value']}"
    path.write_text(updated, encoding="utf-8")
    return f"minSdk lowered to {cfg['value']}"


def fetch_text(url: str) -> str | None:
    """GET a URL, or None if it is unreachable / not there."""
    try:
        return urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def newest_commit(module: str) -> dict:
    url = (f"https://api.github.com/repos/{UPSTREAM_REPO}/commits"
           f"?path={module}&per_page=1")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
        commit = data[0]
    except (urllib.error.URLError, OSError, ValueError, IndexError, KeyError) as exc:
        return {"error": str(exc)}
    return {"sha": commit["sha"][:12],
            "date": commit["commit"]["committer"]["date"][:10],
            "message": commit["commit"]["message"].splitlines()[0][:100]}


def module_config(module: str, ref: str = "main") -> dict:
    """A module's upstream build config: does it exist, which libVersion does it use."""
    info: dict = {"module": module, "ref": ref}
    for build_file in ("build.gradle.kts", "build.gradle"):
        text = fetch_text(f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{ref}/{module}/{build_file}")
        if text is None:
            continue
        info["buildFile"] = build_file
        info["legacyPlugin"] = "kei.plugins.extension.legacy" in text
        match = re.search(r'libVersion\s*=\s*"([^"]+)"', text)
        if match:
            info["libVersion"] = match.group(1)
        return info
    info["missing"] = True
    return info


# --------------------------------------------------------------------------- #
# wanted sources - adopt what upstream builds, without listing it by hand
# --------------------------------------------------------------------------- #


def keiyoushi_index(url: str = KEIYOUSHI_INDEX_URL) -> list[dict]:
    """Upstream's published source list, flattened to one record per extension.

    Returns [] if the index is unreachable or malformed, which is reported to the
    caller rather than treated as "upstream has nothing".
    """
    text = fetch_text(url)
    if not text:
        return []
    try:
        doc = json.loads(text)
    except ValueError:
        return []
    records = []
    for ext in doc.get("extensionList", {}).get("extensions", []):
        pkg = ext.get("packageName", "")
        if not pkg:
            continue
        records.append({
            "pkg": pkg,
            "name": ext.get("name", ""),
            "libVersion": str(ext.get("extensionLib", "")),
            "nsfw": ext.get("contentWarning") == "CONTENT_WARNING_NSFW",
            "sources": [
                {"id": s.get("id"), "name": s.get("name"),
                 "lang": s.get("language"), "url": s.get("homeUrl")}
                for s in ext.get("sources", [])
            ],
        })
    return records


def module_for_pkg(pkg: str) -> str:
    """eu.kanade.tachiyomi.extension.zh.mangaxiaosi -> src/zh/mangaxiaosi"""
    parts = pkg.split(".")
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        raise ValueError(f"unexpected package: {pkg}")
    return f"src/{parts[-2]}/{parts[-1]}"


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url or "").netloc.lower()


def resolve_wanted(wanted: dict, records: list[dict]) -> tuple[str, dict | None, str]:
    """Find the upstream extension for a wanted source, and whether we can build it.

    A match is either a host match against the source's advertised base URL (strongest,
    so a domain we already know pins the right extension), or one of the entry's
    `match` aliases appearing in the package name or a source name. Anything upstream
    still ships on the classic API (libVersion 1.4) is buildable; 1.6 is KeiSource and
    will not load in Tachimanga, so it is only ever reported.
    """
    host = _host(wanted.get("url", ""))
    keys = [str(key).lower() for key in wanted.get("match", []) if key]
    scored = []
    for record in records:
        slug = record["pkg"].rsplit(".", 1)[-1].lower()
        names = [record["name"].lower()]
        names += [str(s.get("name") or "").lower() for s in record["sources"]]
        hosts = [_host(s.get("url", "")) for s in record["sources"]]
        if host and any(host == h or host.endswith("." + h) or h.endswith("." + host)
                        for h in hosts if h):
            score = 2
        elif any(key in slug or any(key in name for name in names) for key in keys):
            score = 1
        else:
            continue
        scored.append((score, record))
    if not scored:
        return "no-upstream-module", None, f"not in keiyoushi's index ({wanted.get('url', '')})"
    scored.sort(key=lambda item: -item[0])
    classic = next((record for _, record in scored if record["libVersion"] == "1.4"), None)
    if classic:
        return "in-upstream-buildable", classic, f"keiyoushi has {classic['pkg']} on libVersion 1.4"
    record = scored[0][1]
    return ("in-upstream-incompatible", record,
            f"keiyoushi has {record['pkg']} but on libVersion {record['libVersion']}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def effective_gates(manifest: dict) -> dict:
    """The gates actually applied, which include the minSdk we rewrite builds to.

    Keeping the two together means the gate can never drift from the value the
    build was told to use.
    """
    return {**manifest["gates"], "maxMinSdk": manifest["targetMinSdk"]["value"]}


def apply_prep_patches(upstream: pathlib.Path, manifest: dict, repo: pathlib.Path) -> None:
    """Run the manifest's global prep patches against a fresh upstream checkout.

    Unlike a module's own `patch`, these fix something that has to hold for every module
    built from this checkout - e.g. making the generated entry class loadable on
    Tachimanga - so they are applied once per clone rather than per module.
    """
    for name in manifest.get("prep", {}).get("patches", []):
        script = repo / ".github/scripts" / name
        try:
            run(["python3", str(script), str(upstream)])
        except RuntimeError as exc:
            # The gate rejects whatever a failed prep leaves behind, so a moved anchor
            # shows up as skipped extensions rather than as silently broken publishes.
            print(f"   prep patch {name} did not apply: {exc}")


def build_entry(entry: dict, upstream: pathlib.Path, ref: str, repo: pathlib.Path,
                manifest: dict, gates: dict, min_sdk: dict, keep_previous: int,
                signing_env: dict, keystore_fp: str, recipes: dict) -> tuple[Result, str]:
    """Build one manifest entry and publish it if it passes the gates."""
    module = entry["module"]
    result = Result(module=module, ref=ref)
    try:
        reset_module(upstream, ref, module)
        if entry.get("patch"):
            patch = repo / ".github/scripts" / entry["patch"]
            run(["python3", str(patch), str(upstream)])
        result.prep = lower_min_sdk(upstream, min_sdk)

        # Decide the version before building, not after: a rebuild that would land on the
        # version already published has to come out higher, and finding that out once the
        # APK exists would mean building it twice. Two things force it - a change to our
        # own recipe for the module, and the published APK turning out to be one that can
        # no longer stand (gone, another key, or failing the gates). A module that is not
        # published yet needs neither: its build is a genuine first release.
        pkg = package_for(module)
        _, entries = load_index(repo)
        current = next((e for e in entries if e["pkg"] == pkg), None)
        fingerprint = recipe_fingerprint(repo, manifest, entry, gates, min_sdk)
        if current is not None and manifest.get("republish", {}).get("onRecipeChange", True):
            stale = ("our recipe for it changed"
                     if recipes.get(pkg) != fingerprint
                     else published_reason(repo, pkg, current, keystore_fp, gates))
            if stale:
                note = bump_version_code(upstream, module, version_patch(current.get("version", "")))
                if note:
                    result.bumped = f"{note} ({stale})"
                    print(f"   republishing at a new version: {result.bumped}")

        print(f"   building {gradle_task(module)}")
        build = subprocess.run(
            ["./gradlew", gradle_task(module), "--console=plain"],
            cwd=upstream, capture_output=True, text=True, check=False,
            env={**os.environ, **signing_env},
        )
        if build.returncode != 0:
            result.status = "failed"
            result.reason = f"gradle exited {build.returncode}"
            result.log_tail = tail(build)
            return result, "failed"

        built = glob.glob(str(upstream / module / "build/outputs/apk/release/*.apk"))
        if len(built) != 1:
            raise RuntimeError(f"expected exactly 1 release APK, found {built}")
        info = apk_info(pathlib.Path(built[0]))
        result.pkg = info.get("pkg", "")
        result.version = info.get("version", "")
        result.code = info.get("code", 0)

        problems = gate_reasons(info, gates, package_for(module))
        if problems:
            result.status = "skipped"
            result.reason = "; ".join(problems)
            return result, "skipped"

        should, why = decide_publish(repo, info, keystore_fp, gates)
        if not should:
            result.status = "up-to-date"
            result.reason = why
            # Only bank the recipe once it is what is published. A build that was bumped
            # and still did not get published has to be tried again, not recorded as done.
            if not result.bumped:
                recipes[info["pkg"]] = fingerprint
            return result, "up-to-date"

        result.apk = publish(repo, module, info, keep_previous, entry.get("_discovered"))
        recipes[info["pkg"]] = fingerprint
        result.status = "published"
        result.reason = why + (f"; {result.bumped}" if result.bumped else "")
        return result, "published"
    except Exception as exc:  # noqa: BLE001 - one entry must not kill the run
        result.status = "failed"
        result.reason = f"{type(exc).__name__}: {exc}"
        return result, "failed"


def write_report(repo: pathlib.Path, report: Report) -> None:
    """Record what happened, including the reason a run stopped early.

    GitHub's own job logs need authentication to read, so this file is the audit trail.
    """
    (repo / "ci-report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--workdir", default="/tmp/keiyoushi-builds")
    parser.add_argument("--only", default="", help="build a single module")
    parser.add_argument("--watch-only", action="store_true")
    args = parser.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    workdir = pathlib.Path(args.workdir)
    manifest = json.loads((repo / ".github/extensions.json").read_text(encoding="utf-8"))

    report = Report(
        generatedAt=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        runUrl=(f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
                f"{os.environ.get('GITHUB_RUN_ID', '')}"),
    )

    try:
        keystore, signing_env = ensure_keystore(manifest["signing"], report)
    except RuntimeError as exc:
        # Bail before building anything: without a keystore the build would still succeed,
        # signed with the debug key, and publish extensions that can never be updated.
        print(f"== {exc}")
        report.keystore = {"source": "missing", "error": str(exc)}
        write_report(repo, report)
        return 1

    keystore_fp = keystore_fingerprint(keystore, signing_env["ALIAS"],
                                      signing_env["KEY_STORE_PASSWORD"])
    if not keystore_fp:
        print("== the signing keystore could not be read (wrong format, or the alias or "
              "password does not match); refusing to sign")
        report.keystore = {"source": "unreadable", "alias": signing_env["ALIAS"]}
        write_report(repo, report)
        return 1
    report.keystore["sha256"] = keystore_fp

    min_sdk = manifest["targetMinSdk"]
    gates = effective_gates(manifest)
    # Absent key keeps every previous build, which is what this did before it was capped.
    keep_previous = manifest.get("retention", {}).get("keepPrevious", -1)
    # What each published module was built with, so a change to our recipe for one is
    # visible. A module missing from it - including every module the first time this ran -
    # counts as changed, which is what flushes builds that are already out there.
    recipes = load_recipes(repo)

    entries = list(manifest["extensions"])
    if args.only:
        entries = [e for e in entries if e["module"] == args.only]

    # "Wanted" sources are never listed by hand: each run resolves them against
    # upstream's published index and adopts any it ships on the classic API, so a source
    # upstream adds or fixes is built and published without a manifest edit. Ones we
    # only want to watch are reported instead. Skipped for a single-module run.
    discovery = manifest.get("sourceDiscovery") or {}
    wanted = discovery.get("wanted") or []
    if wanted and not args.only:
        records = keiyoushi_index(discovery.get("index", KEIYOUSHI_INDEX_URL))
        print(f"== wanted sources ({len(wanted)}; upstream index: "
              f"{len(records)} extension(s))")
        if not records:
            report.watch.append({"module": "upstream-index", "status": "unavailable",
                                 "detail": "could not fetch keiyoushi's published index"})
        adopted = {e["module"] for e in entries}
        for want in wanted:
            if not records:
                report.watch.append({"module": want["name"], "status": "unknown",
                                     "detail": "upstream index unavailable this run"})
                continue
            status, record, detail = resolve_wanted(want, records)
            print(f"   {want['name']}: {status} - {detail}")
            report.watch.append({"module": want["name"], "status": status, "detail": detail})
            if status == "in-upstream-buildable" and record and not args.watch_only:
                module = module_for_pkg(record["pkg"])
                if module not in adopted:
                    entries.append({"module": module, "track": "main", "_discovered": record})
                    adopted.add(module)

    failures = 0
    if not args.watch_only and entries:
        by_ref: dict[str, list] = {}
        for entry in entries:
            ref = entry.get("ref") or entry.get("track", "main")
            by_ref.setdefault(ref, []).append(entry)

        for ref, group in by_ref.items():
            upstream = clone_upstream(ref, workdir)
            shutil.copyfile(keystore, upstream / "signingkey.jks")
            apply_prep_patches(upstream, manifest, repo)
            print(f"== upstream {ref} ({len(group)} extension(s))")
            for entry in group:
                print(f"-> {entry['module']}")
                result, outcome = build_entry(entry, upstream, ref, repo, manifest, gates,
                                              min_sdk, keep_previous, signing_env,
                                              keystore_fp, recipes)
                failures += outcome == "failed"
                report.results.append(asdict(result))
                print(f"   {outcome}: {result.reason}"
                      + (f" -> {result.apk}" if result.apk else "")
                      + (f" [{result.prep}]" if result.prep else ""))

    # Signing is one unit of work: replace any APK still carrying another key, then only
    # let repo.json claim a key that every APK in the index actually has. Skipped for a
    # single-module or watch-only run, where auditing the whole repo would be a surprise.
    if not args.watch_only and not args.only:
        print("== signing")
        resigned = resign_apks(repo, keystore, signing_env, keystore_fp, report)
        print(f"   {resigned} APK(s) re-signed")
        mismatched, unreadable = fingerprint_audit(repo, keystore_fp)
        report.fingerprint = {"expected": keystore_fp, "mismatched": mismatched,
                              "unreadable": unreadable}
        if mismatched:
            failures += 1
            print(f"   {len(mismatched)} APK(s) still carry another key: {', '.join(mismatched)}")
        elif unreadable:
            # Not a mismatch, just something we could not compare - worth saying out loud,
            # but it should not hold the index back.
            print(f"   {len(unreadable)} APK(s) have no readable v1 signature: {', '.join(unreadable)}")
        if not mismatched:
            if sync_repo_fingerprint(repo, keystore_fp):
                print(f"   repo.json now advertises {keystore_fp[:12]}")
                report.fingerprint["repoJson"] = "updated"
            else:
                report.fingerprint["repoJson"] = "already current"

    # watch pass: no builds, just early warning about upstream moving under us
    for module in manifest["watch"].get("incompatible", []):
        cfg = module_config(module)
        if cfg.get("missing"):
            report.watch.append({"module": module, "status": "dropped",
                                 "detail": "no longer in upstream main"})
        elif cfg.get("legacyPlugin") or cfg.get("libVersion") == "1.4":
            report.watch.append({"module": module, "status": "CHANGED",
                                 "detail": f"now classic API (libVersion {cfg.get('libVersion')}); "
                                           "add it to `extensions` to build it"})
        else:
            report.watch.append({"module": module, "status": "still-incompatible",
                                 "detail": f"libVersion {cfg.get('libVersion')}"})

    for module in manifest["watch"].get("dropped", []):
        cfg = module_config(module)
        report.watch.append({"module": module,
                             "status": "returned" if not cfg.get("missing") else "still-dropped",
                             "detail": "checked upstream main"})

    for entry in [e for e in entries if e.get("ref")]:
        report.watch.append({"module": entry["module"], "status": "pinned",
                             "detail": f"pinned at {entry['ref'][:12]}",
                             "newestUpstreamCommit": newest_commit(entry["module"])})

    # Custom sources were built by hand and have no upstream module to track, so the
    # only real news is upstream having picked one up - we could then build it from
    # source instead of shipping an APK that can never be updated again.
    modules = upstream_modules(ensure_upstream_tree(workdir))
    by_name = {module.rsplit("/", 1)[-1]: module for module in modules}
    _, indexed = load_index(repo)
    for name in manifest["watch"].get("custom", []):
        entry = next((e for e in indexed if e["pkg"].rsplit(".", 1)[-1] == name), None)
        upstream_module = by_name.get(name)
        if upstream_module:
            cfg = module_config(upstream_module)
            buildable = cfg.get("legacyPlugin") or cfg.get("libVersion") == "1.4"
            report.watch.append({
                "module": name,
                "status": "in-upstream-buildable" if buildable else "in-upstream-incompatible",
                "detail": f"upstream has {upstream_module} (libVersion {cfg.get('libVersion')}); "
                          + ("add it to `extensions` to build it" if buildable
                             else "still needs a patch to run on Tachimanga"),
            })
        else:
            near = difflib.get_close_matches(name, list(by_name), n=2, cutoff=0.75)
            report.watch.append({
                "module": name,
                "status": "no-upstream-module",
                "detail": f"frozen at v{entry['version'] if entry else '?'}; no upstream module"
                          + (f", closest names: {', '.join(near)}" if near else ""),
            })

    write_report(repo, report)
    if save_recipes(repo, recipes):
        print("== recipe state updated (a changed recipe republishes at a new version)")

    published = [r for r in report.results if r["status"] == "published"]
    print(f"\n{len(published)} published, {failures} failed")
    for row in report.watch:
        if row["status"] in ("CHANGED", "returned", "dropped", "in-upstream-buildable"):
            print(f"watch: {row['module']} -> {row['status']}: {row['detail']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
