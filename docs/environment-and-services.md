# 运行环境与使用的服务

## 环境

| 项目 | 本项目采用方式 |
|---|---|
| 本地系统 | Windows；自动运行使用任务计划程序 |
| Python | 开发环境验证过64位 Python 3.14.6；其他版本未完整验证 |
| PowerShell | 7.x；用于隐藏输入密钥及注册计划任务 |
| Python依赖 | 全部使用标准库，无 requirements.txt、无需pip安装 |
| SQLite | 使用 Python 自带 sqlite3，不单独部署数据库服务器 |
| 网络 | 能访问所选 RSS、自己的 Wechat2RSS HTTPS 服务和 DeepSeek 官方 API |
| 本地磁盘 | 项目目录需可写；图片、数据库、备份与历史报告会持续占用空间 |
| 云端 | Sealos Cloud 上的 Wechat2RSS 应用，具体资源规格未公开 |
| Git | 管理代码；GitHub仅作为代码与文档托管，不替代本地计划任务 |
| 浏览器 | 查看本地HTML日报，或操作云端控制台 |

无需本地 GPU、Docker、Node.js、数据库服务器或大模型运行环境。Windows脚本会尝试找到本机Python；自动查找不适用时通过 -PythonPath 明确指定。任务运行依赖已登录用户，可锁屏；关机、断网和休眠会影响按时产出，补运行也不能保证找回RSS已移除的文章。

## 服务与成本构成

项目使用者确认购买或充值的服务为 Sealos Cloud、Wechat2RSS 和 DeepSeek API。具体金额、套餐和期限不公开，因此不填写未经确认的费用，也不把免费额度当作固定可用承诺。

| 服务 | 用途 | 费用构成与当前披露情况 |
|---|---|---|
| Sealos Cloud | 托管 Wechat2RSS 容器、提供网络访问和持久化能力 | 已购买/充值并部署；具体规格与金额不公开 |
| Wechat2RSS 私有部署 | 将所订阅公众号转换为RSS | 已购买软件授权；授权期限与金额不公开，不随本仓库分发软件 |
| DeepSeek API | 正文与海报分析 | 已充值用于API调用；金额、账户和账单不公开 |
| Codex | 开发过程中协助编写、修改和测试本地代码 | 开发工具；不列入上述三项已购买服务，运行本项目不需要每天打开Codex |
| DeepSeek Harness | 独立软件读取日报并使用其已有IM功能 | 外部协作程序，不列入上述三项已购买服务，也不是采集和生成报告的必要依赖 |
| GitHub | 托管脱敏代码和文字说明 | 仓库账户与可见性由使用者选择，不在代码里保存登录凭据 |

本地阅读RSS、数据库管理和生成已有数据报告不调用模型；启用图文分析后才会调用付费API。云端保持运行、存储和流量可能持续计费，不能用“Python只用标准库”推导整体项目零费用。

本页仅披露服务名称、用途及已购买/充值状态。不公开订单号、付款截图、授权邮箱、账户ID、私人域名、余额截图或任何Key。

官方参考：[Sealos应用部署](https://sealos.io/docs/guides/app-deploy/)、[Wechat2RSS私有部署](https://wechat2rss.xlab.app/deploy/)、[DeepSeek价格说明](https://api-docs.deepseek.com/quick_start/pricing)。本页不承诺固定价格。
