# Email Knowledge Processor

把 Microsoft Graph 导出的 `RawJSON` 或 CSV 转换为可审计的个人邮件知识库。
本机只负责解析和 SQLite 存储；语义提取与独立复核使用 Cursor SDK
的云端模型，不需要本地 GPU。

完整产品方向与公开发布计划见
[个人知识系统产品计划](docs/PRODUCT_PLAN.md)。

## 准确性设计

这不是一次“让模型总结全部邮件”的脚本：

1. 原始 `body` 和完整邮件 JSON 原样保存，清洗结果单独写入 `clean_body`。
2. 邮件按 `conversation_id` 组成完整线程后再分析。
3. Cursor 第一次运行负责提取事实、决策、结果和经验。
4. 第二次独立运行负责删除无依据结论、补充遗漏并重新评分。
5. 每条知识必须附带原文引用和 `message_id`。
6. 程序再次确认引用确实存在于对应邮件；失败内容不会标记为 `verified`。
7. SQLite 保存文件哈希、线程指纹、每次 Agent 运行和失败信息，支持断点续跑。
8. 邮件内容被视为不可信数据，SDK 调用禁用工具，邮件中的指令不会被执行。

这能验证“结论是否有邮件证据”，但不能证明邮件中的说法本身绝对真实，
也不能凭空读取尚未导出的附件内容。

## 安装

项目使用 Python 3.11。Windows PowerShell：

```powershell
cd C:\Users\nanyang2\email-knowledge-processor
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Cursor SDK API Key 在
[Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations)
创建。不要把密钥写入项目文件：

```powershell
$env:CURSOR_API_KEY = "cursor_..."
$env:EMAIL_KB_OWNER = "your.mailbox@example.com"
```

`CURSOR_API_KEY` 只在当前 PowerShell 会话中有效。

安装后也可以使用一键脚本完成导入、分析和质量报告；未设置 API Key 时会
安全提示输入，不会把密钥写入磁盘：

```powershell
.\scripts\run-analysis.ps1 `
  -InputPath "$env:OneDrive\EmailKnowledgeBase" `
  -OwnerEmail $env:EMAIL_KB_OWNER `
  -Limit 5
```

加 `-DryRun` 可以只估算批次数，不调用 Cursor。

## 1. 导入

初始化数据库：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db init
```

导入单个 CSV：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db ingest `
  "C:\path\to\EmailKB_2026-07_page-001.csv"
```

导入 OneDrive 中的完整目录：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db ingest `
  "$env:OneDrive\EmailKnowledgeBase\RawJSON" `
  "$env:OneDrive\EmailKnowledgeBase\CSV"
```

同一文件内容未变化时会自动跳过。RawJSON 和 CSV 中重复出现的邮件按
`email_id` 合并，不会重复保存。

查看导入统计：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db stats
```

## 2. 选择 Cursor 模型

API Key 配置完成后查看当前账户实际可用的模型：

```powershell
.\.venv\Scripts\python.exe -m email_kb models
```

不要依赖写死的模型名称。默认 `auto` 由 Cursor 选择。为了提高独立复核价值，
可以从列表中为 `--model` 和 `--verifier-model` 选择两个不同模型。

## 3. 小批量验证

先估算批次数和超长线程，不调用 Cursor：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db analyze `
  --owner-email $env:EMAIL_KB_OWNER `
  --limit 20 `
  --dry-run
```

然后只分析 5 个线程：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db analyze `
  --owner-email $env:EMAIL_KB_OWNER `
  --limit 5 `
  --max-threads-per-batch 2
```

候选线程优先考虑邮箱主人亲自发送、存在多轮讨论或附件的线程，而不是简单按
最新日期取样，以减少自动通知占满首批分析。

指定不同提取模型和复核模型：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db analyze `
  --owner-email $env:EMAIL_KB_OWNER `
  --model "<extraction-model-id>" `
  --verifier-model "<verification-model-id>" `
  --limit 5
```

通过验证且线程内容未变化的结果不会重复调用 Cursor。需要重新分析时加
`--force`。

## 4. 查看质量报告

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db report
```

报告包括：

- `status_counts`：`verified`、`verified_with_gaps`、`partial`、`rejected`、
  `oversized` 数量；`verified_with_gaps` 表示现有结论都有证据，但线程缺少后续结果
  或附件等上下文。
- `evidence_pass_rate`：模型引用通过本地原文校验的比例。
- `average_verifier_confidence`：第二次独立复核给出的平均置信度。
- `failed_agent_runs`：Cursor SDK 调用、解析或验证失败次数。

`evidence_pass_rate` 只表示引用存在，不等于个人有用性达到相同比例。

## 当前限制

- 现有 Graph 导出只记录 `hasAttachments`，没有附件正文。涉及附件的线程会被
  标记为缺少上下文。
- 超过 `--max-chars-per-batch` 的单个线程会标记为 `oversized`，不会被截断后
  冒充完整分析。
- Cursor SDK 目前是 public beta，流水线因此保留了 SDK Run ID、错误和重试状态。
- 当前版本提供全文证据数据库和结构化知识 JSON；向量检索与 Agent 问答接口将在
  小批量质量确认后再添加，避免在错误提取结果上建立索引。

## 开发自检

```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
