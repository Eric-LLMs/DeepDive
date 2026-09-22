# Chat 控制平面重构 — Pre-Implementation 勘察文档

本文档是 Chat 架构重构(分流控制平面)的实施前勘察,将重构方案逐组件映射到当前代码,
明确调用图、协议契约与"必须改 / 禁止改"文件边界。所有行号基于勘察时的 main 分支。

## 1. 现有调用图与 Chat 生命周期

```text
POST /chat ────────────────┐                      POST /chat/stream ─────────────┐
                           ↓                                                 ↓
   [内联前置:两路由各一份复制]            (chat.py L793-932  vs  L1040-1195)
   1. set_request_user(user_id)                 — ACL/检索范围 ContextVar
   2. 鉴权/游客身份/配额  (SessionLocal + Redis)  — resolve_guest_identity / _guest_quota
   3. 渠道路由          (_resolve_chat_route)    — 无渠道→降级 anonymous→仍无→503
   4. set_request_llm_channel(model,url,key)     — 子调用同渠道钉定
   5. attach note / inline image (_attach_note / _resolve_inline_image)
   6. handoff note (_handoff_note)
   7. viewer 装配+权限 (_build_viewer_assembly → build_viewer_blocks)
      → too_large/unavailable 短路 (L881/L1130,不进 agent)
   8. create_session / research 绑定 (_resolve_research_context, begin_run 互斥槽)
   9. SessionMemoryStore 构造 + _assemble_turn_history (v2 零读 / legacy recovery SQL)
                           ↓                                                 ↓
   get_agent().run(...)   (L935)                get_agent().run_stream(...)  (L1230,
                                                 经 pump 任务 + ApprovalStore 队列)
                           ↓                                                 ↓
   finally: research 交棒 (_maybe_continue_research) / 非 research 断连 cancel
   SESSION_FINALIZE enqueue → _log_usage → flush_writes 取消息 id
   → _extract_retrieval / _viewer_post_turn → done 帧 / JSON 响应
```

**核心问题**:1-9 步在前置阶段对两路由各复制一份;步 10 无条件进入 `get_agent()`,
任何闲聊都要加载 Tool Catalog、memory brief、prompt 分区装配并走完整 ReAct 循环。

## 2. Agent 入口与 SSE 事件契约

### Agent 入口(`packages/agent/engine/`)

| 成员 | 位置 | 说明 |
|---|---|---|
| `AgentKernel.run / run_stream` | kernel.py L286-363 | 唯一对话入口;`run_stream` 纯委托 `ReactLoopAgent` |
| `KernelConfig` | kernel.py L60-66 | `max_steps=5`、`max_parallel_tool_calls=10`、`recall_top_k=5` |
| `ReactLoopAgent` | loop.py L103-121 | 每步取 `gateway.visible_schemas`(常驻全 schema + 延迟 stub) |
| 组合根 | apps/api/agent_factory.py L131-241 | `get_agent_kernel()` lru_cache;API 与 worker 共用 |

**签名不对称**:`run_stream` 接收 `disable_thinking`(kernel.py L339/L344-345),`run()` 不接收;
chat.py 流式路径 L1243 传入 `body.disable_thinking or viewer.mode=="focus"`。新控制平面必须保留该语义。

### SSE 事件 schema(现无独立协议模块,散在三处)

| 事件 | 产生点 | 载荷 |
|---|---|---|
| `thinking` | loop.py L415 | delta 字符串 |
| `content` | loop.py L418 | 用户可见增量(**Commit Point 判定源**) |
| `step-answer` | loop.py L452 | 每步正文 |
| `tool` | loop.py L460 | `{"name": ...}` 派发前通告 |
| `tool-start` / `tool-result` | loop.py L767/L777,经 progress_sink→队列 | 工具执行状态 |
| `approval-request` | approvals.py L210-221,经 ApprovalStore sink | `{approval_id,name,arguments,reason}` |
| `viewer` | chat.py L1132 | 短路帧 `{mode,status,rejected}` |
| `error` | loop.py L432/L464 | 致命 LLM 错 / 预算超支 |
| `done` | loop.py L507 → chat.py L1417 重建 | `{answer,session_id,user_id,user_message_id,assistant_message_id, retrieved?,viewer?,compaction?,compaction_deferred?,persist_failed?,guest_token?,research_continuing?,notice?}` |

