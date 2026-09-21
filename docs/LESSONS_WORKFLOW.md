# Lessons workflow

Use the explicit lessons CLI after a user correction or an evidence-verified method yields one reusable rule. A candidate is review material; capture does not make it canonical or enforced.

## Capture the smallest useful scope

Project scope is the narrowest default. This complete synthetic example uses absolute runtime paths, records a privacy-safe evidence pointer, and adds a retrieval predicate. Replace the path values and project ID with real reviewed values before running it.

```sh
AGENT_CORE="/absolute/path/to/install/bin/agent-core"
HOST_CONFIG="/absolute/path/to/host.json"
WORKSPACE="/absolute/path/to/project"
PROJECT_ID="declared-project-id"

"$AGENT_CORE" lessons capture \
  --config "$HOST_CONFIG" \
  --workspace "$WORKSPACE" \
  --agent codex \
  --rule '当最终 shell 产物准备交付时，先运行只读 CR 字节检查' \
  --trigger 'final shell artifact prepared for delivery' \
  --cost 'a CR byte can break execution on Unix' \
  --sink 'templates/task-card.md' \
  --scope "project:$PROJECT_ID" \
  --evidence 'synthetic:redacted-shell-format-test' \
  --when '{"paths":["**/*.sh"]}'
```

The explicit command works for both Claude Code and Codex; set `--agent` to the runtime that learned the lesson. Whether an agent may invoke it automatically still follows host permissions. The shipped host example retains its current `Only Claude Code` automatic-capture limit.

Use scopes deliberately:

- `--scope auto` or `--scope project:<current-project-id>` writes to the current project's inbox. The workspace must be the matching Git project with its declared project identity.
- `--scope profile:<declared-profile>` targets a profile declared for this workspace.
- `--scope global` targets the bound private global state. Pass `--state <absolute-state-root>` when the host config does not provide a usable bound state root.

Keep `--rule` to one executable action in the form `当 <可观察触发>，先 <一个原子动作>`. `--when` is canonical JSON used for retrieval; it makes that rule form mandatory. Evidence should point to the smallest privacy-safe failure/pass record, test name, or review artifact that proves why the candidate exists. Do not place secrets, identity-bearing paths, or raw external content in any field.

## Review and promote

Plan first, review the complete postimage, then apply the same arguments with the printed plan hash. Choose exactly one of a scoped update or a deliberate new entry.

```sh
STATE_ROOT="/absolute/path/to/private-state"
CANDIDATE="candidate-id-from-capture"

"$AGENT_CORE" lessons promote \
  --config "$HOST_CONFIG" \
  --state "$STATE_ROOT" \
  --workspace "$WORKSPACE" \
  --id "$CANDIDATE" \
  --scope "project:$PROJECT_ID" \
  --force-new

"$AGENT_CORE" lessons promote \
  --config "$HOST_CONFIG" \
  --state "$STATE_ROOT" \
  --workspace "$WORKSPACE" \
  --id "$CANDIDATE" \
  --scope "project:$PROJECT_ID" \
  --force-new \
  --apply \
  --plan-hash '<reviewed-plan-hash>'
```

An explicit scope override is allowed only when the candidate inbox and target ledger resolve to the same Git repository. A cross-repository override is rejected with zero writes; capture again in the target repository's inbox with the desired scope.

`SIMILAR` lines are review advice, as are exact matches in another scope. An exact duplicate in the selected target ledger blocks promotion and directs the reviewer to the scoped update form:

```sh
"$AGENT_CORE" lessons promote \
  --config "$HOST_CONFIG" \
  --state "$STATE_ROOT" \
  --workspace "$WORKSPACE" \
  --id "$CANDIDATE" \
  --scope "project:$PROJECT_ID" \
  --update "project:$PROJECT_ID:<lesson-id>"
```

`--update` replaces the target's complete active row with the candidate's `rule`, `trigger`, `cost`, `sink`, source, and effective `when`; it does not merge fields. Review both `POSTIMAGE_REMOVED` and `POSTIMAGE_ADDED`. The rebuilt row returns to `pending`, including when the old row was `checklist` or `enforced`, so its sink and evidence must earn a later status again. A valid explicit update pointer may be used even when the fuzzy `SIMILAR` shortlist is empty.

Automatic Stop capture is a fallback for a missed explicit capture. Its current candidate has global scope and no `when`. During review, supply a canonical predicate with `lessons promote --when '<canonical-json>'` when one is justified. Automatic capture never authorizes promotion.

## Check final shell artifacts

The portable checker covers one deterministic subproblem: CR or CRLF bytes in explicitly named `.sh` files. It reads bytes without executing or modifying the files. Regular documentation and PowerShell files are reported as not applicable; their non-ASCII text is allowed. Missing paths, non-regular files, symbolic links, unreadable files, and any CR byte in a `.sh` file fail.

Set the absolute engine root and pass the exact reviewed final files. Keep each path quoted so spaces remain one argument.

```sh
ENGINE_ROOT="/absolute/path/to/agent-core/engine"
python "$ENGINE_ROOT/templates/check_shell_format.py" \
  "$ENGINE_ROOT/install.sh" \
  "$ENGINE_ROOT/runtimes/generic/user_prompt.sh"
```

The task-card delivery checklist is the consumer for this command. Hook pretool context is advisory and cannot stop the tool call already in progress. This focused checker does not add a service schema, a verifier registry entry, or a global gate; broader shell policy needs a separate reviewed task.

`lessons retire --report` inspects lifecycle, sink, and evidence state; it cannot be combined with `--verify` or `--allow-exec`. `lessons retire --workspace <workspace> --verify --allow-exec` runs declared proof probes. Those probes do not insert this checker into a real file operation. Adding this checker does not register a workspace verifier or promote any lesson, and prose in a lesson sink or status does not prove enforcement. Register a verifier only when an actual operation owns that call path; the task-card delivery command is the present concrete consumer.
