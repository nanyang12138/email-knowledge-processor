# 定义"好"

这个目录里的两份文件，是整个项目里**只有你本人能写**的部分，也是判断后续所有
工作是否有效的唯一标准。

其余所有指标衡量的都是**忠实度**：引用是否存在、claim 是否解析到原文、两次盲跑
是否一致。这些可以全部满分，而系统对你毫无价值——一条证据完备、定位精确的
claim，同样可以是一句废话。

这里衡量的是**有用性**：Agent 用了这个知识库之后，是不是把事情做得更好了。

真实文件是 gitignore 的，只有 `*.example.toml` 会进仓库。

## 你要做的是"审"，不是"写"

两份文件都能由系统从邮件里自动生成。你的工作是**审阅**：一张卡从零写要十几
分钟，扫一眼判断要十几秒。

但有一件事自动化替代不了，需要先说清楚，否则会得到一个看起来很好看的假数字。

经验卡承担两个作用：**给抽取一个目标形态**，和**当评估基准**。第一个作用自动
生成完全够用。第二个不行——如果卡片由流水线生成，再去测流水线能不能找到这些
卡片，那是拿自己的答案批自己的卷子。

所以 `cards` 命令会明确返回 `baseline_valid`：只有经你确认过的卡片才算基准。
让一张卡成为金标准的是**人的判断**，不是谁敲的字，所以"点认可"和"手写"在评估
上等价。

回放集**不存在这个问题**：答案来自邮件里事发之后的部分，而那部分会被截断规则
整条排除在检索之外，两个对照组谁都拿不到自己的答案。所以它可以放心自动生成，
你只要抽查一下就行。

## 1. 经验卡（`experience_cards.toml`）

现在两次抽取会产出粒度不同的结果，根因不是缺少某个流水线阶段，而是"一条经验"
从来没有被定义过。在没有目标形态的情况下，加多少轮复核输出都会继续漂移。

### 先让系统归纳

```powershell
# 先只看聚类，不调模型。重复出现的情况会被聚成一簇
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db induce --dry-run

# 每簇归纳出候选规则
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db induce --model "<model-id>"
```

聚类是**确定性**的，谁和谁被分到一起可以复现和检查。dry run 会给出每簇内部的
相似度，`--min-similarity` 照着这个数调，不用猜。默认阈值偏松，因为归纳提示词
的职责之一就是把"表面相似但机制不同"的案例拆开——一个从来没送进模型的簇是没有
机会被拆的。

只有**措辞**来自模型。哪些案例支持它、哪些是反例、其中多少条真的有记录到结果，
全部由程序统计。`basis` 为 `pattern_without_recorded_outcome` 表示这是一个有人
注意到的规律，不是已知有效的做法。

### 然后审

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db rules

.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db mark "<rule-id>" useful
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db mark "<rule-id>" wrong --note "两个不同问题硬凑的"
```

| 判断 | 含义 |
| --- | --- |
| `useful` | 认可，这是我的经验，进入基准 |
| `not_useful` | 太琐碎，不值得当规则 |
| `wrong` | 归纳错了 |
| `outdated` | 曾经对，现在不适用 |

导出成可编辑的卡片文件，**改比认更有价值**——你改动的地方正是只有你能补的部分：

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db export cards `
  --out evaluation\experience_cards.toml
```

导出的卡片里 `rationale` 多半是空的。决策理由几乎从不出现在邮件里，这一栏基本
只能靠你补。补上去的每一句都是知识库里原本不可能有的东西。

### 再看流水线自己能找到什么

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db cards `
  --path evaluation\experience_cards.toml
```

输出的是**候选**，不是判定。检索到的规则是不是同一条规则只有你能判断，自动打分
只会制造一个没有意义的数字。`cards_with_no_candidate` 是最有信息量的一栏：那些
卡片对应的经验，邮件里可能根本不存在。

## 2. 决策回放集（`decision_replay.toml`）

这是唯一直接对应产品目标的测量，而且可以几乎全自动生成。

```powershell
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db export replay `
  --out evaluation\decision_replay.toml
```

系统会挑出**同时记录了问题、行动和结果**的案例。`situation`、`asked_at`（结局
被记录下来的那一刻）和 `what_actually_happened` 都能从已验证的 claim 直接得到。

打开文件抽查两件事，这两件机器判断不了：

- `expected_elements` 是不是**真正起作用**的东西。系统填的是"邮件里说做了什么"，
  这和"什么起了作用"不总是一回事。
- `situation` 有没有掺进事后视角。

**不值得测的直接删掉。** 一个你信得过的小集合，胜过一个你没读过的大集合。

然后跑：

```powershell
# 先只看检索到了什么、两份提示词长什么样，不调用模型
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db replay --prompts-only

# 真正的 A/B：同一个模型分别在有和没有知识库的条件下作答
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db replay `
  --model "<model-id>" --out evaluation\reports\replay-001.json
```

```powershell
# 先只看检索到了什么、两份提示词长什么样，不调用模型
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db replay `
  --path evaluation\decision_replay.toml --prompts-only

# 真正的 A/B：同一个模型分别在有和没有知识库的条件下作答
.\.venv\Scripts\python.exe -m email_kb --db data\knowledge.db replay `
  --path evaluation\decision_replay.toml `
  --model "<model-id>" `
  --out evaluation\reports\replay-001.json
```

`mean_coverage_delta` 就是这个项目的全部价值。打分是确定性的要点覆盖，不是
模型评判——这样换模型之后前后两次结果仍然可比。完整报告里保留了两个条件下的
原始回答，覆盖率毕竟只是个摘要，不认同它的时候必须能翻回去看原文。

回放时**在 `asked_at` 当时或之后才结束的线程会被整条排除**（不只是晚开始的），
否则答案会顺着一条后来才结束的线程漏回问题里，整个对照就失去意义。

## 3. 读结果

| 结果 | 说明 | 下一步 |
| --- | --- | --- |
| delta 明显为正 | 知识模型有效 | 按计划推进平台化，用这里的排序决定优先级 |
| delta 接近 0，但检索内容相关 | 知识**正确但没用** | 回到经验卡细化 ontology，不要动存储引擎 |
| delta 接近 0，检索内容不相关 | **情境检索**不行 | 改情境指纹和排序，也不需要换数据库 |
| delta 为负 | 检索到的内容在误导 | 先看 `tasks_regressed` 的原文，多半是把"有先例"当成了"这样做对" |

在拿到这个信号之前，Postgres、Qdrant、Temporal、Graphiti 的选型讨论都是过早
优化。
