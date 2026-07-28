# Agent 协作说明

协作原则、安全边界与代码风格约束见 `CLAUDE.md`。所有模型完成改动后在本文件
「协作修改记录」追加一条简要记录（日期 + 标题 + 修改简介 / 修改原因 / 验证情况）。

## 协作修改记录

### 2026-07-28：Claude Desktop 按节点隔离多实例与无感生命周期

- 修改简介：`apiclaude --desktop [--api-profile NODE]` 改为隐藏 worker 管理，
  每节点使用独立 `CLAUDE_USER_DATA_DIR`、动态回环端口、Desktop 配置和运行状态；
  新增状态、停止、前台诊断命令。worker 等待真实主窗口后才就绪，并在关闭动作将
  Claude 隐藏到托盘时识别主窗口持续消失，自动退出所属 Desktop 和 CPA 进程。
- 修改原因：需要达到与 `apicodex --desktop` 一致的选择上游即打开隔离窗口体验，
  支持多个 GPT Profile 并行使用，同时不保留桥接控制台或要求手工维护网关进程。
- 安全说明：上游密钥仍只从 DPAPI 进入内存认证转发器；每节点本地令牌彼此独立，
  Desktop 目录使用仅当前用户、SYSTEM 和管理员可访问的非继承 ACL。worker 命令行、
  环境和日志不含上游凭据，端口只监听回环地址，运行状态不记录令牌。
- 验证情况：官方 MSIX `1.24012.9.0` 包状态为 `Ok`；真实并行启动
  `codex-muyuan / gpt-5.6-sol` 与 `codex-prism / gpt-5.5`，确认端口、PID、令牌、
  用户数据和会话目录均分离，交叉令牌双向返回 401。使用指定问句时 prism 返回
  HTTP 200，muyuan 由真实上游报告 default 组暂无可用通道（HTTP 503）；停止前者
  不影响后者。修复 Claude 关闭到托盘后，模拟右上角关闭在 4.2 秒内清除窗口、
  worker、CPA、监听端口和运行状态。聚焦测试 39 项、全量测试 138 项通过，2 项
  按集成环境跳过，`py_compile` 与 `git diff --check` 通过。

### 2026-07-28：Claude Desktop 3P 接入 Codex Profile 原型

- 修改简介：CPA 桥支持固定回环端口和稳定本地认证令牌；新增
  `apiclaude --desktop --api-profile NODE [--desktop-port PORT]` 持久桥命令及
  `desktop-token` 显式取令牌命令，并补充官方 Desktop 3P 配置说明。Desktop
  专用桥对外使用目录可识别的 `claude-fable-5` 路由，CPA 内部强制映射到节点的
  真实 GPT 模型；CLI 桥模型名不变，Desktop 显示名要求标明真实 GPT 上游。
- 修改原因：Claude Desktop 已正式开放无需 Anthropic 账号登录的第三方推理网关，
  可复用现有 Codex Profile，让同一 GPT 上游同时体验 Claude Code 与 Desktop 外壳。
- 安全说明：本地网关令牌按节点随机生成并由 DPAPI 保存；上游 API Key 仍只在
  内存认证转发器中注入，不进入 CPA YAML、命令行或日志，桥仅监听回环地址。
- 验证情况：聚焦测试 23 项通过且测试前后真实配置哈希一致；CPA 集成 1 项通过、
  1 项旧 LiteLLM 路径跳过；全量测试 119 项通过、2 项按环境跳过，`py_compile`
  与 `git diff --check` 通过。使用指定问句经固定 `127.0.0.1:18765` 和真实
  `muyuan / gpt-5.6-sol` 验证返回 HTTP 200；同时发现节点旧 CPA 构建不识别禁图
  配置，已从干净 v7.2.101 tag 构建新版到 F 盘并在读回验证后切换节点引用；
  新版 Windows MSIX `1.24012.9.0` 的受限 AppUserModelID 启动回退亦有测试覆盖。
  针对 Desktop 拒绝非 Anthropic 目录路由的校验，新增 Fable 路由映射断言，并以
  `/v1/models` 确认只广告该别名；指定问句经别名真实请求再次返回 HTTP 200。

### 2026-07-27：共享会话兼容 Codex 延迟工具搜索记录

- 修改简介：共享池清洗器新增成对识别并保留 `tool_search_call` /
  `tool_search_output`，同时纳入调用与输出配对审计。
- 修改原因：当前 Codex Desktop 会话包含延迟工具发现记录，旧允许列表将其视为
  未知类型，导致安全发布在首个该记录处停止。
- 安全说明：仅新增两个已实测的记录类型；未知类型、孤立输出、隐藏推理、
  `encrypted_content` 与凭据清洗规则均保持不变。
