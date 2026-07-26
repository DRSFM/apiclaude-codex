# Agent 协作说明

协作原则、安全边界与代码风格约束见 `CLAUDE.md`。所有模型完成改动后在本文件
「协作修改记录」追加一条简要记录（日期 + 标题 + 修改简介 / 修改原因 / 验证情况）。

## 协作修改记录

### 2026-07-26：ApiClaude 功能对齐（阶段二）

- 修改简介：接通 `apiclaude list --json` 非敏感元数据契约、`--vscode [NAME]`
  节点级 VS Code 用户目录与环境隔离、`--up` 官方更新命令及对应子命令别名；
  CLI/VS Code 启动会持久化 `lastUsedAt`，普通列表显示最近使用时间。
- 修改原因：完成 Fable 5 预留的 ApiClaude 阶段二骨架，使启动器与外部工具可以
  安全发现节点，并让 Claude 节点具备与 ApiCodex 对应的 VS Code 和更新入口。
- 安全说明：JSON 不读取或输出 token/凭据标识；VS Code 凭据仅经子进程环境传递，
  且清除父进程遗留的 Claude/API 环境；删除隔离节点改为归档成功后才删除登记与
  DPAPI 凭据，危险路径或归档失败会保留原状态。
- 验证情况：新增 7 项测试覆盖 JSON 无凭据泄漏与危险路径、CLI 路由、隔离/共用
  VS Code 环境、`lastUsedAt`、删除危险路径及归档失败保留状态；`python -m py_compile
  apiagent.py` 通过，完整 `python -m pytest tests/ -q` 共 94 项通过。

### 2026-07-26：ApiClaude 节点隔离/共用双模式（阶段一）

- 修改简介：Claude 节点新增 `isolation` 字段与 `apiclaude mode NAME isolated|shared`
  切换命令；隔离节点以 `CLAUDE_CONFIG_DIR=~/.apiclaude/nodes/<slug>` 启动，
  新节点默认隔离、旧节点保持共用 `~/.claude`；`list`/`current` 显示模式；
  删除节点时归档隔离目录到 `~/.apiclaude/archived-nodes`；同名 slug 冲突自动
  追加摘要后缀。另为 `tests/` 补充 `__init__.py`，避免环境级 `PYTHONPATH` 中
  他项目的顶层 `tests` 包遮蔽本仓库测试导致收集失败。
- 修改原因：apiclaude 功能对齐 apicodex 的阶段一——此前所有节点共用 `~/.claude`，
  会话与配置互相污染；按协作决定做成隔离/共用可随时切换，切换不迁移不删除数据。
- 安全与验证：隔离目录路径经安全校验（拒绝绝对路径、`..`、符号链接逃逸与指向
  `~/.claude` 账号态）；token 仍仅经环境变量传递。新增 7 项单元测试覆盖共用回退、
  隔离注入与持久化、不安全路径拒绝、双向切换保留数据、slug 冲突、删除归档；
  全量 `python -m pytest tests/ -q` 87 项通过。

- 修改简介：新增 `CLAUDE.md`，收录项目概览、协作规则（改动必录、先测后交、
  向后兼容）、安全边界（凭据不落明文与命令行、不碰账号态、隐藏字符清洗、
  dry-run/确认）与代码风格；`AGENTS.md` 顶部加入指引，明确本文件为共享改动日志。
- 修改原因：apiclaude 将对齐 apicodex 的多项功能，工作由多个模型协作完成，
  需要统一的协作约定与共享记录入口。
- 验证情况：纯文档改动，无代码变更；已确认与现有条目格式一致。

### 2026-07-25：Codex Desktop 历史图片自愈

- 修改简介：新增 session 历史图片索引与安全恢复引擎，为 `apicodex` 接入显式修复命令、Desktop 启动前自动修复、账号与 API Profile 分流，并补充测试和使用文档。
- 修改原因：部分历史消息虽然仍在 session JSONL 中保存了图片数据，但对应的 Windows 临时文件已被清理，导致 Codex Desktop 持续显示图片加载状态；本次修改用于从本地 session 安全重建缺失文件。
- 验证情况：核心解析、安全边界、幂等性及 CLI 接入均有单元测试覆盖；提交前再次执行完整测试。

### 2026-07-25：ApiCodex 本机会话共享池

- 修改简介：新增 `apicodex share` 独立命令组、可移植会话清洗、EFS/专属 ACL 共享池、不可变版本对象、本地线程映射，以及通过目标 Codex app-server 创建独立线程的克隆流程。
- 修改原因：需要让账号与不同 API Profile 在不共用线程 ID、不复制隐藏加密推理、也不修改源会话的前提下，手动发布和接续同一份可见对话上下文。
- 验证情况：清洗、密文剔除、工具关联、compaction、篡改、路径、快进/分叉、dry-run、失败回滚及 CLI 均有测试；真实 EFS 池已建立，并保留跨 Profile 克隆用于 Desktop 验收。2026-07-26 修复了 `apicodex-portable` 模型占位符泄漏：池对象不再保存可被续聊误用的模型，克隆前按目标 Profile 临时物化 model/provider/cwd，并在登记映射前审计最终 rollout；旧会话和源会话均未修改或删除。

### 2026-07-26：统一启动器启停响应优化

- 修改简介：在 `E:\新版codex工作区\codex皮肤` 中让统一启动器先显示 Profile 列表、后台扫描状态，并将启动器明确确认的停止操作正常退出宽限期由 15 秒缩短为 5 秒。
- 修改原因：减少管理器初次打开和 Electron 残留子进程关闭时的等待感，同时保留脚本默认行为及跨 Profile 隔离。
- 安全与验证：结束残留进程前仍校验精确 Profile、官方可执行文件路径和进程身份；Windows 回归与 WPF 测试通过，发布版替换前后现有 ChatGPT 进程 ID 保持不变。

### 2026-07-26：ApiCodex 与 Dream Skin 统一托盘整合

- 修改简介：API Desktop 启动成功后按官方可执行文件和精确 `user-data-dir` 自动标记主窗口为 `ChatGPT (Profile名)`；Dream Skin WPF 启动器作为统一托盘与可视化控制面，保留原三栏皮肤功能。
- 修改原因：本机并行运行账号与多个 API Profile 时，官方窗口和托盘名称相同，难以识别和安全管理。
- 安全边界：不修改官方 Store 包、`app.asar`、签名、认证或会话；标题失败不阻断启动，托盘退出不关闭 ChatGPT，停止与聚焦只使用已验证实例归属。
