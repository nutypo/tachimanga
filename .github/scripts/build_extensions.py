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

ci-report.json is always written at the repo root - including the tail of the
Gradle log on failure - because GitHub job logs need authentication to read, so
without it a scheduled run that breaks is a black box.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import glob
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field

UPSTREAM_URL = "https://github.com/keiyoushi/extensions-source.git"
UPSTREAM_REPO = "keiyoushi/extensions-source"


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
    log_tail: str = ""


@dataclass
class Report:
    generatedAt: str = ""
    runUrl: str = ""
    keystore: dict = field(default_factory=dict)
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


def find_aapt() -> str | None:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk:
        return None
    found = sorted(glob.glob(os.path.join(sdk, "build-tools", "*", "aapt")))
    return found[-1] if found else None


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


def publish(repo: pathlib.Path, module: str, info: dict) -> str:
    """Copy the built APK into repo/apk/ and point the index at it."""
    lang, name = module_parts(module)
    apk_dir = repo / "repo/apk"
    apk_dir.mkdir(parents=True, exist_ok=True)
    filename = f"tachiyomi-{lang}.{name}-v{info['version']}.apk"
    shutil.copyfile(info["path"], apk_dir / filename)

    index_path, entries = load_index(repo)
    matches = [e for e in entries if e["pkg"] == info["pkg"]]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly 1 {info['pkg']} entry in the index, found {len(matches)}")
    matches[0]["apk"] = filename
    matches[0]["code"] = info["code"]
    matches[0]["version"] = info["version"]
    index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for stale in apk_dir.glob(f"tachiyomi-{lang}.{name}-*-v*.apk"):
        if stale.name != filename:
            # kept deliberately: makes rolling back to the previous build a
            # one-line index change (and matches how this repo already stores
            # older versions of an extension).
            print(f"   keeping previous build {stale.name}")
    return filename


def decide_publish(repo: pathlib.Path, info: dict, keystore_fp: str) -> tuple[bool, str]:
    """Publish unless the index already advertises this build, signed with our key."""
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
    return False, "already published with this key"


# --------------------------------------------------------------------------- #
# keystore
# --------------------------------------------------------------------------- #

def ensure_keystore(repo: pathlib.Path, cfg: dict, report: Report) -> tuple[pathlib.Path, dict]:
    """A local .jks plus the alias/passwords for Gradle.

    Precedence: SIGNING_KEY secret > keystore committed in the repo > generate one
    and commit it, so the signature is stable across runs and cannot be lost.
    """
    alias = os.environ.get("ALIAS") or cfg["alias"]
    store_pw = os.environ.get("KEY_STORE_PASSWORD") or cfg["storePassword"]
    key_pw = os.environ.get("KEY_PASSWORD") or cfg["keyPassword"]
    path = pathlib.Path("/tmp/signingkey.jks")

    secret = (os.environ.get("SIGNING_KEY") or "").strip()
    if secret:
        path.write_bytes(base64.b64decode(secret))
        source = "SIGNING_KEY secret"
    else:
        committed = repo / cfg["keystoreFile"]
        if committed.exists():
            path.write_bytes(base64.b64decode(committed.read_text(encoding="utf-8")))
            source = f"committed {cfg['keystoreFile']}"
        else:
            run([
                "keytool", "-genkeypair", "-keystore", str(path), "-alias", alias,
                "-keyalg", "RSA", "-keysize", "2048", "-validity", str(cfg["validityDays"]),
                "-storepass", store_pw, "-keypass", key_pw, "-dname", cfg["dname"],
            ])
            committed.parent.mkdir(parents=True, exist_ok=True)
            committed.write_text(base64.b64encode(path.read_bytes()).decode() + "\n",
                                 encoding="utf-8")
            source = f"generated -> {cfg['keystoreFile']}"

    report.keystore = {"source": source, "alias": alias}
    return path, {"ALIAS": alias, "KEY_STORE_PASSWORD": store_pw, "KEY_PASSWORD": key_pw}


def keystore_fingerprint(path: pathlib.Path, alias: str, store_pw: str) -> str:
    try:
        out = run(["keytool", "-list", "-v", "-keystore", str(path), "-alias", alias,
                   "-storepass", store_pw]).stdout
    except (RuntimeError, OSError):
        return ""
    match = re.search(r"SHA256: ([0-9A-Fa-f:]+)", out)
    return match.group(1).replace(":", "").lower() if match else ""


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
# main
# --------------------------------------------------------------------------- #

def effective_gates(manifest: dict) -> dict:
    """The gates actually applied, which include the minSdk we rewrite builds to.

    Keeping the two together means the gate can never drift from the value the
    build was told to use.
    """
    return {**manifest["gates"], "maxMinSdk": manifest["targetMinSdk"]["value"]}


def build_entry(entry: dict, upstream: pathlib.Path, ref: str, repo: pathlib.Path,
                gates: dict, min_sdk: dict, signing_env: dict, keystore_fp: str) -> tuple[Result, str]:
    """Build one manifest entry and publish it if it passes the gates."""
    module = entry["module"]
    result = Result(module=module, ref=ref)
    try:
        reset_module(upstream, ref, module)
        if entry.get("patch"):
            patch = repo / ".github/scripts" / entry["patch"]
            run(["python3", str(patch), str(upstream)])
        result.prep = lower_min_sdk(upstream, min_sdk)

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

        should, why = decide_publish(repo, info, keystore_fp)
        if not should:
            result.status = "up-to-date"
            result.reason = why
            return result, "up-to-date"

        result.apk = publish(repo, module, info)
        result.status = "published"
        result.reason = why
        return result, "published"
    except Exception as exc:  # noqa: BLE001 - one entry must not kill the run
        result.status = "failed"
        result.reason = f"{type(exc).__name__}: {exc}"
        return result, "failed"


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

    keystore, signing_env = ensure_keystore(repo, manifest["signing"], report)
    keystore_fp = keystore_fingerprint(keystore, signing_env["ALIAS"],
                                      signing_env["KEY_STORE_PASSWORD"])
    report.keystore.update({"sha256": keystore_fp} if keystore_fp else {})

    min_sdk = manifest["targetMinSdk"]
    gates = effective_gates(manifest)

    entries = manifest["extensions"]
    if args.only:
        entries = [e for e in entries if e["module"] == args.only]

    failures = 0
    if not args.watch_only and entries:
        by_ref: dict[str, list] = {}
        for entry in entries:
            ref = entry.get("ref") or entry.get("track", "main")
            by_ref.setdefault(ref, []).append(entry)

        for ref, group in by_ref.items():
            upstream = clone_upstream(ref, workdir)
            shutil.copyfile(keystore, upstream / "signingkey.jks")
            print(f"== upstream {ref} ({len(group)} extension(s))")
            for entry in group:
                print(f"-> {entry['module']}")
                result, outcome = build_entry(entry, upstream, ref, repo, gates,
                                              min_sdk, signing_env, keystore_fp)
                failures += outcome == "failed"
                report.results.append(asdict(result))
                print(f"   {outcome}: {result.reason}"
                      + (f" -> {result.apk}" if result.apk else "")
                      + (f" [{result.prep}]" if result.prep else ""))

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

    (repo / "ci-report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    published = [r for r in report.results if r["status"] == "published"]
    print(f"\n{len(published)} published, {failures} failed")
    for row in report.watch:
        if row["status"] in ("CHANGED", "returned", "dropped"):
            print(f"watch: {row['module']} -> {row['status']}: {row['detail']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
