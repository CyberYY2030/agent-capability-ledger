$ErrorActionPreference = "Stop"
$engineRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$engineRoot;$env:PYTHONPATH" } else { $engineRoot }
if ($env:AGENT_CORE_PYTHON) {
    & $env:AGENT_CORE_PYTHON -m agent_core.cli install --source $engineRoot @args
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m agent_core.cli install --source $engineRoot @args
} else {
    & python -m agent_core.cli install --source $engineRoot @args
}
exit $LASTEXITCODE