- 验证情况：新增配对保留与 ID 清洗回归测试，先复现失败后修复；`py_compile`
  通过，全量测试 115 项通过、2 项按集成环境跳过。

### 2026-07-27：ApiCodex 共享池默认路径去除固定盘符

- 修改简介：移除 `E:\CodexConversationPool` 模块级硬编码；首次使用且尚无本地
  配置时改为 `%USERPROFILE%\CodexConversationPool`，`--pool` 与已保存路径仍
  保持优先。本机共享池已显式初始化到 `F:\CodexConversationPool`。
- 修改原因：原默认值来自另一台电脑，跨机器照搬会误用不存在或不应使用的 E 盘。
- 安全说明：误建的 E 盘共享池已移入回收站；F 盘池通过 EFS 与专属 ACL 验证，
  本地配置已写入并读回为 F 盘路径。
- 验证情况：新增默认路径跟随当前用户目录的回归测试；聚焦测试 23 项通过，
  `python -m py_compile` 通过，全量测试 114 项通过、2 项按集成环境跳过；
  `apicodex share doctor --api-profile muyuan` 确认池完整且克隆能力可用。

### 2026-07-27：GPT 桥节点限制 Claude API Skill 自动加载

- 修改简介：GPT 桥节点启动前无损合并隔离 `settings.json`，在用户未显式配置时
  将内置 `claude-api` Skill 设为 `user-invocable-only`；保留 `/claude-api`
  手动入口、既有设置及显式 Skill 选择，普通 Claude 节点不受影响。
- 修改原因：`gpt-5.6-sol` 在首轮模型身份问句中自动调用该 Skill，Claude Code
  2.1.220 将约 86 万字符的整套 API 参考注入消息，使一次问答从约 26k 跳至
  234.1k/200k；依赖不同模型自行判断是否调用不够稳定。
- 验证情况：测试先复现缺失默认值，再验证写入并保留主题及其他 Skill 设置；
  `python -m py_compile` 通过，全量测试 112 项通过、2 项按集成环境跳过。使用指定
  问句完成真实 `muyuan / gpt-5.6-sol` 新会话验证，仅调用 WebSearch/WebFetch，
  `Skill` 调用为 0，最终上下文约 25.1k。

### 2026-07-27：CPA 桥 Windows 流式断流处理修复

- 修改简介：将 `ConnectionAbortedError` 纳入桥接器已知客户端断流类型，在 CPA
  认证转发、Anthropic 请求入口及 SSE 写流层统一静默收尾；真正的 HTTP、上游
  连接与协议转换错误仍沿用原有报告路径。
- 修改原因：CPA 关闭已完成或取消的本地流后，Windows 可能在转发器下一次
  `wfile.write()` 抛出 `[WinError 10053]`；此前只处理 `BrokenPipeError` /
  `ConnectionResetError`，导致正常断流向终端打印 traceback。
- 验证情况：新增确定性测试模拟 CPA 在转发器写流时中止 socket，先复现未捕获
  `ConnectionAbortedError`，修复后通过；桥接聚焦测试 8 项通过，
  `python -m py_compile` 通过，全量测试 113 项通过、2 项按集成环境跳过。

### 2026-07-27：Claude-Codex 桥切换为 CPA

- 修改简介：新建或更新 `apiclaude bridge` 节点时改用 CLIProxyAPI v7.2.101
  执行 Anthropic Messages → OpenAI Responses 转换，新增 `--cpa-exe` 与可选
  `--proxy-url`；既有无 `gateway` 字段节点继续走 LiteLLM，保持向后兼容。
- 修改原因：以更轻量且针对 Codex Responses 内建适配的 CPA 取代新节点的
  LiteLLM 路径，同时复用现有 apicodex URL、模型和 DPAPI 凭据。
- 安全说明：CPA 临时 YAML 只包含固定的非秘密本地占位值；真实上游 API Key
  从 SecureStore 读入后仅保存在进程内存，由回环认证转发器注入，不写配置、
  不进命令行或日志；CPA、转发器及临时目录均随 Claude 进程退出清理。
- 验证情况：先以测试锁定 CPA 参数、节点元数据与配置无密钥，再用官方
  v7.2.101 二进制和模拟 Responses 上游验证 `/v1/messages` 流式往返；聚焦测试
  8 项通过、1 项按 LiteLLM 环境跳过，全量 `python -m pytest tests/ -q` 为
  112 项通过、2 项按显式集成环境跳过。真实 `muyuan / gpt-5.6-sol` 使用指定
  问句返回退出码 0；首轮发现并通过 `disable-image-generation: true` 修复上游
  分组无图像权限的 403，文本与普通工具链路保留。当前版本已部署到 Windows
  全局 npm PATH 目录；`apiclaude --api-profile codex-muyuan --version` 确认
  `gateway=cpa`，旧安装已从 Git 对应 blob 恢复并保存在时间戳备份目录。

