# Delveta 使用手册 · 学习平台与知识库(Web 控制台)

## 词汇导入(Import Data)

1. Web 控制台 → **Domain Management** 创建/选择领域(domain),领域分公共(public)与私有(自己的),公共领域所有人可检索。
2. **Import Vocabulary**:上传 **CSV / Excel / TXT** 或直接粘贴文本;每行英文句子(可带中文释义)。
3. 导入后自动建立两层语料:SQL 句子库(精确管理)与向量索引(语义检索)。

## Study Mode 学习

- 句子卡 **📖 View** 进入学习视图:**TTS 朗读**、**AI 释义**(语法/用词讲解)、**跟读对比**(录音与标准音比对)、图片 **Regenerate**、**Next / Prev** 翻页;可保存最佳语境(save best context)。
- **Manage Vocabulary**:治理面板 —— 编辑、去重、移库、调整领域归属。

## Articles 与 Query Repository

- **Articles**:长文管理,可 **import to RAG** 进入知识库检索。
- **Query Repository**:聊天里勾选的问答对(👍 或 Import to Knowledge)与云文档切片都汇入统一 chunks 库,由 rag_search 检索;云盘文件的入库入口见「云盘」手册(＋ Import to Knowledge)。

## 数据隔离

- 私有领域/句子只有自己可见;公共领域对全体用户开放,游客只见公共内容。
- 云盘资产经 owner / workspace 成员 / ACL(含 public link)三种通道授权。
