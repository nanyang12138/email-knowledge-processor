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
.\.venv\Scripts\python.exe -m pip install -e ".[dev,agent]"
```

`agent` 是接入 Claude Code 和 Cursor 需要的 MCP 依赖。

Cursor SDK API Key 在
[Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations)
创建。不要把密钥写入项目文件：

```powershell
$env:CURSOR_API_KEY = "cursor_..."
$env:EMAIL_KB_OWNER = "your.mailbox@example.com"
```

`CURSOR_API_KEY` 只在当前 PowerShell 会话中有效。

安装后也可以用一键脚本完成导入、分析、质量报告、建索引和体检；未设置 API Key
时会安全提示输入，不会把密钥写入磁盘：

```powershell
.\scripts\run-analysis.ps1 `
  -InputPath "$env:OneDrive\EmailKnowledgeBase" `
  -OwnerEmail $env:EMAIL_KB_OWNER `
  -Database "C:\Users\nanyang2\email-kb-data\knowledge.db" `
  -Limit 20
```

加 `-DryRun` 只估算调用次数，不调用 Cursor。`-Database` 建议指向仓库外面：
数据库是邮箱的派生物，放在仓库外就没有被提交的可能。

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

## 数据放在哪里

知识库是邮箱的派生物：`messages` 存正文，`analysis_runs` 存每次模型调用的完整
输出。**导出这个数据库等于导出邮箱。**

所以：

- 代码可以公开，数据不行。仓库里已经忽略了 `data/`、`*.db` 和
  `evaluation/*.toml`（示例除外）。
- `doctor` 会检查数据库是不是躺在某个 git 工作区里且没被忽略，是的话直接报警
  并给出修法。挂到 Agent 之前跑一次。
- 要做一个聚合多个知识源的 personal-agent 仓库，那个仓库应该只放**配置和
  说明**，数据库用绝对路径引用，留在仓库外面。

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

## 5. 建立检索索引

分析结果确认后，把 case 和 claim 建成可检索索引：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db index
```

索引是纯派生层，每次重建都会先清空。**只索引 `verified` 和 `verified_with_gaps`**，
未通过校验的知识不会进入 Agent 的上下文。

检索按任务意图分四种模式，而不是一个通用搜索框：

```powershell
# 我现在遇到这个情况，过去有类似的吗
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db search `
  --mode cases "夜间构建在 Windows 上失败"

# 有哪些可复用规则可能适用
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db search --mode rules "构建失败"

# 这个做法我是不是已经试过了
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db search --mode prior "锁定工具链版本"

# 精确标识符，不会被语义相近的正文淹没
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db search --mode identifier "CL 12345"

# 展开某个 case 的全部 claim 和证据
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db case "<thread-id>"
```

每条结果都带 `status`、`gap_reasons`、`outcome_state`、`evidence`、`advisories`
和可展开的 `ranking.components`。`outcome_state` 为 `unknown` 表示**这件事的结果
从来没人写下来**，不能当作"这个做法有效"。

## 6. 反馈标注

重要度标定和个人相关性只能靠你的信号校准，模型没有别的来源。标注数据越早开始
积累越好，所以这一步不要等到最后：

```powershell
# 还没判断过的知识，按重要度排序，附带原文引用
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db review --limit 20

# 记录判断。target 是 claim uid（thread:claim）或 thread id
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db mark "<thread>:c1" useful
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db mark "<thread>" wrong --note "把通知要求当成了实际行动"
```

四种判断的效果**故意不对称**：

| 判断 | 效果 |
| --- | --- |
| `useful` / `not_useful` | 只影响排序，不改变知识内容 |
| `wrong` | 对 Agent 隐藏，但 `case` 命令仍可按 id 打开查看 |
| `outdated` | 保留并降权，每条结果附加"已不再适用"的告诫 |

反馈是用户数据不是派生层，存在独立表里、查询时关联，**重建索引不会丢失**。
记录只追加不覆盖，改变判断时旧记录仍在，最新一条生效。

## 7. 给 AI Agent 用（MCP）

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[agent]"

# 先体检：schema、索引是否最新、mcp 是否装了、数据库会不会被误提交
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db doctor

# 生成配置，路径已经解析成绝对路径
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db mcp-config --client cursor
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db mcp-config --client claude
```

把输出里的 `config` 部分贴进对应文件：

| 客户端 | 项目级 | 全局 |
| --- | --- | --- |
| Cursor | `.cursor/mcp.json` | `~/.cursor/mcp.json` |
| Claude Code | `.mcp.json` | `~/.claude.json` |

两边用的是同一套 `mcpServers` 结构，所以同一份配置可以同时挂给两个客户端；
SQLite 开了 WAL，多个客户端并发读同一个库没有问题。

**读和写是两种权限。** 默认只读。加 `--allow-feedback` 后 Agent 多一个
`record_usefulness` 工具，可以回写"这条有没有帮上忙"——但它只能影响排序，
拿不到 `wrong` 和 `outdated`。那两个判断改变可见性，属于你的声明，不该由
Agent 从一次任务顺不顺利去推断。

工具按**任务意图**命名，而不是按检索方式：

- `find_similar_cases(situation)`：我现在的情况过去发生过吗
- `get_applicable_rules(context)`：有哪些规则可能约束现在这件事
- `check_if_i_tried_this_before(approach)`：这个做法试过没有，结果如何
- `lookup_identifier(identifier)`：精确查 CL / bug / ticket / build / commit
- `get_case(thread_id)`：展开证据自行核对
- `knowledge_coverage()`：知识库现在覆盖了多少、可靠性如何

叫 `search_knowledge` 的话，Agent 会把它当搜索框用完就走；按意图命名才会引导出
"先看看这个人以前怎么做的"这一步。排序信号里包含**这条经验过去的结果是好是坏**，
这是纯文档检索无法表达的。

## 8. 跨案例归纳

三条几乎相同的自动通知应该变成**一条规则加三个实例**，而不是三张知识卡。
没有这一步，重复通知会靠数量淹没知识库，而真正值得留下的那条规则哪儿都没写。

```powershell
# 先只看聚类，不调模型
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db induce --dry-run

# 归纳候选规则，然后审阅
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db induce --model "<model-id>"
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db rules
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db mark "<rule-id>" useful
```

聚类是**确定性**的，谁和谁被分到一起可以复现；dry run 会给出每簇内部相似度，
`--min-similarity` 照着调而不用猜。只有**措辞**来自模型——支持案例、反例、
其中有多少条真的记录了结果，都由程序统计。`basis` 为
`pattern_without_recorded_outcome` 表示这是个规律，不是已知有效的做法。

## 9. 判断它到底有没有用

上面所有指标衡量的都是**忠实度**——引用是否存在、两次盲跑是否一致。这些可以
全部达标而系统对你毫无价值。完整流程见
[evaluation/README.md](evaluation/README.md)，这里只说要点。

两份评估文件都能自动生成，你的工作是**审**不是**写**：

```powershell
# 已认可的规则导出成可编辑的经验卡
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db export cards `
  --out evaluation\experience_cards.toml

# 从记录了问题、行动和结果的案例生成回放任务
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db export replay `
  --out evaluation\decision_replay.toml

# A/B：同一个模型，分别在有和没有知识库的条件下回答同一批过去的问题
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db replay `
  --model "<model-id>" --out evaluation\reports\replay-001.json
```

`mean_coverage_delta` 就是这个项目的全部价值。

有一件事自动化替代不了：**未经你确认的卡片不能当评估基准**，因为那是拿流水线
自己的答案批流水线自己的卷子，所以 `cards` 会返回 `baseline_valid`。回放集没有
这个问题——答案来自事发之后的邮件，而**在 `asked_at` 当时或之后才结束的线程会被
整条排除**在检索之外，两个对照组谁都拿不到自己的答案。

## 开发自检

```powershell
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
