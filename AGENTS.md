# Agent 协作说明

## 协作修改记录

### 2026-07-25：Codex Desktop 历史图片自愈

- 修改简介：新增 session 历史图片索引与安全恢复引擎，为 `apicodex` 接入显式修复命令、Desktop 启动前自动修复、账号与 API Profile 分流，并补充测试和使用文档。
- 修改原因：部分历史消息虽然仍在 session JSONL 中保存了图片数据，但对应的 Windows 临时文件已被清理，导致 Codex Desktop 持续显示图片加载状态；本次修改用于从本地 session 安全重建缺失文件。
- 验证情况：核心解析、安全边界、幂等性及 CLI 接入均有单元测试覆盖；提交前再次执行完整测试。
