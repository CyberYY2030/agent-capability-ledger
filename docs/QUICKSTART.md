# Prepare your private agent-core workspace

The public checkout contains the engine only. The supplied helper creates a new private canonical repository with sibling `engine/` and `state/` directories, plus a host config outside that repository. The new `engine/` is the release-manifest-verified installable payload; it intentionally excludes public documentation, tests, and all source Git history. It does not install files, modify the runtime directory, configure a remote, or push.

Use Python 3.11 or newer. Create the workspace parent and config parent yourself, and create or choose the runtime root that agent-core will manage. All three must be owner-controlled paths. The workspace and config must not already exist.

First review a zero-write plan from any working directory:

```sh
python3 /path/to/agent-core/examples/prepare_private.py \
  --workspace /path/to/private-agent-core \
  --config /path/to/agent-core-host/host.json \
  --runtime codex \
  --runtime-root /path/to/codex-runtime \
  --git-name "Your Git Name" \
  --git-email "your-private-git-email" \
  --host-label local
```

`--runtime` accepts `codex` or `claude-code`. `--host-label` is optional and defaults to `local`; use a privacy-safe lowercase kebab-case label. On Windows, use a compatible `python` command and native absolute paths.

If every path is correct and the plan says `ready=true`, repeat the exact command with `--apply`. The helper validates every target again, copies only the release-manifest payload into the private repository, seeds synthetic starter state, creates an independent local Git history, and publishes the config last without replacing an existing object.

```sh
python3 /path/to/agent-core/examples/prepare_private.py \
  --workspace /path/to/private-agent-core \
  --config /path/to/agent-core-host/host.json \
  --runtime codex \
  --runtime-root /path/to/codex-runtime \
  --git-name "Your Git Name" \
  --git-email "your-private-git-email" \
  --host-label local \
  --apply
```

If apply reports retained preparation residue, inspect the exact path in the error before retrying. The helper deliberately does not delete an already published workspace or overwrite either target.

Create a private remote using your Git host's normal owner-controlled workflow. Then add it to the new repository and push its initial `main` branch yourself:

```sh
git -C /path/to/private-agent-core remote add origin '<YOUR_PRIVATE_REMOTE>'
git -C /path/to/private-agent-core push -u origin main
```

Replace every `/path/to/...` value and `<YOUR_PRIVATE_REMOTE>` with your own paths and private remote URL before running these commands.

Confirm the remote is private before installation. From the public checkout, review the existing install plan:

```sh
python3 -m agent_core.cli install \
  --config /path/to/agent-core-host/host.json \
  --state /path/to/private-agent-core/state \
  --source /path/to/private-agent-core/engine \
  --artifact-manifest /path/to/private-agent-core/engine/release-manifest.json \
  --confirm-private-remote
```

Apply only the exact reviewed plan hash printed above:

```sh
python3 -m agent_core.cli install \
  --config /path/to/agent-core-host/host.json \
  --state /path/to/private-agent-core/state \
  --source /path/to/private-agent-core/engine \
  --artifact-manifest /path/to/private-agent-core/engine/release-manifest.json \
  --confirm-private-remote --apply --plan-hash '<REVIEWED_INSTALL_PLAN_HASH>'
```

On POSIX, use the installed wrapper for verification and the first synthetic lesson lookup:

```sh
AGENT_CORE_BIN="${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core"
"$AGENT_CORE_BIN" doctor \
  --config /path/to/agent-core-host/host.json \
  --state /path/to/private-agent-core/state
"$AGENT_CORE_BIN" lessons match \
  --ledger /path/to/private-agent-core/state/experience/profiles/example-domain/LESSONS.md \
  --workspace seed --stage prompt --text "public fixture"
"$AGENT_CORE_BIN" sync \
  --config /path/to/agent-core-host/host.json \
  --state /path/to/private-agent-core/state
```

The final command is a sync preview and should report `DRY_RUN writes=0` immediately after installation. On Windows, the installed wrapper is `%LOCALAPPDATA%\agent-core\bin\agent-core.cmd`; pass the same `doctor`, `lessons match`, and `sync` arguments with your native paths.

The helper-generated hook target follows the existing runtime adapter: POSIX uses the `.sh` target and Windows selects the corresponding PowerShell target during installation. The preparation and full local journey in this alpha were exercised on macOS; Windows behavior remains covered by the existing adapter tests rather than a new live-host claim.
