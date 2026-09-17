#!/usr/bin/env bash
# Generate the signing key this repository signs extensions with, and print what to do
# with it.
#
# The keystore is the only thing that makes repo.json's signingKeyFingerprint mean
# anything, so it is deliberately never stored in the repository: this writes it outside
# the repository, you store it as a repository secret, and then you keep or delete the
# copy yourself.
#
# Keep a backup somewhere private. Android will not update an installed extension across
# a change of signing key, so losing this file means every existing install has to be
# removed and reinstalled.
#
#   .github/scripts/new-signing-key.sh [output-dir]     (default: ~/tachimanga-signing)
#
# The alias and passwords default to the values in .github/extensions.json. They are not
# the secret - the keystore file is - so those defaults are fine as they are; pass your
# own by exporting ALIAS, KEY_STORE_PASSWORD and KEY_PASSWORD (and setting the same
# names as repository secrets) if you would rather they were not in the repository.
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
out="${1:-$HOME/tachimanga-signing}"
keystore="$out/signingkey.jks"
b64="$out/signingkey.jks.b64"

# macOS ships a /usr/bin/keytool that exists but fails at runtime when no JDK is
# installed, so check that it actually runs rather than that it is on PATH.
keytool -help >/dev/null 2>&1 || {
    echo "No working JDK found (keytool does not run), and it ships with one:" >&2
    echo "    brew install openjdk            # macOS" >&2
    echo "    apt-get install default-jre-headless   # Debian/Ubuntu" >&2
    exit 1
}

# Keep these in step with .github/extensions.json rather than repeating them here.
eval "$(cd "$repo" && python3 - <<'PY'
import json, shlex
cfg = json.load(open(".github/extensions.json"))["signing"]
for key in ("alias", "storePassword", "keyPassword", "dname", "validityDays"):
    print(f"{key}={shlex.quote(str(cfg[key]))}")
PY
)"
alias_="${ALIAS:-$alias}"
store_pw="${KEY_STORE_PASSWORD:-$storePassword}"
key_pw="${KEY_PASSWORD:-$keyPassword}"

mkdir -p "$out"
rm -f "$keystore" "$b64"

keytool -genkeypair \
    -keystore "$keystore" -alias "$alias_" \
    -keyalg RSA -keysize 2048 -validity "$validityDays" \
    -storepass "$store_pw" -keypass "$key_pw" -dname "$dname"

# base64 without wrapping, so it survives being pasted into a secret.
base64 < "$keystore" | tr -d '\n' > "$b64"

echo
echo "wrote $keystore"
echo "wrote $b64"
echo
keytool -list -v -keystore "$keystore" -alias "$alias_" -storepass "$store_pw" \
    | grep -i "SHA256:" | sed 's/^/  /'
echo
echo "Next:"
echo "  1. gh secret set SIGNING_KEY --repo <owner>/<repo> < \"$b64\""
echo "     The run will then re-sign every published extension with this key and update"
echo "     the fingerprint in repo/repo.json; the first run after that reports it."
echo "  2. Move $keystore somewhere private as a backup, and delete $b64."
echo "     Anyone who has the keystore can sign extensions that match this repository's"
echo "     fingerprint, so it belongs in the secret store and nowhere else."
