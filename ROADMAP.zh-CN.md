# Roadmap

[English](ROADMAP.md) | 简体中文

WittyCode 接下来打算做的事，大致按优先级排列。不写日期：做好了才算，顺序也可能调整。做完的事从这里移到发布说明。

## 现在

- **独立化收尾**：WittyCode 跑在自己的代理循环上（ChatGPT Plan、API Provider），Devin 是可选引擎，不依赖任何别的代理程序。还剩：不再读别的应用的技能设置（`disabledSkills`、插件），只把它们的技能目录作为只读兼容；清掉注释和文档里最后的旧名字；重拍 README 截图。
- **首个 alpha 版本**（`v0.1.0-alpha.1`）：由 GitHub Actions 构建发布，附 zip、SHA-256 和构建来源证明。暂不签名。
- **保持 CI 全绿**：每次推送和 PR 跑单元测试和页面检查；整机测试和变异测试留在本地。

## 下一步

- **代理循环的沙箱**：现在权限靠路径解析和命令文本判断，没有系统级隔离。计划让命令在隔离环境里运行，复用现成的开源沙箱而不是自己写。候选（优先 MIT 许可，采用前先审许可证）：[microsoft/quicksand](https://github.com/microsoft/quicksand)、[vndee/llm-sandbox](https://github.com/vndee/llm-sandbox)。
- **无人值守时更安全的默认值**：`/goal` 和长循环的默认更保守，「完全访问」前的提醒更明确。
- **代码签名**：项目满足条件后，申请面向开源项目的免费代码签名（例如 SignPath Foundation）；在那之前发布包不签名。
- **自动更新**：检查新版本并就地更新，按发布的校验和核对。
- **包管理器**：winget 和 Scoop 清单。
- **英文界面**，之后再加其他界面语言。

## 以后

- **子代理**：页面已经能显示子代理，但这个版本还没有引擎会启动它。
- **Bedrock 真机**：在真实账号上跑通一次完整请求（请求签名已与 botocore 核对过）。
- **代理循环审阅的后续项**（`docs/` 里循环审阅的 L2–L12）。
- **更多引擎支持运行中提问**（引擎本身支持时）。
- **技能和插件的安全检查**：在它们的指令交给模型之前。

## 暂不计划

- macOS 和 Linux：应用、测试和发布包目前都只针对 Windows。

想法和需求欢迎在 [Discussions](https://github.com/WittyRyuji/WittyCode/discussions) 提，bug 请提到 [Issues](https://github.com/WittyRyuji/WittyCode/issues)。
