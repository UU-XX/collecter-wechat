# Collecter

本地微信聊天记录导出与分析工具。

选择会话和时间范围，将记录保存为可阅读、可检索的本地文件，也可以生成统计报告和 AI 总结。提供 Skill、Python 和 CLI 三种使用方式，按需选择。

> 允许个人非商业使用，包括日常导出、学习和测试。商业用途需另行书面授权。详见 [LICENSE](LICENSE)。

[安装说明](docs/INSTALL.md) · [使用指南](docs/USAGE.md) · [常见问题](docs/FAQ.md)

## 主要功能

- **聊天导出**：支持个人和群聊，按日期范围选择，支持批量会话。
- **多种格式**：TXT、JSONL、SQLite、Markdown、CSV、离线 HTML。
- **本地浏览**：按关键词、发送人、类型和日期筛选记录。
- **统计分析**：查看消息数量、活跃日期、成员发言和时间分布。
- **AI 总结**：逐日整理主要话题、重要观点和明确结论，再生成总体总结。
- **增量与续导**：复用已有导出记录，新任务保存到独立目录。
- **视频与附件**：可选导出本机已下载且能够关联的资源。

## 安装与使用

### CLI 程序

从 [Releases](https://github.com/UU-XX/collecter-wechat/releases) 下载 Windows x64 CLI 压缩包，完整解压后打开 `CollecterCLI.exe`。无须安装 Python。

```powershell
& '.\CollecterCLI.exe'
```

按菜单选择读取导出、浏览记录、生成报告或配置模型。保留同目录的 `_internal` 文件夹。

### Python

需要 Windows 和 64 位 Python 3.12。在源码包的 `python` 目录运行：

```powershell
py -3.12 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
& '.\.venv\Scripts\python.exe' -B '.\local-collector\cli.py'
```

保留相邻的 `local-collector` 和 `reviewed-tools` 目录，详细布局见[安装说明](docs/INSTALL.md)。

### Skill

将完整的 `collecter` Skill 文件夹安装到支持本地脚本执行的 AI 助手中，通过自然语言完成会话选择、导出和分析。Skill 随包携带必要脚本，无须另外安装 CollecterCLI。

例如：

> 导出我指定群聊最近一个月的记录，保存到我选择的文件夹，并简要总结主要话题和明确结论。

Skill 的完整脚本位于 [skills/collecter](skills/collecter/SKILL.md)，源码位于 `python/`。从 [Releases](https://github.com/UU-XX/collecter-wechat/releases) 下载所需的 Skill、Python 或 CLI 包。

## 使用示例

导出后，可以直接浏览记录或生成统计报告：

```powershell
& '.\CollecterCLI.exe' browse --input 'D:\聊天导出\实际结果目录' --search '关键词'
& '.\CollecterCLI.exe' report --input 'D:\聊天导出\实际结果目录' --output 'D:\聊天报告'
```

AI 总结为可选功能。Python / CLI 在菜单中配置模型后，本次明确选择启用内容分析才会发送所选记录。

## 支持范围

- 当前面向 Windows 微信 4.x 的特定数据库布局，版本兼容情况见[常见问题](docs/FAQ.md)。
- 程序分发仅提供 CLI，不包含桌面客户端。
- 只能读取本机已有记录；图片文件导出、语音识别和 OCR 暂不支持。
- 当前 AI 分析为每日 → 总体汇总，月度年度汇总和模型分析断点续跑尚未实现。

## 隐私

导出、浏览与普通统计在本机完成。AI 总结由所选宿主或配置的模型接口处理聊天内容；不使用 AI 也能完成普通导出和统计。详见[数据说明](docs/PRIVACY.md)。

## 反馈与贡献

欢迎反馈问题和提交改进。请说明使用版本、运行环境和错误码，避免在公开反馈中附上真实聊天或密钥。

[贡献说明](CONTRIBUTING.md) · [更新记录](CHANGELOG.md)

## 使用许可

本项目以“Collecter 个人非商业使用许可”公开自有源码。该许可包含非商业限制，不属于 OSI 定义的标准开源许可。

上述约定适用于自有 Skill、Python 代码和 CLI 中的自有部分；第三方组件保留原有许可，见[第三方组件说明](THIRD_PARTY_NOTICES.md)。
