# Delveta 使用手册 · 内容生成(Slides / Mindmap / Summary)

## 从会话或云盘文件生成

1. 在 Chat 输入框上方的 **Generate** 工具栏(或会话头部 **⋯** 菜单)点 **Mind Map / Slides / Summary** 任一工具。
2. 生成对话框:
   - **Source**:选择 **this conversation**(当前会话)或勾选 **Cloud Drive files**(支持多选);
   - **输出文件夹**:结果落到云盘指定目录;
   - 可选 **custom prompt** 与自定义文件名。
3. 点生成后任务转后台运行,按钮变为 ⏳;完成后产物以 `<名称>_<工具>` 命名写入云盘,并提供 **view output** 直达。

## Slides 对话框选项

- **Length**:Short(约 6 页)/ Default(约 8 页);
- **Format**:**Detailed Deck**(详实讲稿版)或 **Presenter Slides**(演讲者版);
- **Choose language**:输出语言;**Describe…**:自由文本补充要求;
- **Generate now** 立即排队 / **Generate later** 稍后执行。
- 产物:16:9 版式 `deck.pdf`,并附 `.md`、`.pptx`、`deck.json`。

## 查看器内直接生成

- 打开视频时可 **Generate PPT**(按字幕分段成片)或 **Generate Book**(整本转写书稿)。
- Research 任务 PUBLISH 后自动编译报告 PDF(`outputs/<任务名>_vN.pdf`),无需手动生成。

## 说明

- 大文档走 map-reduce 汇总,生成可能需要数分钟,可离开页面,完成后按钮自动恢复;
- 生成的 Slides/Summary 是普通云盘文件,可继续被打开、分享、二次提问。
