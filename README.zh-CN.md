# <img src="docs/images/deepdive-logo.png" alt="DeepDive" width="40" valign="bottom" /> DeepDive

[English](README.md) · [中文](README.zh-CN.md)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-20232A?logo=react&logoColor=61DAFB)](https://react.dev/)
[![PostgreSQL + pgvector](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**DeepDive** 是一个**生产级、多租户的 AI 学习平台**——一位持续学习你的材料、记住你的学习方式、并帮你理解、研究、创造的导师。

深入了解:[**你能做什么**](#你能做什么)带你浏览产品,[**工程亮点**](#工程亮点)讲解系统,[docs/architecture.md](docs/architecture.md) 记录了完整设计。

## 什么是 DeepDive?

DeepDive 是一个**多租户 AI 学习平台**——集持久导师、个人记忆与可检索知识库于一体的可自托管系统。

**它的不同之处:**

- **与你的材料一起学,而不是在一旁学** —— 边读边看时选中并讨论段落,得到有依据的讲解与分步拆解。
- **你的材料是起点,不是边界** —— 当手头来源不够时,导师会自动上网、检索学术来源来扩展你的理解。
- **学习洞见不会消失** —— 重要的洞见变成持久记忆,讨论则转成摘要、思维导图与幻灯片,直接回流进你可检索的工作区,让你能随时换设备接着学。

```text
Material → indexed → retrieved → transformed → artifact → searchable again
```

## 你能做什么

| 能力 | 能做什么 |
|---|---|
| **学习** | • 边读边看边提问 —— PDF、Office 文档、视频、音频、图片等<br>• 获得分步讲解与概念拆解<br>• 就选中的段落、页面或片段展开讨论 |
| **研究** | • 一次检索即可搜遍文件、笔记、对话与来源<br>• 材料不足时上网搜索<br>• 把多个来源综合成一个有依据的答案 |
| **记忆** | • 保存持久洞见并在后续会话中召回<br>• 让长期记忆与对话历史分离<br>• 回看书签、笔记与保存的位置 |
| **创造** | • 总结会话、笔记与文档<br>• 生成思维导图与幻灯片<br>• 把对话变成可复用知识,回流进检索 |
| **协作** | • 在工作区中共享文件与知识<br>• 以团队形式、按角色与权限共同研读同一份材料 |

## Demo

> **Agent 导师流程:** 打开论文或视频 → 边学边问 → 检索相关段落 → 需要时上网搜索 → 讨论与澄清 → 保存洞见 → 总结会话 → 之后召回。演示视频即将上线。

---

## 🔧 工程亮点

DeepDive 实现了可控的 agent 运行时,而非把编排交给某个框架。核心架构决策及其生产级实现:

- **Agent 编排显式且可控。** 一个 `ReactLoopAgent` 步骤循环通过可热加载的技能目录与插件运行时来编排模型调用与工具执行;类型化沙箱严格按 `READ` / `WRITE` / `NETWORK` 权限把关每一次工具执行。
- **持久记忆与会话历史解耦。** 两条独立轨道共享同一个 prompt 边界:agent 通过文件存储写入持久长期记忆,而系统在 PostgreSQL 中管理情景会话记忆(`tsvector` + `pgvector` 经 RRF 融合、带时间衰减加权)。分层历史压缩(扁平 token 窗口)在保持上下文有界的同时不丢失上下文。
- **可靠性是内建的,不是后补的。** 硬超时与指数退避重试吸收上游 LLM 的瞬时错误,并以单轮成本预算兜底。工具安全通过 Redis pub/sub 人工审批门(超时即拒绝)、plan 模式、有界子代理、用于状态回滚的 shadow-git 检查点,以及一个资源受限、网络隔离的 Docker 沙箱来保证。
- **检索是配置,不是代码。** 一条模块化节点管道 —— 向量 + 关键词召回、RRF 融合、cross-encoder 重排、父块扩展与 CRAG 相关性检查 —— 可以在管理后台实时重配置、重排或开关,无需重启服务。它包含分块预览、黄金集评测(`Recall@k`、`Precision@k`、`MRR`)、Redis 查询缓存(按 query + config + corpus version 作为 key,重新索引时自动失效),以及 PDF 表格的视觉 LLM 转录。单个节点失败会降级到存活通道而不会中断聊天,用户反馈则记录到黄金评测数据集。
- **Prompt 与工具天然缓存友好。** 一个字节稳定的 prompt 头部(系统身份 + 单行工具索引)配合每一步动态的尾部,最大化 LLM 前缀缓存以大幅降低延迟与 token 成本,并带有可度量的缓存身份。工具采用延迟加载:先挂载轻量 stub,只有在真正调用时才拉取完整参数 schema。
- **存储是统一的知识底座,不是孤立的筒仓。** 私有 Drive 与共享团队工作区(具备 `Owner` / `Admin` / `Editor` / `Viewer` 的 RBAC、成员管理与追加式活动日志)建在一个 SHA-256 内容寻址对象存储之上,带引用计数去重、8 MB 断点分块上传、多级目录、30 天回收站保留、文件级 ACL、公开分享链接与多格式预览。Drive 同时充当内容处理、检索与 agent 工作流的共享数据与工作目录。
- **授权在资源边界处执行。** 角色、租户边界、上游 LLM 供应商凭证、模型目录与路由权重统一在一个管理后台管理。它提供带掩码凭据(`sk-***`)的每用户 key 授权矩阵、用于邮件验证 / 密码重置的 SMTP,以及无状态签名 admin 会话。
- **本地优先的客户端与可自托管基础设施。** Electron 工作台支持离线文件工作流(文件树、多格式查看器、视频帧截取);大媒体在客户端处理,产物回传服务器,而大多数内容处理工作负载在服务端运行。完整后端栈 —— PostgreSQL/pgvector、Redis、TEI 嵌入、Kokoro TTS 与 LiteLLM 网关 —— 通过 `docker-compose` 无缝部署,让你的数据完全留在你自己的基础设施内。

完整设计:[docs/architecture.md](docs/architecture.md)。

## ✅ 实现状态

| 领域 | 状态 |
|---|---|
| Agent Runtime | ✅ 已实现 |
| 双轨记忆 | ✅ 已实现 |
| 可配置检索 | ✅ 已实现 |
| 异步任务系统 | ✅ 已实现 |
| Cloud Drive 与工作区 | ✅ 已实现 |
| 认证 / RBAC / ACL | ✅ 已实现 |
| 用量与模型路由 | ✅ 已实现 |
| 自托管 AI 服务 | ✅ 已支持 |

完整矩阵与规划能力见 [docs/architecture.md §Implementation Status](docs/architecture.md#implementation-status)。

---

## 🏗️ 架构一览

![平台架构 —— 租户与工作区、访问层、核心应用(agent 运行时 · 双轨记忆 · 可配置 RAG · 云工作区 · 处理)、自托管数据与 AI 服务](./docs/images/deepdive-architecture-platform-diagram.png)

---

## 🧩 架构与流程

各模块的架构与流程逻辑(agent 内核 · 记忆 · prompt · RAG):[docs/architecture-diagrams.md](docs/architecture-diagrams.md)。

---

## 🧰 技术栈

DeepDive 用了哪些技术以及为什么选它们 —— 见 [docs/architecture.md §2 Tech Stack](docs/architecture.md#2-tech-stack)。

## 📂 仓库结构

monorepo 如何布局、各模块负责什么 —— 见 [docs/architecture.md §3](docs/architecture.md#3-repository-structure-monorepo)。

## 📚 文档

- [docs/architecture.md](docs/architecture.md) —— 完整系统设计(唯一权威来源):技术栈、仓库布局、agent 内核内部、工具运行时、数据模型、部署,以及已实现 vs 已设计矩阵。
- [docs/architecture-diagrams.md](docs/architecture-diagrams.md) —— 各模块架构图与 mermaid 源码。
- [docs/getting-started.md](docs/getting-started.md) —— 完整手动搭建、一键启动脚本,以及桌面 / 网页 / 管理后台走查。
- [docs/configuration.md](docs/configuration.md) —— 环境变量参考。
- [docs/features.md](docs/features.md) —— 完整功能走查(桌面工作台、聊天助手、RAG 与查询仓库、学习模式、云盘、角色与计费)。

---

## 🚀 快速开始

### 方案 A —— 一键启动(推荐)

| 环境 | 脚本 |
|---|---|
| Windows 桌面 | `bash scripts/start_desktop.sh` |
| Linux 服务器 | `bash scripts/start_server.sh` |

每个脚本会在缺失时安装 Docker、启动数据 + 模型服务、准备好 Python 环境、播种默认 `admin` / `admin` 账号,并启动工作台(桌面)或 Web UI(服务器)。

### 方案 B —— 手动本地开发

```bash
git clone https://github.com/Eric-LLMs/DeepDive.git
cd DeepDive
conda create -n deepdive python=3.11 -y && conda activate deepdive
cp .env.example .env            # fill in LLM_UPSTREAM_KEY
pip install -e ".[dev]"         # + pip install -e ".[rag]" for semantic search
docker compose up -d postgres redis embedding tts llm-gateway worker
python scripts/init_db.py
uvicorn apps.api.main:app --reload     # http://localhost:8300/docs
```

### 方案 C —— 自托管 LLM

LiteLLM 网关把虚拟模型 `deepdive-chat` 路由到任意 OpenAI 兼容上游(`LLM_UPSTREAM_BASE`)。把它指向自托管服务器(vLLM / Ollama / …)即可在你自己的硬件上跑完整 AI 栈,或指向外部供应商 —— 无需改代码。

完整手动步骤、环境变量与桌面 / 网页 / 管理后台走查:[docs/getting-started.md](docs/getting-started.md) · [docs/configuration.md](docs/configuration.md)。

---

## 📝 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。
