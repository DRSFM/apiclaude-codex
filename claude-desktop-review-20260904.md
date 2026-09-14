# apiclaude --desktop 现状评审与补齐建议（2026-09-04）

评审人：Claude Code（运行在 anyrouter 隔离 Desktop 节点内，即被评审的通道本身）。
范围：工作区未提交改动中与 Claude Desktop 相关的部分（`apiagent.py`、
`claude_codex_bridge.py`、`claude_desktop_windows.py`、对应测试）。只做检查与
探测，未修改任何源码；两个临时探针脚本已删除。Codex 同时在改，本文以
AGENTS.md 截至「原生节点默认 1M 与自动压缩」条目为基线。

## 1. 实机现状（已确认）

| 项目 | 观测值 |
| --- | --- |
| Desktop 版本 | MSIX 1.44121.4.0 |
| 当前节点 | anyrouter，worker PID 35972，Desktop PID 77144，网关 127.0.0.1:64192，gateway=anthropic |
| 渲染进程 deploymentMode | 3p（Claude-3p 目录内 `claude_desktop_config.json`） |
| 本会话环境 | `CLAUDE_CONFIG_DIR` / `LOCALAPPDATA` 均指向节点私有目录；`CLAUDE_USER_DATA_DIR` 为空（新版打包构建启动即删除） |
| Code 会话拿到的 tier 环境变量 | `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-5`、`ANTHROPIC_DEFAULT_FABLE_MODEL=claude-fable-5-1`，haiku/sonnet/mythos 为空串 |
| 模型配置 | 显式覆盖 opus-5 + fable-5-1，Context: prefer 1M；`[1m]` 变体可正常对话 |
| 测试 | `python -m pytest tests/ -q`：238 passed, 12 skipped（评审开始时；Codex 之后已加到 245） |
| 凭据 | 命令行、runtime.json、worker.log、Desktop 配置均无上游 Token，符合安全边界 |

结论：隔离窗口、3P 配置发现、原生透传、1M 声明这条主线已经跑通，本文
其余部分是在此基础上的缺陷与补齐。

## 2. 已实锤的缺陷（按优先级）

### P0 · 透传网关不逐块转发 SSE（`claude_codex_bridge.py:271`）

探测方法：本地假上游每 0.6 s 发一个 SSE 事件，共 3 个，经
`anthropic_passthrough_bridge` 用 `read1()` 在客户端逐块计时。

```
[chunked=True]  client chunk arrivals: [(1.84s, 75 bytes)]   # 上游结束后一次到达
[chunked=False] client chunk arrivals: [(1.22s, 75 bytes)]
```

原因：`upstream_response.read(64 * 1024)` 在 `http.client` 中会阻塞到凑满
64 KiB 或 EOF（chunked 走 `_read_chunked(amt)` 也是累积到 amt）。结果是
Desktop/Claude Code 看到的"流式"其实是整段落地后才显示，工具调用要等整轮
生成完才触发。这就是当前 Desktop 里回复"卡一下再整段出来"的根因。

修法：SSE（`text/event-stream`）用 `readline()` 逐行转发；其他响应用
`read1(64 * 1024)`（至多一次底层读即返回）。同一模式还出现在
`claude_codex_bridge.py:553`（CPA 认证 shim 的非别名分支）和
`codex_vision_proxy.py:884`，建议一并改。加一条"慢上游 + 到达时间断言"的
单测，否则这类回归测不出来（现有透传测试只比对最终字节）。

### P1 · Desktop 主进程直连请求在"仅 1M"上游上全部 400

`Claude-3p/logs/main.log`：

```
[custom-3p] ConfigHealth recomputed { state: 'config_model_rejected', provider: 'gateway' }
[title-gen] direct request failed { model: 'claude-opus-5',     kind: 'model_rejected', HTTP 400 }
[title-gen] direct request failed { model: 'claude-opus-5[1m]', kind: 'model_rejected', HTTP 400 }
[title-gen] cli failed { model: 'claude-opus-5[1m]', code: 1 }
```

app.asar 中搜不到 `context-1m-20`，说明 Desktop 主进程自己发的请求
（健康探测、会话标题生成、以及聊天模式）不会加 1M beta 头；`[1m]` 只有
Claude Code CLI 会解析成 `anthropic-beta: context-1m-2025-08-07`（已在捆绑的
claude.exe 2.1.258 里确认该 beta ID）。AnyRouter 要求 1M 请求变体，于是
裸模型 400、带 `[1m]` 的字面 ID 也 400，健康状态停在 `config_model_rejected`，
会话标题一直生成失败。

