# Agent 协作说明

## 协作修改记录

### 2026-07-25：Codex Desktop 历史图片自愈

- 修改简介：新增 session 历史图片索引与安全恢复引擎，为 `apicodex` 接入显式修复命令、Desktop 启动前自动修复、账号与 API Profile 分流，并补充测试和使用文档。
- 修改原因：部分历史消息虽然仍在 session JSONL 中保存了图片数据，但对应的 Windows 临时文件已被清理，导致 Codex Desktop 持续显示图片加载状态；本次修改用于从本地 session 安全重建缺失文件。
- 验证情况：核心解析、安全边界、幂等性及 CLI 接入均有单元测试覆盖；提交前再次执行完整测试。

### 2026-07-25：ApiCodex 本机会话共享池

- 修改简介：新增 `apicodex share` 独立命令组、可移植会话清洗、EFS/专属 ACL 共享池、不可变版本对象、本地线程映射，以及通过目标 Codex app-server 创建独立线程的克隆流程。
- 修改原因：需要让账号与不同 API Profile 在不共用线程 ID、不复制隐藏加密推理、也不修改源会话的前提下，手动发布和接续同一份可见对话上下文。
- 验证情况：清洗、密文剔除、工具关联、compaction、篡改、路径、快进/分叉、dry-run、失败回滚及 CLI 均有测试；真实 EFS 池已建立，并保留跨 Profile 克隆用于 Desktop 验收。2026-07-26 修复了 `apicodex-portable` 模型占位符泄漏：池对象不再保存可被续聊误用的模型，克隆前按目标 Profile 临时物化 model/provider/cwd，并在登记映射前审计最终 rollout；旧会话和源会话均未修改或删除。
