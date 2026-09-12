#!/usr/bin/env bash
#
# Set up whatever is missing, then start Livebound.
#
# First run installs the Python environment and builds the frontend; every run
# after that only checks whether something is out of date. The checks are cheap
# and based on file timestamps, so a normal start costs a few milliseconds.
#
#   ./webui.sh                start (set up first if needed)
#   ./webui.sh --dev          plus the Vite dev server with hot reload
#   ./webui.sh --update       git pull, reinstall, rebuild, then start
#   ./webui.sh --llm          also set up .venv-llm for the local model
#   ./webui.sh --skip-build   do not touch the frontend at all
#
# Anything not listed above is passed straight to `livebound serve`,
# so `./webui.sh --port 9000 --no-browser` works.

set -euo pipefail

cd "$(dirname "$0")"

# Optional webui.settings.sh may set PYTHON, VENV_DIR and COMMANDLINE_ARGS
# (as a Bash array) without changing this tracked file.
COMMANDLINE_ARGS=()
if [ -f webui.settings.sh ]; then
    # shellcheck disable=SC1091
    source webui.settings.sh
fi

VENV="${VENV_DIR:-.venv}"
LLM_VENV=".venv-llm"
PY_STAMP="$VENV/.install-stamp"
RELEASE_CONSTRAINTS="constraints-release.txt"
LLM_CONSTRAINTS="constraints-llm.txt"
NPM_STAMP="frontend/node_modules/.install-stamp"
DIST="frontend/dist/index.html"

DO_UPDATE=0
DO_DEV=0
DO_LLM=0
SKIP_BUILD=0
SERVE_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --update)     DO_UPDATE=1 ;;
        --dev)        DO_DEV=1 ;;
        --llm)        DO_LLM=1 ;;
        --skip-build) SKIP_BUILD=1 ;;
        -h|--help)    awk 'NR>2 && /^#/ {sub(/^# ?/, ""); print; next} NR>2 {exit}' "$0"; exit 0 ;;
        *)            SERVE_ARGS+=("$arg") ;;
    esac
done