修法（放在透传层，节点级）：当节点 `desktop_models_support_1m` 为真时，
对 `/v1/messages` 与 `count_tokens` 的请求体做两件事：
1. `model` 以 `[1m]` 结尾则去掉后缀；
2. `anthropic-beta` 不含 `context-1m-2025-08-07` 时合并注入。
这样 Desktop 自身请求、CLI 请求、健康探测走同一套语义，`--standard` 节点
不受影响。需要读改请求体（目前是原样转发），注意保留非 JSON 体原样透传、
体积上限沿用 64 MiB。

### P2 · 节点根目录已不是有效 userData，状态输出与磁盘都在误导

asar 启动逻辑（打包构建）：先 `delete process.env.CLAUDE_USER_DATA_DIR`，
再由 `%LOCALAPPDATA%\Claude-3p` 判定 3P 并 `app.setPath("userData", …)`。
实机子进程 `--user-data-dir` 已全部指向 `.desktop-localappdata\Claude-3p`，
根目录在本次启动中只被我们自己写了配置镜像，Chromium 未再触碰。

后果：
- `runtime.json.userDataDir` 与 `--desktop` 打印的 "User data:" 指向根目录，
  真实数据目录是 `<root>\.desktop-localappdata\Claude-3p`。建议 runtime state
  增加 `effectiveUserDataDir`，状态/启动输出改为显示它。
- anyrouter 根目录残留 288 MB 首轮 1p 启动数据（其中 `claude-code/` 254 MB 是
  旧的捆绑 CLI 副本，Claude-3p 下又下载了一份 218 MB）。建议提供
  `apiclaude --desktop-prune NODE [--dry-run]`，只删已知 Chromium 缓存目录与
  旧 `claude-code/`，不碰 `.apiclaude-runtime`、`claude-code-config`、
  `cowork-files`、配置镜像。
- 旧版布局迁移：codex-prism / codex-zzzcoding / zzzcoding 三个节点的
  `local-agent-mode-sessions`（各约 3.8 MB）、`IndexedDB`、`config.json`、
  `window-state.json` 仍在根目录；用新启动器打开时 Desktop 会在空的 Claude-3p
  里从零开始，历史不可见。按 CLAUDE.md 第 3 条，应在 Claude-3p 不存在且根目录
  有旧状态时做一次"复制 → 读回校验 → 打标记"的迁移，不删旧数据。
- `--user-data-dir=<root>`（`claude_desktop_windows.py:542`）在 LOCALAPPDATA
  已按节点隔离后很可能只剩"保险"作用（单实例锁在 setPath 之后请求）。可保留，
  但根目录那份 `claude_desktop_config.json` + `configLibrary` 镜像只对旧版
  Desktop 有意义，README 里应写明。

### P2 · 透传路径白名单偏窄（`claude_codex_bridge.py:52`）

捆绑 Claude Code 与 Desktop 都引用 `GET /v1/models/{id}`，此外还有
`/v1/files*`、`/v1/skills*`、`/v1/messages/batches*`、`/v1/complete`。
目前只放行 `/v1/messages`、`/v1/messages/count_tokens`、`/v1/models`，其余
返回 404 JSON。建议：至少放行 `GET /v1/models/{id}`；对被拒路径在 worker.log
记一行"method path"（不记头、不记体），方便后续按需扩白名单。

## 3. 对 Codex 最新改动的意见（原生节点默认 1M）

`claude_desktop_models_support_1m()` 现在在节点未显式设置时对所有非桥接节点
返回 True，CLI 也默认把模型改成 `[1m]` 并注入 `--autocompact auto`。风险：

- 这是对既有节点（muyuan、zzzcoding、ccmaxbug1）的默认行为变更。上游若不
  接受 1M beta，会从"能用"变成 400，违反"不破坏既有行为"原则。
- 建议改为：新建节点默认 1M、已有节点保持原值（迁移时显式写入
  `desktop_models_support_1m: false`），或首次请求 400 且错误文本提到 1M 时
  给出明确提示，而不是静默默认。
