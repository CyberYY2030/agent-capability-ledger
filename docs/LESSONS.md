# 经验的格式和流程

用户纠正或经过证据验证的方法，可以形成候选。候选只供审核；采集不会把它写进台账，也不会把它标为 `enforced`。

## 一条经验的格式

下面是一条内置的合成经验：

```text
- **L-1 [enforced·通用] 改动共享常量时，必须搜索旧字面量本身并检查全部消费者。** 触发: 修改共享常量、schema 或 invariant。 代价: 局部修复后仍有消费者保留冲突真值。 verifier: seed-shared-literal. sink → templates/check_shared_literal.py. when: {"paths":["src/constants.py"]}
```

- ID：`L-1` 是通用台账的编号；领域和项目台账使用 `[[lesson:前缀-数字]]`。
- status：`pending` 表示待落实到可检查的落点；`checklist` 表示已有清单落点，仍需验证；`enforced` 表示应有可复验的执行证据。标签本身不能证明执行已发生。
- scope tag：`通用`、`领域`、`项目` 分别对应 `global`、`profile`、`project`。
- rule：加粗部分中的规则正文，说明遇到问题时要做什么。
- 触发：规则适用的情境；没有 `when` 时，检索会使用这段文字。
- 代价：不执行规则会带来的损失。
- verifier：验证器的 ID，用于查找声明的验证步骤。
- from：经验来自哪条候选。入库时自动写入候选 ID，上面这条内置经验没有这一项。
- sink：规则的落点，例如清单或检查脚本；退役检查会核对文件和证据。
- `when`：检索谓词，使用键排序、无多余空格的单行 JSON，各值都是非空字符串数组。`paths` 匹配路径通配符，`cmds` 匹配命令子串，`tasks` 匹配任务类型，`text` 匹配关键词；任一可用谓词命中即可召回。

## 采集

规则写成 `当 <可观察触发>，先 <一个原子动作>`。`--trigger` 写适用情境，`--cost` 写不执行的代价，`--sink` 写规则落点。`--evidence` 指向能说明失败或成功的最小测试、检查记录或审核材料；不要放凭据、带身份的本机路径或外部原文。

