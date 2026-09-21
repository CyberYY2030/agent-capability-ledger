#!/bin/sh

engine_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 2
export PYTHONPATH="$engine_root${PYTHONPATH:+:$PYTHONPATH}"
if [ -n "$AGENT_CORE_PYTHON" ]; then exec "$AGENT_CORE_PYTHON" -m agent_core.cli install --source "$engine_root" "$@"; fi
floor=$(sed -n 's/^requires-python[[:space:]]*=[[:space:]]*">=\([0-9][0-9]*\)\.\([0-9][0-9]*\)"[[:space:]]*$/\1 \2/p' "$engine_root/pyproject.toml")
floor_major=${floor%% *}
floor_minor=${floor#* }
if [ -z "$floor_major" ] || [ -z "$floor_minor" ]; then
  echo "agent-core installer: cannot derive Python floor from pyproject.toml" >&2
  exit 2
fi
attempts=
candidates="python3.13 python3.12 python3.11 python3 python"
for candidate in $candidates; do
  candidate_path=$(command -v "$candidate" 2>/dev/null) || continue
  version=$("$candidate_path" -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(0 if sys.version_info >= ($floor_major, $floor_minor) else 1)" </dev/null 2>/dev/null)
  qualified=$?
  [ -n "$version" ] || version=unavailable
  attempts="${attempts}${attempts:+; }${candidate_path}=${version}"
  if [ "$qualified" -eq 0 ]; then
    exec "$candidate_path" -m agent_core.cli install --source "$engine_root" "$@"
  fi
done
[ -n "$attempts" ] || attempts="none found ($candidates)"
echo "agent-core installer: requires Python >=$floor_major.$floor_minor; tried $attempts" >&2
exit 2
