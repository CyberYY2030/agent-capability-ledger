# 快速开始

公开仓库只有 engine。你的经验、规则和技能放在你自己的私有 Git 仓库里，由 agent-core 安装到 Claude Code 或 Codex。本页先在第一台机器上装好，再让第二台机器加入同一个私有仓库。

## 准备

- Python 3.11 或更新版本，Git 2.28 或更新版本。下面的命令适用于 macOS 和 Linux。Linux 需要内核和文件系统支持 `RENAME_NOREPLACE`，不支持时生成私有仓库会直接失败，不会覆盖任何文件。Windows 用 `install.ps1` 安装，装好的命令是 `%LOCALAPPDATA%\agent-core\bin\agent-core.cmd`；Windows 上的完整流程还没有实测。
- 公开仓库已经 clone 到 `~/agent-core`。
- 运行时目录已经存在：Claude Code 用 `~/.claude`，Codex 用 `~/.codex`。
- 一个空的私有 Git 远端，例如 GitHub 上的私有仓库。下文用 `<你的私有远端地址>` 表示它的地址。只想先在本机试用，可以先运行 `git init --bare --initial-branch=main ~/agent-core-remote.git`，然后把地址写成 `~/agent-core-remote.git`。

## 第一台机器（6 条命令）

<!-- journey:first-machine -->

1. 建立放 host 配置的目录：

```bash
mkdir -p ~/.agent-core
```

2. 生成私有仓库 `~/agent-core-private` 和 host 配置。把名字和邮箱换成你自己的 Git 身份。去掉 `--apply` 就只预览，不写入。用 Codex 时改成 `--runtime codex --runtime-root ~/.codex`。

```bash
python3 ~/agent-core/examples/prepare_private.py --workspace ~/agent-core-private --config ~/.agent-core/host.json --runtime claude-code --runtime-root ~/.claude --git-name "Your Name" --git-email "you@example.invalid" --apply
```

3. 关联你的私有远端：

```bash
git -C ~/agent-core-private remote add origin <你的私有远端地址>
```

4. 推送初始提交：

```bash
git -C ~/agent-core-private push -u origin main
```

5. 安装。它先打印计划，再问 `确认执行？[y/N]`。确认远端是私有的，再输入 `y`。

```bash
sh ~/agent-core-private/engine/install.sh --config ~/.agent-core/host.json --state ~/agent-core-private/state --confirm-private-remote
```

6. 检查安装结果。没有 `FAIL` 就装好了。刚装好时 `lessons_hook` 一行是 `WARN ... retrieval_connected_unobserved`，意思是 hook 已经接好、还没被调用过；在 Claude Code 里发一条消息后再运行这条命令，它会变成 `PASS`：

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" doctor --config ~/.agent-core/host.json
```

<!-- /journey:first-machine -->

## 第二台机器

第二台机器 clone 同一个私有远端，生成自己的 host 配置，然后安装。下面以 Codex 为例。

<!-- journey:second-machine -->

```bash
git clone <你的私有远端地址> ~/agent-core-private
```

```bash
mkdir -p ~/.agent-core
```

```bash
python3 ~/agent-core-private/engine/examples/prepare_private.py --join --workspace ~/agent-core-private --config ~/.agent-core/host.json --runtime codex --runtime-root ~/.codex --apply
```

```bash
sh ~/agent-core-private/engine/install.sh --config ~/.agent-core/host.json --state ~/agent-core-private/state --confirm-private-remote
```

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" doctor --config ~/.agent-core/host.json
```

<!-- /journey:second-machine -->

`--join` 只写 host 配置。它不改私有仓库，也不替换已有文件。

## 日常同步

另一台机器推送了新经验后，先拉取：

```bash
git -C ~/agent-core-private pull
```

hook 直接读取私有仓库里的经验，拉取后就生效。规则和技能还要运行 sync：先预览；预览里 `planned_writes` 不是 0 时，再用预览给出的 `PLAN_HASH` 执行。

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" sync --config ~/.agent-core/host.json
```

```bash
"${XDG_DATA_HOME:-$HOME/.local/share}/agent-core/bin/agent-core" sync --config ~/.agent-core/host.json --apply --plan-hash <预览里的 PLAN_HASH>
```

## 计划和确认

- `install` 和 `sync` 都先给出计划，不确认就不写入。
- 在终端里，`install` 直接询问是否执行。没有终端的环境（例如由 Agent 运行）不会自动确认，要分两步：先运行同一条命令得到计划，再加上 `--apply --plan-hash <计划里的 PLAN_HASH>` 执行这份计划。
- 计划里有 `NEXT` 行时，表示有文件挡住了安装。按 `NEXT` 行处理那个文件，然后重新运行命令。
- `install` 要求私有仓库停在 `origin/main`，并且没有未提交、未跟踪或未合并的文件；inbox 里还没审核的候选也算。不满足时报 `state is not a clean origin/main snapshot`，后面写着原因和第一个挡住的文件。按提示提交改动，把候选入库或拒绝（或者先移到仓库外），然后重新运行。
