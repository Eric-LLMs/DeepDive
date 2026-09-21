# Delveta 使用手册 · 大模型 Key、路由与 Admin 配置

本章面向管理员。入口:浏览器访问 **/admin**(初始管理员账号密码见项目 README「Configure Model Access」/ getting-started,首次登录后请立即修改);桌面端也可经左下角账号菜单 → **Admin** → **Admin Console** 单点登录进入。

## 概念链

Credential(渠道,含 API key)→ Model(模型目录)→ Route(渠道×模型的加权路由)→ Channel(role↔credential 绑定)→ Role(角色配额)。

## 1. 配置大模型 Key(Credentials)

- **Providers** 模块 → **Credentials**:新增一个 OpenAI 兼容渠道,填 **name**、**base_url**、**api_key**,并设为 **is_active**。
- 支持任意 OpenAI 协议兼容上游(DashScope、DeepSeek、OpenAI、自建网关等)。

## 2. 登记模型(Model Catalog)

- **Model Catalog**:添加模型名(provider model name)、每 1k token 的输入/输出单价(计费与成本审计用)。

## 3. 创建 route(Routing & Weights)

- **Routing & Weights**:选择 credential + model 建立一条 route,并配置:
  - **priority**:主备顺序,数值高者优先;
  - **weight**:同优先级内按权重分流(负载/灰度)。
- 运行时按「角色绑定渠道 → 路由优先级/权重 → 模型」漏斗解析出实际渠道;请求结束按路由表计价。

## 4. 角色与用户路由(Roles / Tokens)

- **Roles**:每个角色(user_roles)设 daily/monthly requests、tokens、RPM、cost 配额、**default model**、可用模型列表,以及 **role ↔ credential 渠道绑定**(即"给用户/角色开 route"的地方)。内置角色:regular / pro / vip / admin / anonymous。
- **Tokens**:按用户发放/回收 LLM key 授权矩阵(掩码 `sk-***`、复制、revoke/restore),并查看 login-sessions(登录令牌)。
- 用户自带 key:登录 token 可 pin 到某个 credential 走自己的渠道计费。

## 5. 验证与其他

- **Chat Test**:Admin 内置冒烟测试,选模型直接对话验证渠道可用。
- 额度耗尽:接口返回 **402**,聊天界面提示充值/升级。
- RAG 流水线参数(chunking 策略、召回 top_k、rerank 等)在 Admin 的 **RAG** 模块调整,保存即热生效。