say()  { printf '\033[36m::\033[0m %s\n' "$*"; }
warn() { printf '\033[33m::\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m::\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites ----------------------------------------------------------

HAVE_UV=0
command -v uv >/dev/null 2>&1 && HAVE_UV=1

PYTHON_BIN=""
PYTHON_CANDIDATES=()
for candidate in "${PYTHON:-}" python3.13 python3.12 python3; do
    [ -n "$candidate" ] || continue
    if ! candidate_path=$(command -v "$candidate" 2>/dev/null); then
        PYTHON_CANDIDATES+=("$candidate: not found")
    elif "$candidate" -m backend.cli starter python >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    else
        PYTHON_CANDIDATES+=("$candidate: found at $candidate_path but rejected")
    fi
done

if [ "$HAVE_UV" -eq 0 ] && [ -z "$PYTHON_BIN" ]; then
    warn "No usable Python 3.12 or newer interpreter was found. Candidates tried:"
    for outcome in "${PYTHON_CANDIDATES[@]}"; do warn "  $outcome"; done
    die "Set PYTHON=/path/to/python3.12, or install Python 3.12 or newer."
fi

if [ "$HAVE_UV" -eq 0 ]; then
    warn "uv not found, falling back to venv + pip. Install uv for faster setup: https://docs.astral.sh/uv/"
fi

HAVE_NODE=0
command -v npm >/dev/null 2>&1 && HAVE_NODE=1

# --- update -----------------------------------------------------------------

if [ "$DO_UPDATE" -eq 1 ]; then
    command -v git >/dev/null 2>&1 || die "--update needs git."
    say "Pulling the latest revision"
    git pull --ff-only
    rm -f "$PY_STAMP" "$NPM_STAMP"
    [ "$HAVE_NODE" -eq 1 ] && rm -f "$DIST"
fi

# --- python environment -----------------------------------------------------

install_python_env() {
    if [ ! -d "$VENV" ]; then
        say "Creating $VENV"
        if [ "$HAVE_UV" -eq 1 ]; then uv venv "$VENV"; else "$PYTHON_BIN" -m venv "$VENV"; fi
    fi
    say "Installing the application"
    if [ "$HAVE_UV" -eq 1 ]; then
        # The dev group carries pytest and ruff; older uv versions do not know
        # --group, and a plain install is still a working setup.
        uv pip install --python "$VENV/bin/python" -e . --group dev -c "$RELEASE_CONSTRAINTS" \
            --build-constraints "$RELEASE_CONSTRAINTS" 2>/dev/null \
            || uv pip install --python "$VENV/bin/python" -e . -c "$RELEASE_CONSTRAINTS" \
                --build-constraints "$RELEASE_CONSTRAINTS"
    else
        "$VENV/bin/python" -m pip install --quiet --upgrade pip
        "$VENV/bin/python" -m pip install -e . -c "$RELEASE_CONSTRAINTS" \
            --build-constraint "$RELEASE_CONSTRAINTS"
    fi
    touch "$PY_STAMP"
}

if [ ! -x "$VENV/bin/livebound" ] || [ ! -f "$PY_STAMP" ] || [ pyproject.toml -nt "$PY_STAMP" ] || [ "$RELEASE_CONSTRAINTS" -nt "$PY_STAMP" ]; then
    install_python_env
fi

"$VENV/bin/python" -m backend.cli starter python \
    --interpreter "$VENV/bin/python" --environment "$VENV"

if [ "$DO_LLM" -eq 1 ]; then
    if [ ! -d "$LLM_VENV" ]; then
        say "Creating $LLM_VENV (this pulls a multi-gigabyte ML stack)"
        if [ "$HAVE_UV" -eq 1 ]; then uv venv "$LLM_VENV"; else "$PYTHON_BIN" -m venv "$LLM_VENV"; fi
    fi
    say "Installing the model worker requirements"
    if [ "$HAVE_UV" -eq 1 ]; then
        # A venv made by `uv venv` has no pip of its own, so `python -m pip`
        # fails outright here. uv installs by interpreter path instead - the
        # same branch the application environment above already takes.
        uv pip install --python "$LLM_VENV/bin/python" -r requirements-llm.txt -c "$LLM_CONSTRAINTS"
    else
        "$LLM_VENV/bin/python" -m pip install --upgrade pip
        "$LLM_VENV/bin/python" -m pip install -r requirements-llm.txt -c "$LLM_CONSTRAINTS"
    fi
    say "Point 'LLM interpreter' in the settings at $(cd "$(dirname "$LLM_VENV/bin/python")" && pwd -P)/python"
fi

# --- frontend ---------------------------------------------------------------

STARTER_FRONTEND_ARGS=()
[ "$HAVE_NODE" -eq 1 ] && STARTER_FRONTEND_ARGS+=(--npm)
[ "$DO_DEV" -eq 1 ] && STARTER_FRONTEND_ARGS+=(--dev)
[ "$DO_UPDATE" -eq 1 ] && STARTER_FRONTEND_ARGS+=(--update)
[ "$SKIP_BUILD" -eq 1 ] && STARTER_FRONTEND_ARGS+=(--skip-build)

"$VENV/bin/python" -m backend.cli starter frontend "${STARTER_FRONTEND_ARGS[@]}"

if "$VENV/bin/python" -m backend.cli starter frontend \
        "${STARTER_FRONTEND_ARGS[@]}" --decision dependencies; then
    say "Installing frontend dependencies"
    (cd frontend && npm install)
    touch "$NPM_STAMP"
fi
if "$VENV/bin/python" -m backend.cli starter frontend \
        "${STARTER_FRONTEND_ARGS[@]}" --decision build; then
    say "Building the frontend"
    (cd frontend && npm run build)
fi

"$VENV/bin/python" -m backend.cli starter dependencies --launcher ./webui.sh

# --- run --------------------------------------------------------------------

if [ "$DO_DEV" -eq 1 ]; then
    [ "$HAVE_NODE" -eq 1 ] || die "--dev needs npm."
    say "Backend on http://127.0.0.1:8430, dev server below. Ctrl-C stops both."
    "$VENV/bin/python" -m backend.cli serve --no-browser "${COMMANDLINE_ARGS[@]}" "${SERVE_ARGS[@]}" &
    BACKEND_PID=$!
    trap 'kill "$BACKEND_PID" 2>/dev/null || true' EXIT INT TERM
    cd frontend && exec npm run dev
fi

exec "$VENV/bin/python" -m backend.cli serve "${COMMANDLINE_ARGS[@]}" "${SERVE_ARGS[@]}"
