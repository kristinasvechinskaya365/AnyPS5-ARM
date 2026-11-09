#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
usage: install.sh [--repository OWNER/REPO] [--version TAG] [options]
       install.sh --archive ZIP (--checksums FILE | --sha256 DIGEST) [options]

  --repository OWNER/REPO  GitHub source; default: kristinasvechinskaya365/AnyPS5-ARM
  --version TAG           Explicit release tag; otherwise resolve latest once
  --archive ZIP           Install a local AnyPS5-macOS.zip without networking
  --checksums FILE        Require its single AnyPS5-macOS.zip SHA256SUMS entry
  --sha256 DIGEST         Require this exact SHA-256 for a local archive
  --destination DIR      Applications folder (default: ~/Applications)
  --open                  Open the app after a successful installation
  --help                  Show this help

ANYPS5_REPOSITORY also selects the release repository. The first installation
trusts that GitHub release over HTTPS, or your provided checksum. Archive bytes
and code-signature integrity are verified; an ad hoc signature does not identify
the publisher. Gatekeeper settings and quarantine attributes are preserved.
No developer tools, administrator password, or automatic Rosetta installation
are required. Games using the x86-64 engine require Rosetta on Apple Silicon.
EOF
}

fail() {
    printf 'AnyPS5: %s\n' "$*" >&2
    exit 1
}

repository="${ANYPS5_REPOSITORY:-kristinasvechinskaya365/AnyPS5-ARM}"
version=
archive=
checksums=
expected=
destination="$HOME/Applications"
launch=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --repository|--version|--archive|--checksums|--sha256|--destination)
            [ "$#" -ge 2 ] || fail "$1 needs a value"
            [ -n "$2" ] || fail "$1 needs a nonempty value"
            case "$1" in
                --repository) repository="$2" ;;
                --version) version="$2" ;;
                --archive) archive="$2" ;;
                --checksums) checksums="$2" ;;
                --sha256) expected="$2" ;;
                --destination) destination="$2" ;;
            esac
            shift 2 ;;
        --open) launch=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; fail "unknown option: $1" ;;
    esac
done

if [ -n "$archive" ]; then
    [ -z "$version" ] || fail "--version cannot be combined with --archive"
    [ -z "$checksums" ] || [ -z "$expected" ] || fail "choose --checksums or --sha256"
    [ -n "$checksums$expected" ] || fail "a local archive needs --checksums or --sha256"
else
    [ -n "$repository" ] || fail "choose --repository OWNER/REPO or --archive ZIP"
    [ -z "$checksums$expected" ] || fail "--checksums and --sha256 require --archive"
    case "$repository" in
        */*) owner="${repository%%/*}"; project="${repository#*/}" ;;
        *) fail "the repository must be OWNER/REPO" ;;
    esac
    for part in "$owner" "$project"; do
        case "$part" in ''|.|..|*[!A-Za-z0-9_.-]*) fail "the repository must be OWNER/REPO" ;; esac
    done
fi

[ "$(uname -s)" = Darwin ] || fail "this installer requires macOS"
machine="$(uname -m)"
case "$machine" in arm64|x86_64) ;; *) fail "unsupported Mac architecture: $machine" ;; esac
system_version="$(sw_vers -productVersion)"
major="${system_version%%.*}"
case "$major" in ''|*[!0-9]*) fail "cannot read the macOS version" ;; esac
[ "$major" -ge 14 ] || fail "macOS 14 or newer is required"
case "$(tar --version)" in bsdtar*) ;; *) fail "the macOS bsdtar command is required" ;; esac

