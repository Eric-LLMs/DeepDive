# DeepDive 使用手册 · 产品总览

DeepDive 是一体化 AI 知识工作台:对话(Chat)、深度研究(Research)、云盘(Cloud Drive)、文档/视频阅读与学习平台集成在同一个产品里。

## 三个使用界面

- **桌面工作台(Electron Desktop)**:主力界面。左侧边栏三个标签 —— Files(文件/云盘)、Chats(对话)、Research(研究任务)。中栏是文档查看器/任务视图,右/下侧是 Chat 面板。
- **Web 控制台(Web console)**:学习平台(Learning Platform),词汇导入、Study Mode 学习、Articles 与 Query Repository 管理。
- **Admin 控制台(/admin)**:管理员配置入口 —— LLM 渠道(Credentials)、模型目录、路由(Routing & Weights)、角色(Roles)、Token 发放。桌面端也可从左下角账号菜单 → Admin → Admin Console 免登录进入。

## 登录与账号

- 登录对话框支持注册(register)与忘记密码(forgot password):重置链接经邮件发送;服务器未配置 SMTP 时,链接会直接返回在页面上。
- 普通用户登录后在 My Account / 桌面账号菜单里管理个人资料与密码(见「账号与个人资料」手册)。
- 额度:免费/付费角色各有每日请求与 token 限额,耗尽时接口返回 402 或按匿名额度降级并在聊天内提示。

## 核心概念

- **云盘资产(Asset)**:上传到 Cloud Drive 的文件,经解析后进入 RAG 知识库(状态徽章 Pending → Parsing → Chunking → Embedding → Indexed)。
- **对话会话(Session)**:一次连续聊天。普通会话显示在 Chats 标签;每个 Research 任务绑定一个专属会话(不在 Chats 列表显示)。
- **知识库(Query Repository)**:文档、句子、聊天问答统一落在 chunks 表并可被 rag_search 检索。
