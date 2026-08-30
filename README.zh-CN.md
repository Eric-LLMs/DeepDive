# <img src="docs/images/deepdive-logo.png" alt="DeepDive" width="40" valign="bottom" /> DeepDive

[English](README.md) · [中文](README.zh-CN.md)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB)](https://react.dev/)
[![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**DeepDive 是一个生产级、支持多租户的 AI 学习平台**——它是一个具备持久记忆的 AI 导师，能够吸收你的学习资料、记住你的认知习惯，并在你的个人知识库中协助你进行深度理解、研究与创作。

深入探索：**[你能做什么](#你能做什么)** 演示完整产品体验，**[工程亮点](#-工程亮点)** 拆解系统核心机制，完整设计文档请参考 [docs/architecture.md](docs/architecture.md)。

---

## 什么是 DeepDive？

DeepDive 围绕一个核心理念构建：**学习应当是与你的材料持续深度的对话，而不是一次次孤立的聊天。**

### 为什么它与众不同：

* **围绕材料边学边问，而非脱离上下文**：在阅读或观看音视频时选中并讨论段落，获取有据可依的拆解讲解。
* **材料是探索的起点，而非认知的边界**：当现有资料不足时，AI 导师能够自主检索网络与公开社区（Reddit、X、知乎），获取最新讨论与外部佐证，拓展认知深度。
* **学习成果沉淀为持久知识**：核心认知沉淀为持久记忆，讨论过程自动转化为摘要、思维导图与演示幻灯片，回流至可检索工作区，支持跨设备随时无缝续学。

> **学习闭环**  
> `阅读/观看 → 提问与讨论 → 深度理解 → 检索拓展 → 沉淀记忆 → 产出创作 → 随时续学`  
>  
> **数据飞轮**  
> `导入材料 → 智能索引 → 精准召回 → 深度推理转化 → 生成知识产物 → 重新被索引检索`

---

## 你能做什么

| 能力模块 | 具体应用场景 |
| :--- | :--- |
| **学习与理解** | • 边读边看边提问 —— 支持 PDF、Office 文档、视频、音频、图片等多格式直读<br>• 获得分步推理讲解与核心概念拆解<br>• 就某个具体时刻展开探讨 —— 选中一段文字、一页或一个视频片段作为上下文 |
| **研究与探索** | • 单次查询即可跨越个人文件、笔记、历史会话与导入资料库进行全域检索<br>• 超越手头材料 —— 检索网络与公开社区（Reddit、X、知乎），获取更新的研究与佐证<br>• 将多源材料综合提炼为有据可查的结构化答案 |
| **持久记忆** | • 沉淀关键学习认知并在后续跨会话中精准召回<br>• 长期记忆与临时会话历史彻底解耦<br>• 随时回溯重点书签、批注笔记与记录位置 |
| **内容创作** | • 自动提炼会话、笔记与长篇文档的高质量摘要<br>• 一键将材料提炼为结构化思维导图与演示幻灯片<br>• 将探讨内容转化为可复用的知识资产并自动反哺检索库 |
| **协同研讨** | • 在团队工作区中安全共享学习资料与沉淀知识<br>• 基于清晰的角色与细粒度权限协同研读同一份材料 |

### Demo 交互流程

> **Agent 导师工作流：** 打开论文/视频 → 边学边问 → 精准检索相关段落 → 必要时联网搜索 → 深入探讨与澄清 → 沉淀核心认知 → 生成会话摘要 → 未来跨会话召回。*(完整演示视频即将上线)*

---

## 🔧 工程亮点

DeepDive 自研了高可控的 Agent 运行时，拒绝将核心编排委托给僵化的第三方框架。以下是系统的核心架构决策及生产级实现：

* **Agent 编排显式且完全可控**：`ReactLoopAgent` 单步循环通过支持热重载、依赖注入的技能目录（Skill Catalog）与插件运行时统一调度；权限沙箱按 `READ` / `WRITE` / `NETWORK` 维度对每次工具调用实施严格的拦截把关。
* **持久记忆与会话历史彻底解耦**：两条独立轨道共享统一的 Prompt 上下文边界——Agent 主动写入长期文件记忆，系统在 PostgreSQL 中自动维护情景会话记忆（通过 RRF 算法融合 `tsvector` 全文检索与 `pgvector` 向量检索，并引入时间衰减权重）。分层历史压缩（扁平 Token 窗口）在保证核心上下文不丢失的同时，将上下文严格限制在安全窗口内。支持相关历史会话的主动检索召回，且用户偏好指令采用原地覆盖更新而非物理删除。
* **可靠性原生内嵌，拒绝事后补丁**：针对上游 LLM 的瞬态错误，内置硬超时与指数退避重试机制，并通过单轮成本预算设置硬顶限额。工具安全由多重防线共同保障：基于 Redis Pub/Sub 的人工审批门（HITL，超时默认拒绝）、Plan 规划模式、有界子代理、用于状态回滚的 Shadow-Git 检查点，以及具备网络隔离和资源配额的 Docker 沙箱。可观测性原生内置：trace 上下文（`trace_id` / `turn_id` / `user_id` / `session_id`）贯穿每轮 Agent 执行，每轮产出指标 span（步骤数 / 工具延迟 / 错误 / 成本），并以 JSONL 审计日志落盘。
* **异步任务基座**：内容富化、定时 Agent 轮次与 toolkit 五阶段生成管道（*validate → ingest → generate → render → persist*，产出摘要、思维导图、演示文稿）由 arq worker 承载，以 `jobs` 表为唯一真相源，客户端轮询进度。摄取竞争通过 per-asset 锁串行化（上传自动入队、手动导入与 admin 重索引不会互相删除彼此的父节点块），先整删再分批重嵌入使 worker 超时只丢失未提交的尾部且重跑幂等；任务状态如实记录（取消与非终局失败均如实落库），终局失败写入 JSONL dead-letter。
* **检索流程高度可配置，无需硬编码**：基于节点编排的模块化 RAG 管道（*query rewrite → vector + keyword recall → RRF fusion → cross-encoder rerank → parent expansion → CRAG relevance checks*）支持在管理后台实时调整拓扑、重排顺序或启闭节点，无需重启服务。文本切分可在同一 RAG 模块中配置 —— 支持多种切分策略（可配置窗口大小与重叠度的固定滑动窗口、段落、句子），并提供 `contextual`（LLM 为每个切片生成上下文前缀）、`parent_child`（由小到大分层检索：索引叶子节点并关联父节点窗口，召回命中叶子后自动展开父节点全文）以及 `cjk`（jieba 中文分词索引）等配置开关。支持实时分块效果预览与一键重建索引生效。同时提供 Golden-set 黄金测试集评测（`Recall@k`、`Precision@k`、`MRR`）、基于版本感知的 Redis 查询缓存（按 query + config + corpus version 联合生成 Key，重新索引后自动失效）以及基于视觉大模型的 PDF 表格解析。单节点故障时自动降级至可用通道，保障对话不中断，同时用户反馈会被实时记录并沉淀至评测数据集。全会话聊天支持增量导入：LLM 将对话按问题分段为 Q&A 块，per-message 已导入标记使重复导入零开销，源内容变更重导入时仅替换其对应块；管道每个节点记录独立的 trace（状态 / 耗时 / 输出），可在 admin Test 面板逐段查看；租户绑定的 gRPC 检索服务在入口强制租户作用域（token 鉴权 + 令牌桶限流 + 显式 guest 标记），杜绝无作用域调用跨租户全量读取。检索统一覆盖网盘文件、学习卡片与对话历史等多源语料，既可进程内直连，亦支持通过该 gRPC 服务独立部署。
* **Prompt 与工具原生适配前缀缓存**：字节级稳定的 Prompt 头部（系统身份设定 + 单行工具索引目录）配合每轮动态尾部，最大化复用 LLM 前缀缓存（Prefix Caching）以大幅降低延迟与 Token 成本，并具备可度量的缓存标识。工具层采用延迟加载：优先挂载轻量 Stub 存根，仅在真正调用时才按需拉取完整 Schema。
* **统一知识底座，告别孤立存储**：个人网盘与共享工作区（具备 Owner / Admin / Editor / Viewer 角色权限、成员管理与追加式审计日志）基于 SHA-256 内容寻址对象存储构建，支持引用计数去重、8 MB 断点分块续传、多级目录、30 天回收站留存、文件级 ACL、组内分享与多格式在线预览。网盘同时作为内容处理、检索与 Agent 工作流的共享工作目录。
* **权限控制在资源边界处强制执行**：角色权限、租户边界、上游 LLM 凭证、模型目录与路由权重均由统一管理后台集中管控。提供带掩码（`sk-***`）的每用户密钥授权矩阵、用于邮件验证 / 密码重置的 SMTP 服务，以及无状态签名 Admin 会话机制。登录时按角色动态绑定 LLM 通道并支持自动故障转移（Failover），无可用密钥时平滑降级至访客配额，避免异常掉线。用量按免费额度优先计费，超额溢出至钱包扣款；扣款为原子操作（`UPDATE ... WHERE balance >= cost` 防超支），记录 `balance_after` 快照并支持幂等，余额不足返回 HTTP 402。
* **多租户数据隔离，检索不失边界**：请求身份经 ContextVar 传递——RAG 与记忆召回器是进程级单例，无法构造注入用户，`/chat` 端点写入 ContextVar、召回器兜底读取。同一条可见性谓词写成两份（SQLAlchemy 表达式 + 原生 SQL 片段），确保走 tsvector/pgvector 原生 SQL 的召回与 ORM 查询遵守完全一致的三通道（本人拥有 / 工作区成员 / 文件级 ACL 含公开链接）；chunk 级谓词直接基于 `chunks.user_id` 判定，使无 asset_id 的学习 / 对话 chunk 不会越出所有者边界。词汇语料采用部分唯一索引：公共行全局唯一、私有行按用户唯一，不同用户可各自拥有同名词条而不冲突。
* **Local-First 客户端配合私有化部署**：Electron 工作台支持离线文件工作流（文件树浏览、多格式查看器、视频逐帧截图）；大体积媒体在客户端本地预处理并回传分析产物，常规计算任务由服务端承载。视频可一键生成 PPT/PDF 学习册：基于字幕时间戳抽取关键帧，每页一帧加对应字幕文本，并内置 CJK 字体保证中文渲染不乱码；TTS 支持中英文声线自动切换、按句流式合成（首句秒回），并通过内容哈希缓存波形实现重放零延迟。完整后端技术栈（PostgreSQL/pgvector、Redis、TEI 向量推理、Kokoro TTS 与 LiteLLM 网关）支持通过 `docker-compose` 一键拉起，确保所有数据完全留存在你自己的基础设施之内。

> 完整设计规范请参考：[`docs/architecture.md`](docs/architecture.md)。

---

## ✅ 实现状态

| 领域模块 | 实现状态 |
| :--- | :---: |
| **Agent Runtime** | ✅ 已实现 |
| **双轨记忆 (Dual-track Memory)** | ✅ 已实现 |
| **可配置检索 (Configurable Retrieval)** | ✅ 已实现 |
| **异步任务系统 (Async Job System)** | ✅ 已实现 |
| **Cloud Drive 与工作区** | ✅ 已实现 |
| **认证 / RBAC / ACL** | ✅ 已实现 |
| **用量与模型路由** | ✅ 已实现 |
| **自托管 AI 服务 (Self-hosted AI Services)** | ✅ 已支持 |

> 完整功能矩阵与规划能力见 [`docs/architecture.md § Implementation Status`](docs/architecture.md#implementation-status)。

---

## 🏗️ 架构一览

![平台架构 —— 租户与工作区、访问层、核心应用（agent 运行时 · 双轨记忆 · 可配置 RAG · 云工作区 · 处理）、自托管数据与 AI 服务](./docs/images/deepdive-architecture-platform-diagram.png)

* **各模块架构与流程图**（Agent 内核 · 记忆 · Prompt · RAG）：请参阅 [`docs/architecture-diagrams.md`](docs/architecture-diagrams.md)。
* **技术选型考量**：请参阅 [`docs/architecture.md §2 Tech Stack`](docs/architecture.md#2-tech-stack)。
* **Monorepo 仓库结构**：请参阅 [`docs/architecture.md §3`](docs/architecture.md#3-repository-structure-monorepo)。

---

## 📚 项目文档

* **[`docs/architecture.md`](docs/architecture.md)** —— 完整系统架构设计（单一真实数据源 SSOT）：涵盖技术栈、代码目录、Agent 内核、工具运行时、数据模型、部署架构及已实现 vs 已设计矩阵。
* **[`docs/architecture-diagrams.md`](docs/architecture-diagrams.md)** —— 模块架构图与 Mermaid 原图源码。
* **[`docs/getting-started.md`](docs/getting-started.md)** —— 手动部署指南、一键启动脚本与各端（桌面端/Web/管理后台）走查。
* **[`docs/configuration.md`](docs/configuration.md)** —— 环境变量全量参考手册。
* **[`docs/features.md`](docs/features.md)** —— 全功能详解（桌面工作台、聊天助手、RAG 与查询知识库、学习模式、云盘、权限与计费）。

---

## 🚀 快速开始

### 方案 A —— 一键启动（推荐）

| 运行环境 | 执行脚本 |
| :--- | :--- |
| **Windows 桌面端** | `bash scripts/start_desktop.sh` |
| **Linux 服务器** | `bash scripts/start_server.sh` |

*脚本会自动检测并安装 Docker、拉起数据与模型服务容器、初始化 Python 环境、注入默认管理员账号（`admin / admin`），并自动启动桌面工作台或 Web 界面。*

### 方案 B —— 本地手动开发模式

```bash
git clone https://github.com/Eric-LLMs/DeepDive.git
cd DeepDive

# 创建并激活环境
conda create -n deepdive python=3.11 -y && conda activate deepdive
cp .env.example .env            # 填入你的 LLM_UPSTREAM_KEY

# 安装依赖
pip install -e ".[dev]"         # 如需语义检索支持: pip install -e ".[rag]"

# 启动依赖服务容器
docker compose up -d postgres redis embedding tts llm-gateway worker

# 初始化数据库并运行 API 服务
python scripts/init_db.py
uvicorn apps.api.main:app --reload     # 访问接口文档: http://localhost:8300/docs
```

### 方案 C —— 自托管 LLM

LiteLLM 网关把虚拟模型 `deepdive-chat` 路由到任意 OpenAI 兼容上游（`LLM_UPSTREAM_BASE`）。把它指向自托管服务器（vLLM / Ollama / …）即可在你自己的硬件上运行整套 AI 技术栈，或指向外部供应商 —— 无需改动任何代码。

完整手动步骤、环境变量与桌面 / 网页 / 管理后台走查：[docs/getting-started.md](docs/getting-started.md) · [docs/configuration.md](docs/configuration.md)。

---

## 📝 许可证

本项目基于 [MIT License](LICENSE) 开源。
