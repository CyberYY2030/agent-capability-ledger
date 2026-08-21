#!/bin/sh

engine_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 2
export PYTHONPATH="$engine_root${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "$AGENT_CORE_PYTHON" ]; then exec "$AGENT_CORE_PYTHON" -m agent_core.cli install --source "$engine_root" "$@"; fi
if command -v python3 >/dev/null 2>&1; then exec python3 -m agent_core.cli install --source "$engine_root" "$@"; fi
if command -v python >/dev/null 2>&1; then exec python -m agent_core.cli install --source "$engine_root" "$@"; fi
echo "agent-core installer: Python 3 is unavailable" >&2
exit 2
