# Lessons Ledger
<!-- lessons-schema: lessons-ledger/2 -->
<!-- lessons-scope: global -->

## 活跃

- **L-1 [enforced·通用] 改动共享常量时，必须搜索旧字面量本身并检查全部消费者。** 触发: 修改共享常量、schema 或 invariant。 代价: 局部修复后仍有消费者保留冲突真值。 verifier: seed-shared-literal. sink → templates/check_shared_literal.py. when: {"paths":["src/constants.py"]}
- **L-2 [checklist·通用] 跨机器物化必须运行时探测差异，不能把本机相等当成跨机一致。** 触发: 修改 runtime adapter 或跨机 materialization。 代价: 一台机器全绿而另一台静默使用旧能力。 verifier: seed-task-card. sink → templates/task-card.md. when: {"paths":["runtimes/**"]}
- **L-3 [pending·通用] 结构化字段交给机器，说明文案交给人；创建/更新/晋升使用同一份 canonical 数据。** 触发: 设计同时面向程序与人的状态或报告。 代价: 程序解析散文，或用户只能阅读难懂的机器字段。 sink → templates/audit-checklist.md. when: {"tasks":["build"]}
- **L-4 [enforced·通用] 新建 canonical 定义前，必须枚举全部副本、生成物与消费者。** 触发: 新增或迁移 canonical 文件、schema 或 registry。 代价: 新真值与旧副本并存，产生不可解释的分叉。 verifier: seed-shared-literal. sink → templates/check_shared_literal.py. when: {"paths":["manifest.yaml"]}
- **L-5 [pending·通用] 先测量真实数据的规模、分布与异常，再冻结设计。** 触发: 根据数据实况选择算法、阈值或存储结构。 代价: 设计建立在想象的输入上，真实数据到来后整体返工。 sink → templates/audit-checklist.md. when: {"tasks":["research"]}
- **L-6 [checklist·通用] 等待与重试属于程序控制流，不属于模型轮询。** 触发: 启动长任务、后台任务或外部进程。 代价: 会话浪费调用预算，并且无法形成可靠的终态证据。 verifier: seed-task-card. sink → templates/task-card.md. when: {"cmds":["python batch.py"]}
- **L-7 [checklist·通用] 隐私闸必须先于内容进入 Git 历史，并在 docs/privacy-review.md 记录判据。** 触发: 准备提交公开内容、fixture 或示例。 代价: 工作树可修复，已发布历史却无法原地清除泄漏。 verifier: seed-audit-checklist. sink → templates/audit-checklist.md. when: {"paths":["docs/**"],"tasks":["audit"]}
- **L-8 [pending·通用] 脚本化 Git 文件事务只对已跟踪内容拥有可验证的回退面。** 触发: 脚本移动、删除或暂存候选文件。 代价: 未跟踪内容删除后无法由 Git 恢复。 sink → templates/task-card.md. when: {"cmds":["git mv"]}

## 归档
