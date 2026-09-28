# Brev Blackwell Monitor

每 30 分钟检查一次 Brev GPU 目录，通过本私有仓库的 GitHub Issue 提醒你。
仅查询，不创建 GPU 实例，不产生 GPU 租用费用。

本文面向使用者；后续让编程 Agent 修改项目时，请先让它阅读 [AGENTS.md](AGENTS.md)。

## 先配置 GitHub

1. 打开仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret**，添加 `BREV_API_KEY`，值为 Brev **Read** API Key。不要把密钥写进文件或聊天。
2. 在同一页面的 **Variables → New repository variable** 中添加 `BREV_API_KEY_EXPIRES_ON`，值为创建密钥时选择的到期日期，格式 `YYYY-MM-DD`。未配置时，GPU 检查仍会运行，但会提醒你到期提醒尚未启用。
3. 确认仓库 **Settings → General → Features → Issues** 已启用。默认通知仓库所有者；如果需要通知另一位有仓库访问权限的用户，添加变量 `NOTIFY_USER`，填写用户名，不带 `@`。
4. 打开 **Actions → Blackwell monitor → Run workflow**，选择 `main` 后运行。首次推送代码也会自动检查；如果首次运行时还没有配置密钥，添加密钥后可重新运行，不必重新推送。
5. 在自动创建的 **Brev Blackwell monitor** Issue 中点击 **Subscribe**。在 [GitHub 通知设置](https://github.com/settings/notifications) 中启用参与讨论和被提及时的邮件通知，并确认邮箱已验证。首次运行会 `@` 你，便于确认通知渠道。

GitHub Actions 自动提供 `GITHUB_TOKEN`，不需要创建个人 GitHub Token。
不需要配置 `ORG_ID`，脚本会从 Brev API Key 自动查询组织；已经添加的 `ORG_ID` 变量不会被使用。
工作流只申请 `contents: read` 和 `issues: write`。
如果 GitHub API 不可用或工作流在脚本执行前失败，Issue 通知无法送达；
请同时启用 Actions 失败通知，并留意 Actions 页面。

## 监控内容

- 使用与 [Brev 官方 CLI](https://github.com/brevdev/brev-cli/blob/4d93d635e09f76f6198c2f5a9f26fe7992817a3b/pkg/store/instancetypes.go) 相同的官方服务接口，按 API Key 所属组织查询。不是抓取网页 HTML，也不能保证与网页所有筛选条件完全一致。
- 检查 GPU 名称：`Blackwell`、B100/B200/B300、GB10/GB200/GB300、RTX PRO 2000/4000/4500/5000/6000（含 Server 命名），以及 RTX 5050/5060/5070/5080/5090。支持 Brev 的 `RTXPro6000` 等别名，不把 `RTX6000Ada`、A6000 或 RTX4090 当成 Blackwell。
- **首次出现、新配置出现、消失后重新出现，以及 API 可用状态转为 available** 时通知。首次运行已存在的 Blackwell 也会通知。查询包括目录中不可用的型号；通知会区分型号出现和可用状态。
- 通知会附带当时 API 返回的基础价格；仅价格变化不会单独触发通知。
- 表格提供 GPU 数量、实例类型、提供商、地区和基础价格。`unknown` 表示 API 未明确返回可用状态。即使显示 `available`，也不保证下单时一定能创建；价格可能因地区变化，存储等费用可能另计。
- 状态保存在同一个 Issue 的隐藏 JSON 中；无变化只更新状态，不反复发评论。不依赖容易失效的 Actions Cache，也不提交状态文件或保存密钥。
- Issue 正文只预览前 20 个配置，通知评论会包含本次所有新增或恢复可用的配置及其基础价格；超过 GitHub 单条评论容量时会拆分。
- 登录失效、接口错误或响应格式变化会使工作流失败，并在 Issue 提醒。失败不会清空上一份成功结果，避免把错误当成“没有 Blackwell”。
- 密钥到期前 7 天内提醒一次，配置的到期日期已过时再提醒一次。按 UTC 日期计算。更新密钥时也要更新日期变量；脚本不会自动延长或轮换密钥。

不要删除状态 Issue、修改标题或隐藏 JSON。关闭 Issue 不会停止监控，下次运行会重新打开。
如需停止，请在 **Actions → Blackwell monitor → … → Disable workflow** 禁用。
评论送达后才确认状态；如果 GitHub 在送达后、保存状态前出错，下次可能重复提醒，以免丢失通知。

## 运行频率与额度

工作流使用 UTC cron `17,47 * * * *`：每小时第 17 和 47 分钟各运行一次，
避开整点繁忙时段。GitHub 的定时任务可能延迟，不保证严格准时或不会漏过短暂库存。
默认分支必须是 `main`，工作流才会按当前配置在推送时启动。

只有一个 Linux job，无第三方 Python 依赖，也不安装浏览器或 Brev CLI。
单次 job 超时上限为 3 分钟。
按每次计费 1 分钟估算，30 天约 1,440 分钟，31 天约 1,488 分钟；
若每次计费 2 分钟则翻倍。最终以 GitHub 的实际用量为准，账号下其他私有仓库也会消耗额度。

## 本地开发

需要 Python 3.10+，只使用标准库。

```bash
python3 -m unittest discover -s tests -v
```

无密钥、无 GitHub 写操作的公开目录预览：

```bash
python3 scripts/monitor.py --dry-run --public-catalog
```

公开目录预览不代表你的组织可租到相同实例。生产工作流始终使用组织目录，
认证失败时不会偷偷回退到公开目录。

如果已经通过安全方式设置了 `BREV_API_KEY` 环境变量，可以只读预览组织目录：

```bash
python3 scripts/monitor.py --dry-run
```

脚本只允许固定服务地址，不输出密钥或原始错误响应，也不会跟随重定向发送认证信息。
