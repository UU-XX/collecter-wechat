# 安装 Collecter

从 [Releases](https://github.com/UU-XX/collecter-wechat/releases) 下载 Skill、Python 或 CLI 中需要的一种。

## 方式一：Skill

需要支持本地脚本执行的 AI 助手、Windows 和 Python。Skill 包包含指令、参考资料和业务脚本，无须另外安装 CollecterCLI。

安装流程：

1. 从正式 Release 获取完整的 Skill 包。
2. 按 AI 宿主的说明导入整个 `collecter` 文件夹，保留包内脚本和参考资料。
3. 使用 Windows 与包内声明的 Python 运行环境。Skill 包自包含业务脚本，不表示所有宿主都自带 Python。
4. 启用 Skill，说明目标会话、时间范围、导出位置以及是否需要分析。

示例：

> 使用 Collecter 导出我指定群聊最近一个月的记录，保存到我选择的本地文件夹，完成后简要总结主要话题和明确结论。

完整入口、业务模块和读取组件已随 Skill 提供，详见[Skill 入口与流程](../skills/collecter/SKILL.md)。
解压后在 Skill 根目录安装依赖，再由宿主使用 `scripts/collecter.py`：

```powershell
py -3.12 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
& '.\.venv\Scripts\python.exe' -B scripts/collecter.py --help
```

## 方式二：Python 源码

需要 Windows 与 64 位 Python 3.12。

源码包目录结构：

```text
python/
  local-collector/
    cli.py
    启动CLI.cmd
    其他命令行与业务模块
  reviewed-tools/
    group_probe.py
    wcdb_key_tool_windows.79f1b5b92e12.py
```

两个目录都需要保留，不能只复制 `cli.py`。

在解压后的 `python` 目录打开 PowerShell，创建独立环境并安装 CLI 运行所需的加密、压缩依赖：

```powershell
py -3.12 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
& '.\.venv\Scripts\python.exe' -B '.\local-collector\cli.py' --help
& '.\.venv\Scripts\python.exe' -B '.\local-collector\cli.py'
```

准备好依赖后，也可运行 `启动CLI.cmd`。目前不提供 `pip install collecter` 安装方式。

## 方式三：CLI 便携程序

需要 Windows x64，无须另装 Python。

1. 从正式 Release 下载名称以 `Collecter-CLI-Windows-x64-` 开头的完整 ZIP。
2. 解压到自己选择的目录。
3. 保留 `CollecterCLI.exe`、`_internal` 及附带说明和许可文件。
4. 打开 `CollecterCLI.exe`，按菜单操作。

PowerShell 中可先查看帮助：

```powershell
& '.\CollecterCLI.exe' --help
```

程序只提供终端界面。请完整保留解压后的文件。

## 更新与原有数据

新版解压到新目录即可，保留原有结果。

Python / CLI 的配置与缓存位于 `%LOCALAPPDATA%\WechatLocalCollector`，数据库密钥和模型配置与当前 Windows 用户绑定。结果文件保存到操作时选择的目录。
