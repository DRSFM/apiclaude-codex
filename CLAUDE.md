# CLAUDE.md — 项目协作原则

本项目由多个模型（Claude Code、Codex 等）协作开发。本文件与 `AGENTS.md` 配套：
本文件描述协作原则与项目约束，`AGENTS.md` 的「协作修改记录」是所有模型共享的
改动日志。

## 项目概览

apiagent / apicodex / apiclaude：为 Codex CLI 和 Claude Code 提供多 API Profile
隔离启动。核心模块均为仓库根目录下的单文件 Python：

- `apiagent.py` — 三个入口的主体逻辑（Codex Profile 管理 + Claude 节点管理）
- `secure_store.py` — Windows DPAPI 凭据加密（`~/.apiagent-secrets`）
- `codex_conversation_pool.py` / `codex_share_cli.py` — `apicodex share` 本机会话共享池
- `codex_history_images.py` — Desktop 历史图片自愈
- `codex_app_server.py` — Codex app-server 交互
- `codex_desktop_windows.py` — API Desktop 窗口标记
- `tests/` — 单元测试；`tauri-app/`、`web/` — GUI 前端

## 协作规则（每次改动必做）

1. **记录改动**：完成一项修改后，在 `AGENTS.md` 的「协作修改记录」追加一条，
   格式沿用现有条目：日期 + 标题，然后写三行——修改简介、修改原因、验证情况
   （涉及安全边界时补一行安全说明）。保持简要，几句话即可。
2. **先测后交**：改动核心 Python 模块必须有对应单元测试（放在 `tests/`），
   提交前运行完整测试：`python -m pytest tests/ -q`（Windows 下用 `python`）。
3. **不破坏既有行为**：默认行为、命令兼容性、配置文件格式变更需向后兼容或
   提供迁移路径；迁移必须"写入并读回验证后再删除旧数据"。

## 安全边界（不可违反）

- API key / token **绝不放命令行参数、绝不写明文文件**；只经子进程环境变量或
  stdin 传递，持久化必须走 `SecureStore`（DPAPI）。
- **不读写账号态**：`~/.codex` 与 `~/.claude`（账号登录数据）除显式 `--account`
  类命令外一律不碰；API Profile 数据隔离在 `~/.codex-api`、`~/.apicodex-*`、
  `~/.apiclaude*` 下。
- 保存或传递凭据前用 `clean_hidden_prefix` 清洗 BOM / 零宽字符。
- 危险操作（删除、覆盖、修复写入）提供 `--dry-run` 或确认提示；失败时报告
  而非静默降级。

## 代码风格

- Python 3.10+，标准库优先，无第三方运行时依赖。
- 类型标注沿用现有风格（`dict[str, Any]`、`| None`）。
- 面向用户的 CLI 输出为英文；`AGENTS.md` 协作记录为中文。
- Codex 与 Claude 两侧功能对等时，命名和结构保持镜像
  （如 `codex_*` / `claude_*` 函数对）。

## 当前进行中的工作

apiclaude 功能对齐 apicodex（详见 AGENTS.md 后续记录）：

1. 阶段一（已完成，2026-07-26）：每节点隔离/共用双模式，`apiclaude mode` 切换，
   新节点默认隔离、旧节点保持共用，切换不迁移数据。
2. 阶段二（已完成，2026-07-26）：`list --json` 机器可读契约、`--vscode`
   节点级 VS Code 用户目录与环境隔离、`--up` 官方更新、`lastUsedAt`，并将删除
   流程收紧为隔离目录归档成功后才删除节点登记和凭据。
3. 实验性原型（已完成，2026-07-26）：`apiclaude bridge CODEX_PROFILE` 将现有
   Codex API Profile 以引用方式映射为隔离 Claude CLI 节点；运行期启动本机短时
   CPA Anthropic Messages → OpenAI Responses 桥（旧节点保留 LiteLLM 兼容路径），
   暂不接入 VS Code。
4. Desktop 3P 原型（进行中，2026-07-28）：官方已开放无需 Anthropic 账号登录的
   Claude Desktop 第三方推理网关；CPA 桥节点可用固定回环端口和 DPAPI 本地令牌
   接入 Desktop。该路径不复用 `CLAUDE_CONFIG_DIR`，且 GPT 协议转换仍属实验能力。
- 不同步：历史图片修复（Codex 特有损坏模式）；share 共享池 Claude 版暂缓，
  等出现真实跨节点续聊需求再立项。
