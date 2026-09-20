# DeepDive 使用手册 · Research 深度研究

## 创建研究任务

1. 打开左侧 **Research** 标签,点顶部 **＋ New Research**(或在 Chat 头部点 **＋ Research**,同一个对话框)。
2. 对话框字段:
   - **Working directory / 父文件夹**:为任务选择 My Drive 下的父文件夹;
   - **Task title**:必填,任务标题;
   - **Description**:任务描述(留空则使用系统默认研究指令);
   - **Materials**:从云盘挑选参考资料(＋ Add files,每行 × 可移除);
   - **Execution mode**:二选一,`progressive`(默认,逐步可干预)或 `strict`(全自动严格执行)。创建后锁定,后续 resume 都按此模式。
3. 点 **Create task**。系统原子创建任务目录(`materials/`、`outputs/`、`temp/`,并镜像 `task_spec.json`、`session_history.json`),同时为该任务绑定一个专属聊天会话,Chat 面板自动切到这个绑定的新任务 chat,旧 chat 关闭。

## 运行与监控

- 任务卡片上:**▶ Run** 一键开始/继续(打开专属会话并自动发送运行指令);**⏹ Stop** 协作式取消;**🗑 Delete** 删除任务(需确认;任务 RUNNING 或报告已被索引时禁止删除)。
- 左侧状态面板显示 10 阶段流程(DISCOVER → … → PUBLISH)的当前 stage、gate 状态与 last_block 摘要;主面板是任务工作区目录树。**Evidence graph** 卡片上的 **View graph** 在右缘抽屉打开证据图。
- 某个 gate 失败/暂停时,任务会话聊天内会出现 **Approve / Reject** 卡片:Approve 记录裁定并自动继续运行;Reject 保持 gate FAIL 并要求 agent 换方案。
- 也可以直接在任务的绑定会话里输入指令(如 "继续"、"补充 XX 部分"),即 chat-driven run。

## 空白 Research chat 的语义

- 点 Research 标签但没有选中任何任务时,Chat 显示临时空白聊天:可以在这里闲聊提问,但它**不会保存**到会话历史(不入库的抛弃型聊天)。
- 选中任务或新建任务后,聊天面板才切换到任务绑定的专属会话;只有绑定任务的 chat 会被记录。

## 产出物

- `outputs/<任务名>.md` 实时镜像报告;历史版本在 `temp/vN/`;
- 完成 PUBLISH 后自动编译 PDF:`outputs/<任务名>_vN.pdf`;
- 报告可在 Chat 工具栏进一步生成 Slides / Mindmap / Summary(见「内容生成」手册)。