下面用通用范围采集一条合成候选。host 配置和私有仓库路径沿用[快速开始](QUICKSTART.md)。将 `--agent` 设为实际学到这条经验的 runtime：`codex` 或 `claude-code`；Agent 自动采集仍须遵守 host 权限。

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" lessons capture \
  --config ~/.agent-core/host.json --state ~/agent-core-private/state \
  --workspace ~/agent-core-private --agent codex --scope global \
  --rule '当最终 shell 产物准备交付时，先运行只读 CR 字节检查' \
  --trigger '交付最终 shell 产物' --cost 'CR 字节会使 Unix 脚本执行失败' \
  --sink 'templates/task-card.md' --evidence 'synthetic:shell-format-test' \
  --when '{"paths":["**/*.sh"]}'
```

选择最小适用范围：

- `--scope auto`：默认写入当前项目的 inbox；Git 项目沿用 `.agents/lessons.json` 的 `lessons-routing/1` 声明和有效 HEAD；非 Git 项目须使用下述显式目录声明。
- `--scope project:<current-project-id>`：显式选择当前项目，ID 必须与 `.agents/lessons.json` 中的声明一致。
- `--scope profile:<declared-profile>`：选择当前 workspace 声明的领域。
- `--scope global`：写入绑定的私有状态 inbox；host 配置无法给出有效路径时，用 `--state` 指定私有仓库的 `state` 目录。

`--when` 的格式见上面的字段说明。带 `--when` 时，程序会强制检查上述规则句式。

## 非 Git 项目：仅捕获候选

必须显式传入 `--workspace <项目根目录>`，并在该目录的 `.agents/lessons.json` 声明：

```json
{"schema":"lessons-routing/2","workspace_kind":"directory","project_id":"example-project","profiles":[]}
```

目录声明只适用于 Git 仓库之外的项目；不向父目录寻找目录身份，不借用父 Git 项目的身份。声明、`.agents`、inbox 和已有项目台账必须是本项目内的普通路径，不接受符号链接或目录联接。未声明、身份冲突、Git 探测失败或路径不安全时拒绝，不改投其他 scope。

Git 子目录按普通 JSON 解析当前目录的配置，以最后一个同名键为准。只有有效对象明确包含 `workspace_kind: "directory"`，或 `schema` 是 `lessons-routing/` 开头且不等于 `lessons-routing/1`，才视为与 Git 归属冲突的声明并拒绝。无法读取或解析、非对象、无关配置及旧 `/1` 子目录配置继续沿用 Git 根身份，不把它们作为子项目身份校验。Git 根配置继续走原有 schema 校验，并保留普通 JSON 的重复键处理；非 Git 声明仍严格拒绝重复键。

此路径必须提供合法的 `--when`，只向本项目 `.agents/inbox` 写入 `candidate/3`。新格式保留 `candidate/2` 的其他字段，以 `source_kind: "directory"` 替代 `base_revision`，明确没有 Git 修订证据；不生成假 SHA。非 Git 首次捕获不要求已有 `.agents/LESSONS.md`；Git 项目保留原有台账读取要求。声明的 profile 只参与来源读取，不改变候选的项目归属。

`candidate/3` 仅供候选检查，不能晋升；复制到 Git 项目或覆盖目标 scope 也不会使它可晋升。Git 的 `candidate/1`、`candidate/2` 仍强制校验 Git SHA。

显式 CLI 捕获使用新 UUID 和独占创建，保护已有候选，但不保证按内容去重。提交前检查本项目 inbox；结果不明时先核对返回路径和已有候选，不盲目重试。自动捕获路径不在此目录模式的支持范围内。

## 入库

从 `CAPTURED` 路径取候选文件名，去掉 `.md` 后缀，替换下面的 `<候选 ID>`。先预览，再人工审核目标、`POSTIMAGE_REMOVED` 和 `POSTIMAGE_ADDED`，最后用相同参数及预览里的 `PLAN_HASH` 执行。

新建条目用 `--force-new`：

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" lessons promote \
  --config ~/.agent-core/host.json --state ~/agent-core-private/state \
  --workspace ~/agent-core-private --id '<候选 ID>' --force-new
```

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" lessons promote \
  --config ~/.agent-core/host.json --state ~/agent-core-private/state \
  --workspace ~/agent-core-private --id '<候选 ID>' --force-new \
  --apply --plan-hash '<预览里的 PLAN_HASH>'
```

更新已有条目时，把 `--force-new` 换成 `--update '<scope:store:lesson-id>'`，两者只能选一个。例如通用台账用 `--update 'global:global:L-1'`，领域台账用 `--update 'profile:example-domain:EXAMPLE-1'`，项目台账用 `--update 'project:<current-project-id>:<lesson-id>'`。先预览更新，再带同一计划哈希执行。

`SIMILAR` 和其他范围的精确重复都只是审核建议。目标台账里的精确重复会阻塞新建；要改已有规则，请明确选择 `--update`。有效的更新指针不必出现在 `SIMILAR` 列表中。

`--update` 替换整行，不合并字段：规则、触发、代价、落点、来源和最终生效的 `when` 都取自候选或显式覆盖值。更新后状态重置为 `pending`，原来的 `checklist` 或 `enforced` 需要重新取得证据。

入库默认沿用候选范围。可以用 `--scope global`、`--scope profile:<declared-profile>` 或 `--scope project:<current-project-id>` 覆盖，但候选与目标台账必须属于同一个 Git 仓库。跨仓库覆盖会零写入拒绝；请在目标仓库的 inbox 重新采集。

计划哈希绑定候选字节、目标范围和路径、台账原内容、动作与结果 ID。执行时加锁复核，只写目标台账、归档候选并暂存相关路径；不会自动提交、推送或 sync。若台账已写入而后续步骤中断，重新预览并按新计划执行即可收敛。

每次 apply 后，先提交这次暂存的路径，再预览下一条候选的入库计划。上一条 apply 的暂存改动会阻塞下一条入库，并报 `FAIL_INDEX_CONFLICT`。

Claude Code 的 Stop 自动采集是漏掉显式采集时的后备。它当前生成通用范围、没有 `when` 的候选。审核时，在预览和执行两条入库命令里加上同一个 `--when`，例如 `--when '{"paths":["**/*.sh"]}'`。自动采集不授权入库。

## 退役检查

`lessons retire` 报告当前状态和证据，不会自动修改状态或归档条目。`--workspace` 必填，这里指向私有仓库的 `state` 目录：

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" lessons retire \
  --workspace ~/agent-core-private/state --report
```

只读报告会显示 `BROKEN_SINK`、`NOT_READY`、`READY_TO_CHECKLIST`、`STALE_EVIDENCE` 或 `FRESH_EVIDENCE` 等结果。`--report` 不能与 `--verify` 或 `--allow-exec` 合用。

审核过 workspace 中声明的验证器后，显式允许执行其探针：

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" lessons retire \
  --workspace ~/agent-core-private/state --verify --allow-exec
```

`checklist` 条目通过负例、正例和消费者探针后，输出 `READY_TO_ENFORCED` 和证据收据路径。`enforced` 条目有新鲜证据并再次通过验证后，输出 `READY_TO_ARCHIVE`；输入变化、证据过期或验证失败都需要处理后再检查。

一个具体检查例子是 shell 格式检查器：从私有仓库的 engine 中运行，明确传入最终文件。

```sh
python3 ~/agent-core-private/engine/templates/check_shell_format.py \
  ~/agent-core-private/engine/install.sh \
  ~/agent-core-private/engine/runtimes/generic/user_prompt.sh
```

它只读文件字节，不执行或改写文件；`.sh` 中有 CR 字节（含 CRLF）、路径缺失、符号链接或文件不可读都会失败。普通文档和 PowerShell 文件报告不适用。这个命令由任务卡的交付清单调用；它不会自行注册验证器，也不能证明某次真实文件操作已受检查。