scratch=
work=
lock=
backup=
target=
committed=0
cleanup() {
    result=$?
    trap - 0 HUP INT TERM
    keep=0
    if [ "$committed" -eq 0 ] && [ -n "$backup" ] && [ -d "$backup" ]; then
        if [ ! -e "$target" ] && [ ! -L "$target" ] && mv -n "$backup" "$destination/" && [ ! -e "$backup" ]; then
            printf 'AnyPS5: restored the previous app\n' >&2
        else
            printf 'AnyPS5: the previous app is preserved at %s; restore it after resolving the destination error\n' "$backup" >&2
            keep=1
            result=1
        fi
    fi
    if [ -n "$work" ] && [ "$keep" -eq 0 ]; then
        rm -rf "$work" || printf 'AnyPS5: temporary installation files remain at %s\n' "$work" >&2
    fi
    [ -z "$lock" ] || rmdir "$lock" || printf 'AnyPS5: installation lock remains at %s\n' "$lock" >&2
    [ -z "$scratch" ] || rm -rf "$scratch" || printf 'AnyPS5: download files remain at %s\n' "$scratch" >&2
    exit "$result"
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
scratch="$(mktemp -d)"
quarantine=
if [ -n "$archive" ]; then
    [ -f "$archive" ] || fail "the archive is not a file: $archive"
    quarantine="$(xattr -p com.apple.quarantine "$archive" 2>/dev/null || true)"
    cp "$archive" "$scratch/AnyPS5-macOS.zip"
else
    if [ -z "$version" ]; then
        latest="$(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
            --connect-timeout 15 --max-time 60 --output /dev/null --write-out '%{url_effective}\n' \
            "https://github.com/$repository/releases/latest")" || fail "cannot resolve the latest release; retry or choose --version TAG"
        prefix="https://github.com/$repository/releases/tag/"
        case "$latest" in "$prefix"*) version="${latest#"$prefix"}" ;; *) fail "latest did not resolve to a release of $repository" ;; esac
    fi
    case "$version" in ''|.|..|*[!A-Za-z0-9._+-]*) fail "use a release tag containing only letters, digits, dots, underscores, plus or minus signs" ;; esac
    release="https://github.com/$repository/releases/download/$version"
    printf 'AnyPS5: installing release %s from %s\n' "$version" "$repository"
    checksums="$scratch/SHA256SUMS"
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 60 --max-filesize 1048576 "$release/SHA256SUMS" -o "$checksums" ||
        fail "the release has no readable SHA256SUMS; nothing was installed"
    curl --fail --show-error --location --progress-bar --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 1800 "$release/AnyPS5-macOS.zip" -o "$scratch/AnyPS5-macOS.zip" ||
        fail "the release archive could not be downloaded; nothing was installed"
fi
archive="$scratch/AnyPS5-macOS.zip"
if [ -n "$checksums" ]; then
    [ -f "$checksums" ] || fail "the checksum file is not readable: $checksums"
    expected="$(awk '
        { sub(/\r$/, ""); digest = substr($0, 1, 64); name = substr($0, 66); sub(/^[ *]/, "", name) }
        name == "AnyPS5-macOS.zip" {
            if (substr($0, 65, 1) != " " || length(digest) != 64 || digest ~ /[^0-9a-fA-F]/) bad = 1
            count++; value = tolower(digest)
        }
        END { if (bad || count != 1) exit 1; print value }
    ' "$checksums")" || fail "SHA256SUMS must contain exactly one valid AnyPS5-macOS.zip entry"
fi
case "$expected" in ''|*[!0-9a-fA-F]*) fail "the SHA-256 must contain 64 hexadecimal digits" ;; esac
[ "${#expected}" -eq 64 ] || fail "the SHA-256 must contain 64 hexadecimal digits"
expected="$(printf '%s' "$expected" | tr A-F a-f)"
actual="$(shasum -a 256 "$archive" | cut -d' ' -f1)"
[ "$actual" = "$expected" ] || fail "the archive does not match its SHA-256; nothing was installed"

unzip -tq "$archive" > "$scratch/archive-test.txt" || fail "the download is not a complete ZIP archive"
tar -tf "$archive" > "$scratch/entries.txt" || fail "the archive entries cannot be read"
awk '
    /^AnyPS5[.]app(\/|$)/ || /^__MACOSX(\/|$)/ {
        count = split($0, parts, "/")
        for (i = 1; i <= count; i++) if (parts[i] == "..") bad = 1
        next
    }
    { bad = 1 }
    END { exit bad }
' "$scratch/entries.txt" || fail "the archive contains paths outside AnyPS5.app"
mkdir "$scratch/unpacked"
tar -xf "$archive" -C "$scratch/unpacked" --no-same-owner --no-same-permissions || fail "the archive could not be safely extracted"
app="$scratch/unpacked/AnyPS5.app"

