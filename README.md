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
3. **两次抽取互相不可见**。两次运行拿到完全相同的输入，谁也看不到对方的输出，
   所以两者的差异测量的是真实的不稳定性，而不是对第一次结果的确认。
4. 第三次运行在两份结果都经过证据校验之后做对账，产出最终结论。
5. 每条知识必须附带原文引用和 `message_id`，程序再确认引用确实存在，
   并记录引用在规范化正文中的偏移和该正文的哈希。
6. **状态完全由程序判定**。没有任何模型自报字段参与 `verified` 的认定，
   摘要必须用 `summary_claim_ids` 指明支撑它的 claim，否则只能是 `partial`。
7. 超长线程被切成有序分段分别抽取后合并，而不是整条丢弃；单封超长邮件会被
   如实报告为无法分析，不会截断后冒充完整。
8. SQLite 保存 schema 版本、文件哈希、线程指纹、每次 Agent 运行和失败信息，
   支持断点续跑。
9. 邮件内容被视为不可信数据，SDK 调用禁用工具，邮件中的指令不会被执行。

这能验证“结论是否有邮件证据”，但不能证明邮件中的说法本身绝对真实，
也不能凭空读取尚未导出的附件内容。引用存在也不等于结论由引用蕴含——
对账阶段会被要求删掉这类 claim，但这一步目前仍依赖模型判断。

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

不要依赖写死的模型名称。默认 `auto` 由 Cursor 选择。**两次盲跑用不同模型时，
一致性分数才真正有意义**——同一个模型跑两次主要测的是采样噪声。建议从列表中
为 `--model` 和 `--second-model` 选择两个不同模型。

## 3. 小批量验证

先估算批次数和超长线程，不调用 Cursor：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db analyze `
  --owner-email $env:EMAIL_KB_OWNER `
  --limit 20 `
  --dry-run
```

`estimated_cursor_runs` 按 `分段数 × 2 + 1` 计算：每个分段两次盲跑，每个线程
一次对账。

然后只分析 5 个线程：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db analyze `
  --owner-email $env:EMAIL_KB_OWNER `
  --limit 5
```

候选线程优先考虑邮箱主人亲自发送、存在多轮讨论或附件的线程，而不是简单按
最新日期取样，以减少自动通知占满首批分析。

用两个不同模型做盲跑：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db analyze `
  --owner-email $env:EMAIL_KB_OWNER `
  --model "<model-a-id>" `
  --second-model "<model-b-id>" `
  --reconciler-model "<model-c-id>" `
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
  `oversized`、`stale` 数量。`verified_with_gaps` 表示所有结论都有证据，但存在
  已知缺口——见 `gap_reasons`。
- `evidence_pass_rate_by_stage`：**按阶段分开**统计引用通过校验的比例。
  `extraction` 是两次盲跑的原始幻觉率，`reconcile` 是对账结果的。只看后者会
  高估质量，因为被对账删掉的 claim 不在它的分母里。
- `independent_pass_agreement`：两次盲跑引用的原文字符重合度，`mean` 和 `min`。
  这是目前唯一一个不依赖模型自我评价的稳定性指标。
- `gap_reasons`：降级原因计数，例如 `independent_passes_disagreed_on_category`、
  `attachment_content_unavailable`、`message_too_large_to_analyze`。
- `average_model_reported_confidence`：模型自报置信度。**不参与任何状态判定**，
  保留它只是为了让"模型自称的把握"和"程序实际查到的"之间的偏差可见。
- `failed_agent_runs`：Cursor SDK 调用、解析或验证失败次数。

引用通过校验只表示引用存在，既不等于结论由引用蕴含，也不等于个人有用性达到
相同比例。

## 当前限制

- 现有 Graph 导出只记录 `hasAttachments`，没有附件正文。涉及附件的线程会被
  标记为缺少上下文。
- 单封超过 `--max-chars-per-request` 的邮件无法分析，会被列入
  `unanalyzable_message_ids`，线程其余部分照常分析。整条线程的所有邮件都超限时
  才标记为 `oversized`。
- 证据校验是归一化子串匹配，只能证明引用存在。语义蕴含目前由对账阶段的模型
  判断，还没有独立的程序或第三方校验。
- 引用历史去重只处理完全一致的历史正文。真实回复常改写引用格式，因此长线程
  实际会更多地走分段路径。
- Cursor SDK 目前是 public beta，流水线因此保留了 SDK Run ID、错误和重试状态。
- 升级到 schema v2 时，此前用「抽取后复核」流程产生的分析会被标记为 `stale`
  并重新分析。那些结论的 `verified` 状态曾由模型自报字段决定，不能直接沿用。

## 开发自检

```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
