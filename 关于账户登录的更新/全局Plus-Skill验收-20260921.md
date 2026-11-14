# 全局 Plus Skill 验收（2026-09-21）

已按用户要求取消逐项目目录登记，在现有当前 API profile 与账号 A 两个入口中，
允许选取任意已有项目目录。主模型每次传当前项目的绝对 cwd，B 继续使用原有
workspace-write 沙箱；此次没有把整个磁盘设为可写目录。

## 使用

在任意项目的新任务里输入：

```text
$plus-agent 让 B 检查这个项目的错误处理，你负责规划和验收。
```

也可从客户端 Skills 列表选择“Plus 账号子代理”；Codex CLI 支持 `/skills`。
没有注册自定义 `/plus-agent` 或 `/skill` 斜杠命令。现有会话需先重载 MCP，
或新开任务加载新版控制器及 Skill。

- 用户级 Skill：`C:/Users/SFM/.agents/skills/plus-agent/SKILL.md`。
- 仓库维护副本：`skills/plus-agent/`。
- 当前 API profile：`tiantiansub2api`；另一个来源：`account-a`；执行账号：`account-b`。
- 任意文件夹可用无需单独写 AGENTS.md。其他尚未注册 MCP 的 profile 能发现
  这个用户级 Skill，但仍需先配置自己的 MCP 来源；没有修改所有无关 profile。
- 界面仍显示 MCP 派发/等待调用，详细记录留在每个任务项目的 `子代理任务/`。

## 验证

- 全量回归 372 passed、16 skipped、218 subtests；新增用例验证旧模式仍拒绝
  目录外任务，全局模式可派发至新目录且将该 cwd 与原沙箱传给官方线程，恢复保持目录。
- 全局模式拒绝缺省 cwd、相对路径及不存在的目录；安装的两种目录参数互斥，
  dry-run 不写配置。没有任务范围时，Skill 询问具体任务。
- 官方 Skill 校验通过（Windows 使用 `python -X utf8`，避免校验器默认 GBK 解码错误）。
- 官方 app-server 在原先两个根目录之外的临时项目中，两来源均读回
  `plus-agent` 为唯一条目、`scope=user`、`enabled=true`，路径为上面的用户级位置；
  同时发现全部 6 个委派工具，`toolsError=null`。
- 两入口设置读回 `workspacePolicy=any`、`workspaces=[]`；原目标账号、沙箱、
  完整 MCP 配置和三项明确批准的信任策略相同。
- 此轮验证目录策略、Skill 发现和官方工具发现，没有新增真实 A/B 模型请求。
  真实模型完整链路的既有证据仍见 2026-09-18 验收记录。

## 备份

运行模块：`C:/tools/backups/delegate-global-20260921-085756/`。
两个 settings 的原始目录范围保存在 `C:/Users/SFM/.codex-api/delegation/` 下
相应的 `settings-*.before-global-20260921.json`；来源配置各有新的
`config.before-delegate-*.toml` 备份。

仓库和部署模块哈希核对、Python 3.10 语法、编译及 diff 检查完成后交付。
未重启用户 Desktop、未修改凭据、未提交或推送。