validate_app() {
    bundle="$1"
    [ -d "$bundle" ] && [ ! -L "$bundle" ] || fail "the archive must contain one real AnyPS5.app directory"
    bundle_root="$(CDPATH= cd -P "$bundle" && pwd -P)"
    find "$bundle" -type l -print > "$scratch/links.txt"
    while IFS= read -r link; do
        resolved="$link"
        hops=0
        while [ -L "$resolved" ]; do
            hops=$((hops + 1))
            [ "$hops" -le 40 ] || fail "an app link cannot be resolved"
            points="$(readlink "$resolved")" || fail "an app link cannot be read"
            case "$points" in ''|/*) fail "an app link points outside the bundle" ;; esac
            resolved="${resolved%/*}/$points"
            parent="$(CDPATH= cd -P "${resolved%/*}" && pwd -P)" || fail "an app link cannot be resolved"
            resolved="$parent/${resolved##*/}"
        done
        if [ -d "$resolved" ]; then
            resolved="$(CDPATH= cd -P "$resolved" && pwd -P)" || fail "an app link cannot be resolved"
        else
            [ -f "$resolved" ] || fail "an app link does not resolve to a bundled file"
        fi
        case "$resolved" in "$bundle_root"|"$bundle_root"/*) ;; *) fail "an app link escapes the bundle" ;; esac
    done < "$scratch/links.txt"
    plist="$bundle/Contents/Info.plist"
    [ "$(plutil -extract CFBundleIdentifier raw -o - "$plist")" = org.anyps5.launcher ] || fail "the bundle is not the AnyPS5 launcher"
    [ "$(plutil -extract CFBundlePackageType raw -o - "$plist")" = APPL ] || fail "the bundle is not a macOS application"
    [ "$(plutil -extract CFBundleExecutable raw -o - "$plist")" = AnyPS5 ] || fail "the launcher executable is not AnyPS5"
    minimum="$(plutil -extract LSMinimumSystemVersion raw -o - "$plist")" || fail "the app does not declare its minimum macOS version"
    case "$minimum" in ''|*[!0-9.]*|.*|*.|*..*) fail "the app declares an invalid minimum macOS version" ;; esac
    awk -v installed="$system_version" -v required="$minimum" 'BEGIN {
        if (split(installed, hostParts, ".") > 3 || split(required, minimumParts, ".") > 3) exit 1
        for (i = 1; i <= 3; i++) {
            if ((hostParts[i] + 0) > (minimumParts[i] + 0)) exit 0
            if ((hostParts[i] + 0) < (minimumParts[i] + 0)) exit 1
        }
    }' || fail "this app requires macOS $minimum; this Mac runs $system_version"
    binary="$bundle/Contents/MacOS/AnyPS5"
    [ -f "$binary" ] && [ -x "$binary" ] || fail "the launcher executable is missing"
    case "$(file -b "$binary")" in *"Mach-O 64-bit executable $machine"*) ;; *) fail "the launcher does not contain a compatible $machine executable" ;; esac
    codesign --verify --deep --strict "$bundle" || fail "the app's code-signature integrity check failed"
}
validate_app "$app"

mkdir -p "$destination"
destination="$(cd "$destination" && pwd -P)"
[ -w "$destination" ] || fail "the destination is not writable: $destination"
target="$destination/AnyPS5.app"
[ ! -L "$target" ] || fail "the existing app is a symbolic link; nothing was replaced"
[ ! -e "$target" ] || [ -d "$target" ] || fail "the existing destination is not an app directory"
mkdir "$destination/.AnyPS5-install.lock" 2>/dev/null || fail "another install may be active: $destination/.AnyPS5-install.lock"
lock="$destination/.AnyPS5-install.lock"
work="$(mktemp -d "$destination/.AnyPS5-install.XXXXXX")"
incoming="$work/AnyPS5.app"
mkdir "$work/previous"
backup="$work/previous/AnyPS5.app"
ditto "$app" "$incoming" || fail "the replacement could not be staged; the previous app is unchanged"
[ -z "$quarantine" ] || xattr -w com.apple.quarantine "$quarantine" "$incoming"
validate_app "$incoming"
if [ -e "$target" ]; then
    mv -n "$target" "$work/previous/" || fail "the previous app could not be preserved"
    [ ! -e "$target" ] || fail "the previous app could not be moved"
fi
mv -n "$incoming" "$destination/" || fail "the replacement could not be placed"
[ ! -e "$incoming" ] || fail "the destination changed during installation"
committed=1
printf 'AnyPS5: installed %s (SHA-256 %s)\n' "$target" "$actual"
if [ "$machine" = arm64 ] && ! arch -x86_64 /usr/bin/true 2>/dev/null; then
    printf '%s\n' 'AnyPS5: the launcher is installed. Games using the x86-64 engine need Rosetta; install it with: softwareupdate --install-rosetta'
fi
if [ "$launch" -eq 1 ]; then
    open "$target" || printf 'AnyPS5: installed successfully; open %s manually\n' "$target" >&2
fi