### 2026-07-27：Claude-Codex 桥接日志告警修复

- 修改简介：将桥接器最低 LiteLLM 版本收紧到 1.93.0，并在启动时拒绝已知有
  Responses 日志缺陷的旧版本；本机仅升级 LiteLLM 本体，保留现有 Python
  依赖版本。README 同步最低版本和故障说明。
- 修改原因：LiteLLM 1.83.3 会在成功请求后错误序列化 Responses usage，
  1.83.14 至 1.84.10 又会把完成事件错误校验为 AnthropicResponse，分别造成
  Pydantic 警告和未回收的异步日志异常，污染 Claude Code 界面。
- 验证情况：集成测试新增连续两轮工具流、目标 Pydantic 警告和 asyncio 后台
  异常断言；LiteLLM 1.93.0 下单测 6 项、集成测试 1 项通过，全量
  `python -m pytest tests/ -q` 为 111 项通过、1 项按环境变量跳过；真实
  `codex-muyuan / gpt-5.6-sol` 多轮请求退出码 0，终端无警告或 traceback。

### 2026-07-26：ApiClaude 引用 Codex Profile 的 GPT 桥接原型

- 修改简介：新增 `apiclaude bridge CODEX_PROFILE [--name/--model]`，将现有
  Codex API Profile 引用为隔离 Claude CLI 节点；节点运行期在 `127.0.0.1`
  启动短时 Anthropic Messages → OpenAI Responses 流式桥，并注入主模型、
  子代理模型及默认模型映射；VS Code 暂明确拒绝该实验节点。
- 修改原因：在不复制 Profile 凭据、不混用 Claude 账号态的前提下，让同一套
  GPT API 模型也能使用 Claude Code 的交互与工具外壳，便于并行体验两种客户端。
- 安全说明：桥节点只保存 Codex Profile ID 和模型名；上游密钥运行时从 DPAPI
  读取且仅留内存，本机桥使用每次启动随机令牌并只监听回环地址；实验性 beta、
  自适应 thinking 与归因头默认关闭。2026-07-27 补充透明的 Codex 兼容客户端
  标识，兼容会拒绝 LiteLLM 默认 User-Agent 的 Profile 网关，不冒充官方版本。
- 验证情况：新增 5 项单元测试及 1 项可选协议集成测试，覆盖无凭据复制、CLI
  路由、运行期环境隔离、VS Code 边界，以及工具调用的双向流式转换；集成测试
  1 项通过，全量 `python -m pytest tests/ -q` 为 110 项通过、1 项按环境变量
  跳过，`py_compile` 通过。真机建立 `codex-muyuan` 隔离节点并以
  `claude --version` 完成无模型调用启动冒烟；2026-07-27 进一步以真实
  `claude -p` 请求验证 `muyuan / gpt-5.6-sol` 返回 `OK`。

### 2026-07-26：ApiClaude 命令风格对齐 ApiCodex

- 修改简介：`apiclaude` 支持与 `apicodex` 完全一致的旗标命令（`--api-add/--setup`、
  `--api-list [--json]`、`--api-profile <name>`、`--api-remove`、
  `--vscode [--api-profile]`、`--up`、`--api-help`），解析逻辑镜像 `codex_main`；
  `--api-profile` 带来免交互指定节点启动能力；`--api-remove` 无名字时进入
  交互式列表选择；旧子命令全部保留为别名。
- 修改原因：用户日常以 apicodex 的旗标习惯操作，要求两工具仅差 `claude`/`codex`
  一词；同时补齐"指定节点直接启动"这一原本缺失的能力。
- 验证情况：新增 11 项 CLI 路由测试（JSON 契约、--api-profile 直启与透传、
  错误路径、别名路由、交互删除、裸参数透传）；调整 1 项旧测试适配 `--vscode`
  新语义（位置参数改经 `vscode` 子命令）。全量 `python -m pytest tests/ -q`
  105 项通过；真机 `--api-list`、`--api-profile ghost` 错误路径冒烟通过。

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
  2026-07-26 真机验收通过：5 个既有节点以共用模式正常列出且 JSON 契约无泄漏；
  临时隔离节点完成 添加→选择启动（`CLAUDE_CONFIG_DIR` 正确注入与建目录、
  `home`/`lastUsedAt` 持久化）→双向切换→删除归档→凭据清除 全链路，
  真实节点与 `current` 未受影响。注意：`add` 的 token 输入经 `getpass`
  读控制台而非 stdin，自动化调用需绕开（交互使用不受影响）。

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
