# Delveta 使用手册 · 云盘 Cloud Drive

## 打开云盘

- 桌面端:左侧 **Files** 标签,边栏顶部的来源下拉从 **💻 Local** 切到 **☁️ Cloud**,即显示云盘目录树。
- Web 端:顶部标签 **☁️ Cloud Drive**。

## 目录结构

- **My Drive**:个人根目录;**＋ New workspace** 创建共享工作区,**⚙ Manage** 管理成员角色(owner / admin / editor / viewer);**🗑 Trash** 回收站(可恢复)。
- 视图可切换 **list / grid**。

## 上传与整理

- **⬆ Upload**:文件按 8MB 分块上传,SHA-256 校验命中即"秒传"(不重复存储);超过 256MB 的大文件走桌面端本地处理后仅上传结果。
- **＋ New folder** 新建文件夹;右键菜单:**📄 New text file / 📁 New folder / 📤 Upload / 🗑 Delete**。
- **✏ Edit** 进入批量模式:勾选后 Download / Open / Share / Rename / Move / Copy / Trash。
- **🔗 Share**:分享给指定用户(读或写)或生成 public link(公开链接资产对所有登录用户与游客可见)。
- **⬇ Download** 下载。

## 文档入库(RAG)

- 文件徽章显示解析状态:**Pending → Parsing → Chunking → Embedding → Indexed**。Indexed 之后即可在对话中被 rag_search 检索。
- 文件行上的 **＋ Import to Knowledge** 手动触发入库;已入库显示灰色 **✓ In Knowledge**。
- 旧版 .doc、PDF、电子书、视频字幕等由 worker 流水线自动抽取(含内嵌图片恢复);文本笔记(📄 New text file)直接入库。

## 与 Chat 的联动

- 打开云盘文件即进入 Files 查看器,Chat 的 👁 FOCUS 徽章随之生效(引用当前页/当前时间窗)。
- **📎** 附件可选择云盘文件随消息发送(引用不复制,删除消息不影响原文件)。