翻译层 = chat.py `gen()` 的 `json.dumps` 透传(L1355)。citation 无独立流内帧,
全部收敛在 `done.viewer`(citations/cited/invalid)与 `done.retrieved`。

## 3. RAG 能力边界与 Viewer 流

### RAG(`packages/rag/pipeline/`)

- 执行器:`executor.py` L58-92 `_run` 遍历 `config.enabled_nodes` → `registry.create(name, params)` → `node.run(ctx, deps)`;单节点异常仅入 `ctx.errors`。
- 配置驱动:`pipeline_config.py` L35 节点列表即拓扑(默认 query_rewrite→vector→keyword→rrf→cross_encoder);`registry.py` L62-90 注册 7 节点;持久化于 `app_settings["rag"]` JSON。
- 租户/ACL 不在 pipeline 内:工具层 `rag_search_tool.py` L24 从 ContextVar 取 `filters={"user_id": ...}` → 召回节点 → `PgVectorStore.search` 谓词(`visibility.py` L31-80)。Executor 严禁绕开该链直连向量表。
- 缓存:`CachedRetriever`(query_cache.py L65-100)键含 filters+config_version+corpus_version,在 agent_factory L150/L161 `wrap_retriever` 挂上。
- 失败契约:`RetrievalUnavailable`(executor.py L28)——全部排序通道失败才抛;这是 Fail-Closed 的现成挂点。

Staged RAG(Phase 4)只能扩展现有 `enabled_nodes`/registry/ordering/cache/telemetry 契约,不建平行管道。

### Viewer 流

- 纯装配层 `apps/api/viewer_context.py`:`build_viewer_blocks`(L178-326)输出 status ∈ `none|injected|stub|too_large|unavailable`;权限检查在 chat.py `_asset_readable`(drive.ensure_asset_readable)。
- `injected` → 经 turn context 由 `viewer_reference_section`(L450-470,kernel DYNAMIC_SUFFIX zone L226-228)渲染,**不混入 user_text**;`stub` → 模型经 `read_document` 工具按需读取("Open != Inject" 已落地)。
- 后置:`_viewer_post_turn`(chat.py L295-333)落 snapshot + citation 校验。

## 4. Memory 召回触发与依赖

- `MemoryService`:`packages/agent/memory/service.py`。`begin_session()`(L51)每 turn 载 MEMORY.md 前 200 行简报(常驻);
- **按需召回门控**:`should_recall(query)`(L91-105)廉价词法预筛(长度阈值 + 触发词表),`_memory_recall_section`(kernel.py L209-235)门控后才 `recall_all`(RRF 双轨 PG 查询),结果缓存在 `turn.recall_hits` 每 turn 一次。
- 结论:`requires_memory` 水合资格已存在,`MemoryService.should_recall()` 保持权威裁决,`understanding.py` 不得复制召回策略。

## 5. 关键事实(对方案的直接修正)

