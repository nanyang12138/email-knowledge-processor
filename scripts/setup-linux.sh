#!/usr/bin/env bash
#
# One-shot setup on Linux: import an export and make it searchable.
#
# Costs nothing and calls no model. Stops at the first thing that is wrong and
# says what to do about it, rather than continuing and failing later somewhere
# less obvious.
#
#   bash scripts/setup-linux.sh /path/to/export [/path/for/database]
#
set -euo pipefail

SOURCE="${1:-}"
if [[ -z "$SOURCE" ]]; then
    echo "usage: bash scripts/setup-linux.sh <export-directory> [database-path]" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$(cd "$SOURCE" 2>/dev/null && pwd || echo "$SOURCE")"
DATABASE="${2:-$(dirname "$SOURCE")/email-kb/knowledge.db}"
VENV="$PROJECT_ROOT/.venv"

say() { printf '\n== %s\n' "$1"; }
die() { printf '\nSTOPPED: %s\n' "$1" >&2; exit 1; }

[[ -d "$SOURCE" ]] || die "$SOURCE is not a directory."

say "Checking the export at $SOURCE"
if compgen -G "$SOURCE"/*.zip > /dev/null && ! compgen -G "$SOURCE"/*.csv > /dev/null \
    && ! compgen -G "$SOURCE"/*.json > /dev/null; then
    command -v unzip > /dev/null 2>&1 || die "Only .zip files are here and unzip
is not installed. Unpack them some other way, then run this again."
    echo "only archives here, unpacking them"
    for archive in "$SOURCE"/*.zip; do
        unzip -q -o "$archive" -d "$SOURCE" || die "Could not unpack $archive"
    done
fi

say "Looking for Python 3.11 or newer"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" > /dev/null 2>&1 && "$candidate" -c \
        'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
        PYTHON="$candidate"
        break
    fi
done
[[ -n "$PYTHON" ]] || die "No Python 3.11+ found. On a managed machine try
    module avail python
and load a 3.11 or newer module, or install one with pyenv or conda."
echo "using $PYTHON ($("$PYTHON" --version))"

say "Creating the virtual environment"
if [[ ! -x "$VENV/bin/python" ]]; then
    # Some distributions ship Python without ensurepip, so venv fails. Fall
    # back to virtualenv, which does not need it, before giving up.
    if ! "$PYTHON" -m venv "$VENV" > /dev/null 2>&1; then
        rm -rf "$VENV"
        echo "venv is unavailable, trying virtualenv"
        "$PYTHON" -m virtualenv "$VENV" > /dev/null 2>&1 \
            || "$PYTHON" -m pip install --quiet --user virtualenv > /dev/null 2>&1 \
            && "$PYTHON" -m virtualenv "$VENV" > /dev/null 2>&1 \
            || die "Could not create a virtual environment. On a managed
machine, load a Python module that includes venv:
    module avail python
Otherwise install virtualenv by hand:
    $PYTHON -m pip install --user virtualenv"
    fi
fi
[[ -x "$VENV/bin/python" ]] || die "The virtual environment at $VENV is broken.
Remove it and run this again:
    rm -rf '$VENV'"
KB="$VENV/bin/python -m email_kb"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e "$PROJECT_ROOT[dev,agent]"

say "Checking the export is complete before importing it"
SURVEY="$($KB check-source "$SOURCE")"
echo "$SURVEY"
"$VENV/bin/python" - "$SURVEY" <<'PY' || die "Fix the points above, then run this again."
import json, sys
survey = json.loads(sys.argv[1])
sys.exit(0 if survey["ready"] and not survey["advice"] else 1)
PY

mkdir -p "$(dirname "$DATABASE")"
export EMAIL_KB_DB="$DATABASE"

say "Importing into $DATABASE"
$KB init
$KB ingest "$SOURCE"

say "Building the search index (no model involved)"
$KB index

say "Where things stand"
$KB stats
$KB doctor

cat <<EOF

== Done. Nothing above called a model or needed an API key.

Search it:
    export EMAIL_KB_DB="$DATABASE"
    $VENV/bin/python -m email_kb search "your question"

Give it to Cursor or Claude Code, then restart the client:
    $VENV/bin/python -m email_kb mcp-config --client cursor
    $VENV/bin/python -m email_kb mcp-config --client claude

The stats and doctor output above is counts only, no message text, so it is
safe to share when asking for help.
EOF
