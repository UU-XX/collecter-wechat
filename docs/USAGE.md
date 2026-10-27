# 使用 Collecter

## Skill 使用

向 AI 助手说明以下信息即可：目标会话、日期范围、保存位置，以及是否需要分析。

例如：

> 先列出本机可用会话，让我选一个。导出选定日期范围的记录，完成后只总结主要话题和明确结论。

Skill 负责组织完整流程，读取与导出由随包脚本完成，不要求安装 CLI 程序。脚本入口为 `scripts/collecter.py`，详见[Skill 工作流](../skills/collecter/references/workflow.md)。

## Python / CLI 使用

以下命令在 `CollecterCLI.exe` 所在目录运行。Python 方式将 `& '.\CollecterCLI.exe'` 替换为对应的 `python -B cli.py` 入口，后面的参数相同。

路径、账号和会话编号都是示例，请换成自己的实际值。不要直接照抄示例账号目录。

### 1. 选择账号与会话

```powershell
& '.\CollecterCLI.exe' accounts
& '.\CollecterCLI.exe' conversations --account 'D:\微信数据\账号\db_storage'
```

账号列表中的本地目录不代表账号已登录。程序会显示可用的会话短编号，导出时按编号选择，避免重名混淆。

首次没有可用数据库密钥缓存时，读取可能失败。需要本次获取密钥时，在交互菜单明确允许，或在需要读取的命令上显式添加 `--allow-memory`；不要默认加到所有命令。

### 2. 导出记录

```powershell
& '.\CollecterCLI.exe' export --account 'D:\微信数据\账号\db_storage' --conversation '实际会话短编号' --start '2025-01-01' --end '2025-01-31' --output 'D:\Collecter导出'
```

纯日期按北京时间解释，结束日期包含当天。可重复指定 `--conversation` 选择多个会话；需要本机视频和附件时，分别添加 `--videos`、`--files`。

每次生成独立结果目录，主要文件包括：

| 文件 | 用途 |
| --- | --- |
| `消息记录.txt` | 按时间阅读 |
| `messages.jsonl` | 逐条结构化消息 |
| `聊天记录.md` | Markdown 阅读和整理 |
| `messages.csv` | 表格处理 |
| `records.sqlite3` | 本次任务记录数据库 |
| `聊天记录.html` | 离线浏览 |
| `采集说明.json` | 数量、覆盖范围、解析问题和完成状态 |

启用媒体后，结果中还可能包含 `videos/`、`files/` 及媒体关联状态。没有命中的媒体不会伪装成导出成功。

### 3. 浏览已有导出

```powershell
& '.\CollecterCLI.exe' browse --input 'D:\Collecter导出\实际结果目录' --search '会议' --page 1 --page-size 20
```

可按发送人、类型和时间筛选。默认终端中每条显示最多 2,000 字符；使用 `--full` 查看完整正文。这只影响浏览显示，不截断导出记录或模型分析输入。

### 4. 生成本地统计报告

```powershell
& '.\CollecterCLI.exe' report --input 'D:\Collecter导出\实际结果目录' --output 'D:\Collecter报告'
```

只统计已有导出，不重新读取微信，不发送聊天到模型。

### 5. 可选的模型总结

先在菜单 4 配置厂商、接口与模型，密钥通过不回显的输入保存。也可运行：

```powershell
& '.\CollecterCLI.exe' model configure
```

明确允许将指定导出的正文、发送人和时间发到配置接口后，再执行：

```powershell
& '.\CollecterCLI.exe' report --input 'D:\Collecter导出\实际结果目录' --output 'D:\Collecter报告' --with-model
```

较大任务还需要确认完整处理计划；参数模式追加 `--allow-large-summary`。该参数确认请求规模，不是解锁更多聊天内容。

分析按北京时间逐日进行，繁忙日期拆段后汇总，最终生成总体总结。只整理主要话题、重要观点和明确结论，合并重复内容，不单列待办或行动建议。

输出包括 `每日总结/日期.md`、完成后的 `总体总结.md`、`分析报告.html`、`统计数据.json` 和 `模型诊断.jsonl`。

总结采用非流式。兼容 Chat、Responses、Gemini 与 Ollama 默认不发送输出预算字段；原生 Anthropic 要求必填预算，未指定时保留 8,192 的兼容值。只有需要主动设置预算时再使用 `--max-output-tokens`；省略不代表服务端无限输出。

### 6. 取消、失败与重试

读取时按 `Ctrl+C` 请求安全停止，等待当前操作结束。导出失败后可在结果页选择手动重试，新结果仍写入新目录。

导出任务的增量和续导与模型分析是两回事。模型分析失败后会保留完整日期总结，但目前重新生成报告不会自动复用旧片段；重跑可能再次发送记录并产生费用。
