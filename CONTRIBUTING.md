# 问题反馈与贡献

Collecter 当前聚焦 Windows 本地微信记录的手动读取、导出、浏览和简洁分析。公开程序形态仅为 CLI，Skill 与 Python 方式使用同一套核心能力。

自有内容采用[个人非商业使用许可](LICENSE)，商业用途需另行书面授权。引用或贡献第三方内容时，请保留其原有来源和许可，参见[第三方组件说明](THIRD_PARTY_NOTICES.md)。

## 报告问题

建议提供使用方式、Collecter 版本、Windows 与微信版本、发生阶段、错误码，以及预期和实际行为。复现材料优先使用合成数据。

不要在公开 Issue 或附件中提交聊天正文、原始数据库、账号目录、密钥缓存、模型配置或 API Key。截图发布前检查联系人名称和本地路径。涉及敏感信息泄露的问题见[安全说明](SECURITY.md)。

## 提交改动

先阅读现有实现与相关说明，复用已有逻辑，保持改动小而明确。新增代码注释使用中文；不要修改原用户数据、上传真实导出或混入桌面客户端文件。

读取、解析和恢复流程的改动应提供合成样本和相关检查。依赖、第三方模块或打包范围改变时，同步说明来源、许可与分发影响。

源码位于 `python/local-collector`，Skill 入口位于 `skills/collecter/scripts`。在隔离的 `LOCALAPPDATA`、`APPDATA` 中运行以下合成检查，不依赖真实微信记录和模型配置：

```powershell
python -X utf8 -B -m unittest discover -s python/local-collector -p 'test_*.py' -v
python -X utf8 -B -m unittest discover -s tests -v
```

CLI 打包使用 `python -X utf8 tools/build_release.py --version 0.1.0`，构建前安装 `requirements-build.txt`。已有发行目录不会覆盖。可选的 Windows CI 模板位于 `tools/github-actions-test.yml`，需要时可自行启用。

功能建议请说明真实使用场景。月度年度汇总、模型分析断点续跑等未完成事项，不应在文档或发行说明中提前写成已支持。
