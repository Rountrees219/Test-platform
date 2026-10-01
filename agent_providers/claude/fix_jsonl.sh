#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 [-i] [-b backup_ext] <file.jsonl> [file2.jsonl ...]"
    echo
    echo "Detects and strips bad characters (NUL, other C0 control chars except"
    echo "TAB/CR/LF, and a leading UTF-8 BOM) from .jsonl files."
    echo
    echo "  -i            Edit files in place (default: write <file>.clean)"
    echo "  -b backup_ext When editing in place, keep a backup as <file><ext>"
    echo "                (e.g. -b .bak). No backup kept if omitted."
    echo
    echo "Exit status: 0 if all files clean or successfully fixed; 1 on error."
    exit "${1:-1}"
}

INPLACE=0
BACKUP_EXT=""

while getopts ":ib:h" opt; do
    case "$opt" in
        i) INPLACE=1 ;;
        b) BACKUP_EXT="$OPTARG" ;;
        h) usage 0 ;;
        \?) echo "Unknown option: -$OPTARG" >&2; usage ;;
        :)  echo "Option -$OPTARG requires an argument" >&2; usage ;;
    esac
done
shift $((OPTIND - 1))

[ "$#" -ge 1 ] || usage

# Reports whether $1 contains bad chars: NUL, a leading BOM, or C0 controls
# other than TAB(09) CR(0d) LF(0a). Byte-accurate; grep is unreliable with
# embedded NULs, so we use tr to count offending bytes.
has_bad_chars() {
    local f="$1"
    if [ "$(head -c 3 "$f" | od -An -tx1 | tr -d ' \n')" = "efbbbf" ]; then
        return 0
    fi
    local bad
    bad=$(LC_ALL=C tr -dc '\000-\010\013\014\016-\037' < "$f" | wc -c | tr -d ' ')
    [ "$bad" -gt 0 ]
}

# Strips a leading BOM plus bad control chars, prints result to stdout.
strip_bad_chars() {
    local f="$1"
    sed '1s/^\xef\xbb\xbf//' "$f" | LC_ALL=C tr -d '\000-\010\013\014\016-\037'
}

rc=0
for f in "$@"; do
    if [ ! -f "$f" ]; then
        echo "SKIP  $f (not a regular file)" >&2
        rc=1
        continue
    fi

    if ! has_bad_chars "$f"; then
        echo "OK    $f (no bad characters)"
        continue
    fi

    nul_count=$(LC_ALL=C tr -dc '\000' < "$f" | wc -c | tr -d ' ')
    echo "BAD   $f (NUL bytes: $nul_count, plus possible other control chars/BOM)"

    tmp="$(mktemp "${f}.XXXXXX")"
    strip_bad_chars "$f" > "$tmp"

    if [ "$INPLACE" -eq 1 ]; then
        if [ -n "$BACKUP_EXT" ]; then
            cp -p -- "$f" "${f}${BACKUP_EXT}"
            echo "      backup -> ${f}${BACKUP_EXT}"
        fi
        mv -- "$tmp" "$f"
        echo "FIXED $f (in place)"
    else
        out="${f}.clean"
        mv -- "$tmp" "$out"
        echo "FIXED $f -> $out"
    fi
done

exit "$rc"