1. **历史已是纯净 user/assistant 文本**:v2 tail schema `ChatTurnMessage.role ∈ {user,assistant}`(schemas.py L121);legacy recovery `load_session_messages`(memory.py L828-846)**只返回 user/assistant 行,tool 行不入 prompt**。方案中"Agent wire format 泄漏"风险不存在;`sanitization.py` 职责收敛为:DIRECT 前的最终事实级校验/截断与来源标注,而非重渲染。
2. **前置阶段的 I/O 无法归零**:鉴权/配额/渠道路由(chat.py L1063-1099)必然含 DB+Redis 读,viewer 装配含 drive 权限读。`context.py` 的"零新增 I/O"定义为:**不新增**——复用既有一次读取,把 viewer/attach 等重资源解析标记为 lazy hydration 点。
3. **不存在 CancellationToken 类**:取消 = `asyncio.Task.cancel()` 传播 + 未接线的 `turn.cancel_token`(context.py L84,全库无 set 点)。新 `CancellationToken` 抽象若引入仅作 wrapper,不改 loop 语义。
4. **断连语义是显式分支,必须逐字保留**:research 轮 `await pump_task`(等跑完+落库+交棒)vs 非 research 轮 `pump_task.cancel()`(chat.py L1356-1402,T4 invariant #3)。
5. **approval 死锁约束**:approval 阻塞时 `async for run_stream` 会死锁,pump+queue 解耦(L1209-1219 注释)是控制平面必须继承的结构。
6. **DIRECT 直调 LLM 有先例**:`plugins/research/plugin.py` L547 `complete_stream`(全库唯一生产例);`agent_factory.llm` 单例 + `_ChannelAwareLLM` 渠道钉定可直接复用。
7. **前置复制代码是首要合并对象**:`/chat` 与 `/chat/stream` 的 1-9 步是逐行复制(L793-932 vs L1040-1195),控制平面抽取天然消除。

## 6. 文件映射:必须改 vs 禁止改

### 新建(`packages/core/application/chat/`,目录当前不存在,无冲突)

| 新文件 | 承载的现有逻辑 | 来源行号 |
|---|---|---|
| `turn_orchestrator.py` | gen() 骨架:事件泵、commit guard、finally 分支 | chat.py L1199-1417 |
| `understanding.py` | 无(新增;viewer mode 正则 `classify_viewer_mode` 可作 cheap signal 参考) | viewer_context.py L93-107 |
| `execution_plan.py` | 无(新增纯函数) | — |
| `context.py` | 前置 1-9 步合并(user/配额/渠道/attach/handoff/viewer/research 绑定/历史装配) | chat.py L793-932 + L1040-1195 |
| `sanitization.py` | 历史纯净性校验(见 §5.1,职责收窄) | memory.py L828-846 契约 |
| `lifecycle.py` | finalize_turn/落库/计量/retrieval/viewer post-turn | chat.py L627-733, L1275-1333 |
| `executors/base.py` | ChatEvent 协议显式化 | loop.py L341-516 事件名 |
| `executors/agent.py` | `get_agent().run_stream` 包装 + progress_sink + disable_thinking | chat.py L1230-1246 |
| `executors/direct.py` | (Phase 2 新建,复用 llm 单例) | research/plugin.py L547 先例 |

### 必须改(现有文件)

| 文件 | 改动 |
|---|---|
| `apps/api/routers/chat.py` | 收敛为薄适配层:鉴权依赖 + 构造 ChatTurnRequest + 调 orchestrator + SSE 翻译;保留 import/research/配额对外端点原样 |
| `apps/api/deps.py` | 暴露 orchestrator 装配入口(重导出模式已存在) |

### 禁止改(边界冻结)

`packages/agent/engine/{kernel,loop,context,telemetry}.py`、`packages/agent/memory/**`、
`packages/agent/llm/llm_guard.py`、`apps/api/viewer_context.py`(纯函数,只被调用)、
`apps/api/agent_factory.py`(仅加装配项,不动内核构造)、
`packages/rag/**`(Phase 4 在 `pipeline/executor.py`+nodes 内部原地扩展)、
`plugins/research/**`、`core/infrastructure/{memory,db,visibility,request_context}.py`、
`apps/web` / `apps/desktop` 前端(SSE 契约零变化)。

## 7. 分阶段落点与验证锚点

| Phase | 交付 | 集成测试锚点 |
|---|---|---|
| 0 | Golden 集:闲聊/多轮/文档问答/RAG 成败/联网/Action/断连/research;固化 `chat.stream-first-token` 等现有日志锚(chat.py L1058/L1351) | 基准报告 |
| 1 | orchestrator+context+execution_plan+lifecycle 上线,**策略恒 = AgentExecutor**;SSE 帧字节级兼容对比 | 现有 chat/research/viewer 测试全绿 + done 帧 schema 对比 |
| 2 | understanding(L0+cheap signals+optional L1)+ sanitization + direct executor;`direct_fast_path_enabled` 开关 | TTFT 显著下降;400 校验回归;prompt cache 命中 |
| 3 | viewer executor(复用 build_viewer_blocks,Open != Inject 不变) | viewer citation 测试 |
| 4 | staged RAG(executor.py 内原地:并发召回+充分性门禁,Golden Set 校准)+ Fail-Closed 测试 | `RetrievalUnavailable` 不外溢 web |
| 5 | action + composite(仅独立并行输入) | 参数校验/鉴权二次门禁测试 |
| 6 | L1 阈值调优、Agent 兜底收敛、老路径 feature flag 归档 | 全量回归 |

每阶段结束交付:改动文件清单、行为差异、残余风险;全局 feature flag 保持旧 Agent 入口随时可回退。