- `--1m` 目前对 CPA 桥接节点也能写入标志但 worker 忽略，应在 CLI 层直接拒绝
  或警告。

## 4. 其余补齐建议

1. **tier 默认模型补齐**：Desktop 把每个 `anthropicFamilyTier` 的
   `isFamilyDefault` 映射成 `ANTHROPIC_DEFAULT_<TIER>_MODEL`；缺失的 tier 传空串，
   Claude Code 回退到内置 ID（当前构建内置 sonnet 默认为 `claude-sonnet-5`，
   AnyRouter 列表里没有）。显式覆盖只写 opus/fable 时，`/model sonnet`、后台
   haiku 任务会撞到不存在的模型。建议 `desktop-models` 在缺 haiku/sonnet 时
   警告，或从最近一次发现列表里按 tier 自动补最新一款。
2. **自动发现的排序**：`--auto` 下 `isFamilyDefault`/`prefer1m` 落在上游顺序
   的第一项（AnyRouter 是 `claude-3-5-haiku-20241022`）。建议按版本号取每个
   tier 最新者作默认，并把最高 tier 的默认放到列表首位。
3. **利用 `/v1/models` 元数据**：Desktop 自带发现会读 `supports_1m` /
   `max_input_tokens >= 1e6` / `anthropic_family_tier` / `is_family_default`。
   `discover_anthropic_models` 可顺手读取这些字段，为后续"自动判定 1M"留口。
4. **`LOCALAPPDATA` 覆盖的副作用**：整棵进程树都被重定向，节点私有目录里已
   出现 `GitHub CLI`、`Microsoft`、`claude-cli-nodejs`。会影响依赖该变量的
   开发工具（uv 的 Python 缓存、Playwright 浏览器、npm/pip 缓存会重新下载）。
   建议在节点 `claude-code-config/settings.json` 合并写入
   `env.LOCALAPPDATA=<真实值>`，让 Code 会话及其 Bash/MCP 子进程恢复真实路径；
   Desktop 主进程保持私有值。不建议改成删除 LOCALAPPDATA：asar 中日志路径回退
   会落到官方 `%APPDATA%\Claude`。
5. **原生节点的 web_search**：目前只剩 Bing RSS 兜底。可让 `claude_gateway_mcp`
   经本地网关调用 Messages 的 `web_search_20250305` 服务端工具（只需本地令牌，
   不接触上游 Token），上游支持则用，不支持再回退。
6. **上游 429 观测**：`cli-diagnostics.jsonl` 显示本会话对 opus-5[1m] /
   fable-5-1[1m] 连续 11 次 429 后才成功，首条回复 40 s。属于 AnyRouter 限流，
   不是网关问题，但与 P0 的缓冲叠加会被用户感知为"网关很慢"。
7. **每节点一份 218 MB 捆绑 CLI**：隔离的必然代价，四个节点已 ~1.3 GB，README
   应提示。

## 5. 建议的落地顺序

1. P0 流式修复 + 慢上游单测（改动小、收益最大，三处同模式一起改）。
2. P1 透传层 `[1m]` 规范化与 beta 注入，观察 `config_model_rejected` 与
   title-gen 是否消失。
3. 默认 1M 的兼容性收口（第 3 节）。
4. 有效数据目录报告、旧布局迁移、prune 命令。
5. tier 默认补齐、白名单扩展、settings.json 恢复 LOCALAPPDATA。

## 6. 证据来源

- 探针：本地 `ThreadingHTTPServer` 假上游 + `anthropic_passthrough_bridge`，
  分别测 chunked / content-length 两种编码（脚本已删除，未入库）。
- `%LOCALAPPDATA%` 私有目录下 `Claude-3p/logs/main.log`、
  `cli-diagnostics.jsonl`；`.apiclaude-runtime/runtime.json`、`worker.log`。
- `C:\Program Files\WindowsApps\Claude_1.44121.4.0_x64__pzs8sxrjxfjjc\app\resources\app.asar`
  中的 3P 路径解析、`prefer1m`/`isFamilyDefault` 说明文本、ConfigHealth 枚举、
  `ANTHROPIC_DEFAULT_${tier}_MODEL` 生成逻辑。
- 捆绑 `claude-code/2.1.258/claude.exe` 中的 `/v1/*` 路径与 `context-1m-2025-08-07`。
