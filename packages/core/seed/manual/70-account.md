# DeepDive 使用手册 · 账号、个人资料与密码

## 注册与登录

- 登录对话框内可 **register** 注册新用户;已注册用户以 username 或 email + 密码登录。
- **Forgot password**:提交邮箱后发送一次性重置链接;服务器未配置 SMTP 时,重置链接会直接显示在页面上供复制。

## 修改密码

- Web 端:进入 **My Account**,在 profile 区修改密码(输入旧密码确认)。
- 桌面端:左下角 **账号菜单** 进入个人资料页,同样支持修改密码。
- 修改后既有登录 token 的处理以界面提示为准;Admin 的 Tokens 模块可查看/管理各用户 login-sessions。

## 修改个人信息

- **My Account**(Web)或桌面账号菜单:编辑 **display name、username、email、phone、avatar**。头像上传后本地存储并经 `/api` 静态目录发布。
- 同一页可查看:钱包余额(wallet balance)、当日用量(daily usage)、用量明细(usage logs)、交易记录(transactions)。

## 桌面端 Settings(⚙️)

桌面 **Settings** 仅客户端偏好:主题(theme)、字号(font size)、窗口与显示(window & display)、检查更新(updates)、帮助/关于(help/about)。账号、模型、配额等服务端配置都在 Web 控制台或 Admin 控制台。

## 匿名/游客

- 未登录也可以聊天:游客使用 anonymous 角色额度(每天限次),界面会提示登录以继续。游客身份经浏览器本地签名的 `gt_` token 持久化。
