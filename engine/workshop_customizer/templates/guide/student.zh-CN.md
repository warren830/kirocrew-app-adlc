{{marker}}
{{draft_banner}}
# Eval-First：{{title}}

**{{tagline}}**

---

> [!NOTE]
> 这是配套 **《Eval-First：用 AgentCore 构建企业级 Agent》** workshop 的动手指南，已按本场景定制。知识库文档、工具
> Lambda 的 fixtures、Skills、提示词和 practice golden 问题来自 `pack/`；基础设施、评估器和脚本顺序沿用锁定的模板。
> 按顺序跑完这些脚本，就能在讲师为本场景准备好的 Workshop 环境里搭出整个 Agent（见[前置条件]({{anchor_prerequisites}})）。

> [!IMPORTANT]
> **请从上到下照着本文走。** 每一步都写清了它做什么、依赖什么、产出什么。脚本要**按编号顺序**跑——每个脚本结束时会打印
> `Next: ...` 指向下一步。

{{generated_by}}

---

## 目录

{{toc}}

---

## 1. 场景

{{scenario_intro}}

{{scenario_table}}

### 角色

{{roles_table}}

### 哪些是真实的，哪些是合成的

{{provenance_banner}}

**客户已确认的事实**——只有这些陈述描述该组织的真实规则：

{{customer_facts}}

**合成教学设定**——为本课程编写；绝不能当作该组织的制度引用：

{{synthetic_facts}}

{{asset_summary}}

---

## 2. 这套东西在搭什么，为什么这么搭

这个 workshop 的核心是 **eval-first**：重点在于先立起一个贴近真实的企业 Agent，再**用代码化的评估器去量化它的质量**，
而不是凭眼睛扫几条回答。脚本会搭出 **{{title}}**（基于 Amazon Bedrock AgentCore 的知识库 + Gateway 工具 + Memory +
Skills），跑起来产出 trace，再用两个自定义评估器和一组确定性的代码检查给这些 trace 打分。

这两个评估器是整个 workshop 的核心。它们放在 `evaluators/` 下，是对**已发表研究方法**的独立复现（不是 AWS 产品），
并被接到 Amazon Bedrock AgentCore 上运行：

### `thelma_eval/` —— 单轮 RAG 质量（THELMA）

在 **`TRACE`** 级别运行（即*玻璃盒*粒度——它检视一条执行轨迹）。把一次问答拆成 `(问题, 检索到的来源, 回答)`。
THELMA 论文定义了 **6 项指标**；本实现把 **Source Precision 拆成两个分数**上报（chunk 级 vs. fact 级），所以你每条
trace 会看到 **7 个数**（都是 0–1）：

| 指标 | 全称 | 它回答什么问题 |
|:------:|------|---------------------|
| **SP1** | Source Precision（chunk 级）| 检索回来的**整块 chunk**相关吗？ |
| **SP2** | Source Precision（fact 级） | chunk **内部的事实**里，有多少是真正相关的？ |
| **SQC** | Source Query Coverage（来源覆盖率）   | 来源能覆盖这个问题吗？ |
| **RP**  | Response Precision（回答精确率）      | 回答是否切题？ |
| **RQC** | Response Query Coverage（回答覆盖率） | 问题是否被完整回答？ |
| **SD**  | Self-Distinctness（自洽不重复）       | 回答内部有没有重复啰嗦？ |
| **GR** | **Groundedness（有据性）** | **每句话都有来源支撑吗？（即有没有幻觉，通过线 ≥ {{gr_pass}}）** |

它真正的价值在于**诊断**——这些分数之间的*相互关系*能指出该修 RAG 的哪个环节（检索器 vs. 提示词 vs. 源文档）。
SP1/SP2 的拆分就是最典型的例子：**SP1 高但 SP2 低**，说明 chunk 看着切题，但它携带的*事实*大多是噪声——这正是
源文档里混入脏数据的征兆。评估器会打印这些模式：

{{diagnosis_table}}

### `mtg_eval/` —— 多轮目标达成（Mind the Goal）

在 **`SESSION`** 级别运行（即*黑盒*粒度——端到端的目标达成结果），分三步：**切分目标**（把围绕同一件事的多轮合并成
一个目标）、**判定成功/失败**（一个目标里任意一轮失败则整个目标算失败），然后算出 **GSR** = *Goal Success Rate
（目标达成率）*（成功目标数 ÷ 总目标数，通过线 ≥ {{mtg_pass}}%），并用 **RCOF** = *Root Cause of Failure（失败根因）*
（7 类缺陷归因）给每个失败定位。它回答的是*「Agent 到底有没有完成用户来办的事」*。本 release 的裁判提示词带上了本场景的
规则（第 1 节的范围、转交条件和禁止行为），所以规则要求的拒绝或转交算作成功，而不是 *Refusal to Answer*。

### `l1_eval.py` —— 场景断言（L1 代码检查）

`09-run-eval.sh` 还会运行 `l1_eval.py`：对照 golden 期望，对**每个** practice 问题做确定性检查——必须 / 禁止的工具调用、
回答必须包含或不能包含的词句，以及该拒绝或该转交时有没有做到。它们零成本、从不波动，而且覆盖了 THELMA 不打分的问题
（工具查询、拒绝、转交）。L1 判不了的检查交给 Mind the Goal 的结论。

> [!NOTE]
> **TRACE → 玻璃盒**、**SESSION → 黑盒** 这个对应是刻意的：AgentCore 的 session / trace / span 三个层级，正好对上
> 配套白皮书里的三种评估粒度（黑盒 / 玻璃盒 / 白盒）。在那套框架里，这两个评估器是**自定义的 L2 评估器**
> （经校准的 LLM-as-a-judge），`l1_eval.py` 是它的 **L1** 层——见[下文]({{anchor_methodology}})。

裁判模型是 `00-config.sh` 里的 `WORKSHOP_JUDGE_MODEL`（默认与 Agent 模型相同，即 `us.amazon.nova-2-lite-v1:0`）；
本场景声明的是 `{{judge_model}}`，`08-create-evaluators.sh` 会打印它实际设置的模型。每个评估器都打包了自己的算法、
一层**适配层**（ADOT span → 评估器输入）和一个 Lambda handler。

> [!TIP]
> 完整的指标定义、THELMA 诊断表、论文引用和许可信息，见 **[`evaluators/README.md`](evaluators/README.md)**。

{{noise_paragraph}}

---

## 3. 架构速览

Agent 以 VPC 模式跑在一个 AgentCore **Harness** 里。每次调用都会把 Memory + Skills 拉进上下文，通过 **Gateway**（MCP）
调用本场景的工具，并吐出 OTel trace span 流向 CloudWatch——评估器就在那里读取它们。

{{architecture_diagram}}

> 实线是真实调用路径；虚线是 trace / 裁判流。

{{names_table}}

---

## 4. Eval-First 循环（ADLC）

本 workshop 闭合了 **Agent 开发生命周期（ADLC）**：构建、运行、采集 trace、评估、_诊断_，再优化——并用一次重新评估来
证明修复确实有效。

1. **构建**（一次性）：第 1–7 步创建知识库、工具、Skills 和 Agent。
2. **运行**：第 8–10 步和 `09-run-eval.sh` 提问。
3. **采集 trace**：每个回答都在 CloudWatch `aws/spans` 留下 span。
4. **评估**：THELMA 给检索 trace 打分，Mind the Goal 评每个 session，L1 检查每个问题。
5. **诊断**：分数模式告诉你是收紧提示词（留在环上），还是修检索（当 SP2 接近 0 时分叉出去）。
6. **优化**：`10-optimize-prompt.sh` 装上优化后的提示词——并重新评估来证明它。

> [!TIP]
> {{adlc_tip}}这个对比恰恰就是 THELMA 区分*「该修提示词」*还是*「该修检索」*的方式。

---

## 5. 与 Eval-First 方法论的对应

这个 workshop 是一套四篇《企业生产级智能体开发部署指南》白皮书的**动手姊妹篇**。白皮书讲*为什么*和方法论框架，
这个 release 让你把它端到端跑一遍。对应关系：

| 白皮书概念 | 在这里对应跑什么 |
|---------------------|-------------------|
| **ADLC** —— 构建 → 运行 → 采集 trace → 评估 → 诊断 → 优化 的飞轮 | 整个脚本序列；`10-optimize-prompt.sh` 闭合循环 |
| **三种评估粒度** —— 黑盒 / 玻璃盒 / 白盒，对应 AgentCore 的 **session / trace / span** | **Mind the Goal = SESSION（黑盒）**、**THELMA = TRACE（玻璃盒）** |
| **三层证据权重** —— L1 代码 / L2 校准过的 LLM-judge / L3 默认拒评 | **L1** = `l1_eval.py`，由 `09-run-eval.sh` 在每个 practice 问题上运行；THELMA 和 Mind the Goal 都是**自定义 L2 评估器**；`13-judge-stability.sh` 检验 L2 可信度，人工 TPR/TNR 校准是其 L3 补充 |
| **决策为先 KPI** —— 决策质量 / 响应时效 / 认知卸载 | 质量（GR）+ 速度与成本（`11-cost-latency.sh`）把前两个维度变成硬数字 |
| **AgentCore Evaluations** —— 内置 + 自定义评估器 | 这里两个评估器都是**自定义**的（打包成代码的 LLM-as-a-judge），由 `08-create-evaluators.sh` 部署 |

> [!TIP]
> `10-optimize-prompt.sh` 是一个**手动**的「优化并重新评估」循环。AgentCore **Optimization**（public preview）把同一个
> 思路产品化了——在 AgentCore Evaluations 之上提供 Recommendations、版本化的 Configuration bundles 和 A/B testing。
> 这里的手动循环就是它背后的概念原型。

---

## 6. 前置条件

### 在哪里运行

这个定制的 release **只能在讲师用 Workshop Customizer 为本场景准备好的 Workshop 环境里运行**（每个场景一个环境）。
把它解压到别的账号里是跑不起来的。准备好的环境提供：

| 要求 | 脚本为什么需要它 |
|-------------|-------------------------|
| **`us-west-2`** 的一台 EC2 工作环境，由 `workshop-infra` 栈创建，通过 SSM 连入 | 所有脚本都在那里运行；第 2 步会找到这个栈，只打印它的输出 |
| 同一账号里的 **`workshop-customizer-addons`** CloudFormation 栈 | 第 6 步（`04-deploy.sh`）读取它的 `SkillsFilesAccessPointArn` 输出，没有就停止；`99-cleanup.sh` 会删除它 |
| `/opt/workshop-customizer/` 下的 Workshop Customizer 主机工具，并且本 release 已被应用为**当前生效**的 release | 第 7 步（`05-setup-memory.sh`）会运行 `/opt/workshop-customizer/runtime_permissions.py`，它拒绝不是当前生效的 release |
| 那台 EC2 上的 release 根目录 **`~/workshop/current`** | 里面有 `RELEASE.json`、本 README 和所有脚本；一切都从那里运行 |

缺任何一项都请停下来问讲师——不要自己部署这些栈。

### 工具

| 要求 | 说明 |
|-------------|-------|
| AWS 账号 | 准备好的 Workshop 环境所在的账号，并在 **`us-west-2`** 有那台用于运行的 EC2 实例 |
| AWS CLI | 已配置好凭证（`aws sts get-caller-identity` 要能成功） |
| Node.js | v20+ |
| Python | 3.10+（带 `pip`） |
| AgentCore CLI | `npm i -g @aws/agentcore@preview` |

### IAM 权限

你运行所用的身份（准备好的环境里的 EC2 实例角色）需要有创建和管理以下服务的权限。

> [!WARNING]
> **只读或权限过窄的角色会失败。** 脚本会涉及：
> `cloudformation`、`ec2`（VPC/子网/NAT/SG）、`s3` + `s3vectors`、`iam`（创建/挂载角色和策略）、`bedrock` +
> `bedrock-agent` + `bedrock-agentcore-control`、`lambda`、`ssm`、`logs`、`xray`、`application-signals`、`sts`。

实例角色由讲师在准备环境时配置好；不要自己放宽它。

### 区域

在 release 根目录 `~/workshop/current` 下、你运行一切的那个 shell 里**一次性**设好区域（所有脚本默认 `us-west-2`）：

```bash
export AWS_DEFAULT_REGION=us-west-2
cd static/scripts
chmod +x *.sh
```

`static/scripts/` 里每个脚本有一个小包装脚本，它用相同的参数运行 release 根目录下的同名脚本。

{{note_prerequisites}}

---

## 7. 执行顺序速览

下面的耗时是模板在一个空白账号（us-west-2）上端到端跑出来的近似值。整体约 **25–30 分钟**，大部分是无人值守的等待。

{{execution_table}}

{{timing_footnote}}

---

## 8. 逐步说明

### 第 1 步 —— `00-setup.sh`  ·  _Phase 0_
校验 `agentcore`、`node`、`aws` 是否已安装，打印你的账号/区域，并创建 Skill 目录 {{skill_dirs}}。

```bash
./00-setup.sh
```

你应该看到打印出的账号 ID 和区域。

{{note_setup}}

### 第 2 步 —— `00-deploy-infra.sh`  ·  _Phase 0 —— 必须_
部署 `workshop-infra` CloudFormation 栈：VPC、私有子网、NAT、安全组、**数据** S3 桶 + Access Point，以及一个 EC2
工作环境（通过 SSM 连入）。模板会自动挑选 AgentCore 支持的 AZ。在空白账号上约 5–8 分钟。

```bash
./00-deploy-infra.sh
```

> [!CAUTION]
> **不要跳过这一步。** 后续步骤依赖这个栈的输出：
> - `01-create-kb.sh` 要读 **`DataBucketName`** 输出才知道把知识库数据源放哪——栈不存在它就会**失败**。
> - `04-deploy.sh` 用 VPC/子网/SG 输出来把 Harness 部署成 VPC 网络模式。
>
> 脚本是幂等的：如果 `workshop-infra` 栈已存在——准备好的环境里正是如此——它会跳过创建、只打印输出。

{{note_infra}}

### 第 3 步 —— `01-create-kb.sh`  ·  _Phase 0_
从 `pack/knowledge-base/docs/` 复制本场景的 {{doc_count}} 份知识库文档（会先清空文档目录，所以不会入库之前场景的文档），
然后创建由 **Amazon S3 Vectors** 支撑的 Amazon Bedrock 知识库 `{{kb_name}}`（嵌入模型 `amazon.titan-embed-text-v2:0`，
1024 维），删除它的 S3 前缀下过期的对象，把文档入库，并把 KB ID 存到 SSM 的 `{{ssm_prefix}}/knowledge_base_id`。
下一步的 Lambda 直接从这里读——**不需要手动设环境变量**。

本场景的文档：

{{documents_table}}

> [!NOTE]
> {{noise_sentence}}文中引用的模型（`amazon.titan-embed-text-v2:0`、`us.amazon.nova-2-lite-v1:0`）是作为托管的
> Amazon Bedrock 模型调用的——不包含也不分发任何模型权重。

```bash
./01-create-kb.sh
```

完成时会打印完整的 KB 详情（ID、数据位置、向量库、嵌入模型）。数据桶名是从 `workshop-infra` 栈的输出 `DataBucketName`
自动读取的——**所以第 2 步必须先完成**，否则脚本会以
`Stack 'workshop-infra' has no DataBucketName/SkillsBucketName output — is workshop-infra deployed?` 中止。

> [!TIP]
> **成本提示：** Amazon S3 Vectors 按存储 + 查询计费（没有常开集群），所以比常开的向量数据库便宜得多——但**用完仍然要删**
> （见清理章节）。

{{note_knowledge_base}}

### 第 4 步 —— `02-create-gateway.sh`  ·  _Phase 2_
一步做两件事：
1. 打包并部署本场景的工具 **Lambda**（`{{lambda_name}}`：通用 handler 加上本场景的 `fixtures.json`）及其 IAM 角色
   （带从 SSM 读 KB ID、查询知识库的权限）。
2. 通过 `gateway/create_gateway.py` 创建 **Gateway** `{{gateway_name}}`（MCP 协议，AWS_IAM 鉴权），并把上面那个
   Lambda 设为它的 target `{{target_name}}`。Gateway ARN 会写入 SSM 的 `{{ssm_prefix}}/gateway_arn`。

```bash
./02-create-gateway.sh
```

Gateway 暴露 {{tool_count}} 个工具：

{{tools_table}}

trace 里工具名形如 `{{compact_target}}___<tool>`（target 名去掉连字符）。

{{note_gateway}}

### 第 5 步 —— `03-configure-skills.sh`  ·  _Phase 2_
{{skills_sentence}}它们会在下一步被挂载进 Harness（BYO Filesystem）。

```bash
./03-configure-skills.sh
```

{{note_skills}}

### 第 6 步 —— `04-deploy.sh`  ·  _Phase 2_
创建 Harness 项目 `{{agent_name}}` 和 Memory `{{memory_name}}`（semantic + user-preference 策略），按 ARN 挂上
**已存在的** Gateway（这样不会重复创建 Gateway——这正是部署能**一次成型**的原因），从 `pack/prompts/baseline.md`
装上基线 system prompt，把 `allowedTools` 限制为 `@{{target_name}}/*`，通过 `workshop-customizer-addons` 的
access point 挂载 Skills 文件系统，然后部署。

```bash
./04-deploy.sh
```

> [!NOTE]
> **网络模式：** 有了 `workshop-infra` 栈（第 2 步），Harness 会用该栈的子网/SG 部署成 **VPC** 模式。

{{note_agent}}

### 第 7 步 —— `05-setup-memory.sh`  ·  _Phase 2_
先为当前生效的 release 准备运行时（`/opt/workshop-customizer/runtime_permissions.py`），再在已部署的 Harness 上配置
Memory **检索**，这样每次调用都会自动从 Memory 拉取用户的偏好（`/users/{actorId}/preferences`，前 20 条）和事实
（`/users/{actorId}/facts`，前 10 条）并注入上下文。

```bash
./05-setup-memory.sh
```

> [!NOTE]
> `04-deploy.sh` 已经*创建*了 `{{memory_name}}`。这一步是把每次调用的自动*检索*接上——两者不是一回事。

{{note_memory}}

### 第 8 步 —— `06-test-conversation.sh`  ·  _Phase 3_
用一个全新的 session ID 和 `actor-id {{first_actor}}` 跑第一次对话，提问：

{{first_query}}

此刻回答会刻意地很通用——Agent 还不了解你。脚本会先报出这个问题，最后打印这条提示：

{{first_console}}

{{memory_lesson}}

这一步也会产出第一条 **trace**。

```bash
./06-test-conversation.sh
```

{{note_conversation}}

---

### 第 9 步 —— `07-setup-eval-env.sh`  ·  _Phase 4 准备_
安装 `uv`（打包评估器 Python 依赖需要它）并开启 **CloudWatch Transaction Search**，这样 Agent 的 OTel trace span 才会
落到 CloudWatch、被评估服务读到。

```bash
./07-setup-eval-env.sh
```

> [!TIP]
> 如果脚本提示你，把 `uv` 加进 PATH：
> ```bash
> export PATH="$HOME/.local/bin:$PATH"
> ```

{{note_eval_env}}

### 第 10 步 —— `06-test-conversation.sh` *（再跑一次）*  ·  _Phase 4_
Transaction Search 只会捕获它**开启之后**产生的 span。重跑一次对话，生成一条评估器能读到的 trace（同一个问题，
同一个 actor `{{first_actor}}`）：

```bash
./06-test-conversation.sh
```

{{note_conversation_rerun}}

### 第 11 步 —— `08-create-evaluators.sh`  ·  _Phase 4_
注册并部署两个自定义的代码化评估器，再给它们的执行角色授予 Bedrock invoke 权限（LLM 裁判需要）：

- `{{thelma_evaluator}}` —— **TRACE** 级，RAG 质量（7 个分数，见上方指标表），主分 = **Groundedness（有据性）**
- `{{mtg_evaluator}}` —— **SESSION** 级，**目标达成率（GSR）** + 失败归因（**RCOF**），按本场景的规则评判

每个指标的含义见 [`evaluators/README.md`](evaluators/README.md)。

```bash
./08-create-evaluators.sh
```

{{note_evaluators}}

### 第 12 步 —— `09-run-eval.sh`  ·  _Phase 4_
默认会问下面 **{{total_questions}} 个 practice 问题**，每个问题都用一个**新的 Memory actor**
（`<persona>-<phase>-<epoch>-q<i>`，所以不会有回答复用其他问题或其他运行的 Memory），等 **{{probe_count}} 条检索 trace**
索引完成，再评估并为每条打印：**Query**、截断后的 **Response**、以及**分数**——检索 probe 上的 THELMA 7 个分数拆解 +
诊断，每个问题上的 Mind the Goal GSR + RCOF，最后还有一张覆盖每个问题的 L1 表。每次运行都会在
`~/workshop/eval-runs/{{agent_name}}/<phase>-<epoch>/` 下留一份记录。

{{practice_table}}

```bash
./09-run-eval.sh                        # 问 {{total_questions}} 个 practice 问题，再评估它们（THELMA、Mind the Goal、L1）
./09-run-eval.sh --eval-only [N]        # 跳过对话；评估最近 N 条含检索的 trace（默认 {{probe_count}}）
./09-run-eval.sh <trace-id>             # 只跑 THELMA，针对一条 trace
./09-run-eval.sh <session-id> session   # 只跑 Mind the Goal，针对一个 session
```

#### 读懂输出

每个被评估的问题会打印三行，然后是评估器的解释。一条 THELMA trace：

```text
  Query:    <practice 问题>
  Response: <回答的前 300 个字符> …[截断]
  Score:    trace=<16 位十六进制> value=0.62 [Fail] case=<case id>
     THELMA 7 维 (query: …): GR(接地/防幻觉)=0.62 | SP1(块级检索精度)=0.40 | SP2(事实级检索精度)=0.20 | SQC(源覆盖)=0.75 | RP(响应精度)=0.55 | RQC(响应覆盖)=0.60 | SD(去重)=0.80. 诊断: SP↓ SQC↑->Retriever
```

中文标签由模板的评估器打印；`value` 就是 GR，`[Pass]` 表示 GR ≥ {{gr_pass}}。

{{thelma_legend}}

{{diagnosis_levels}}

一个 Mind the Goal session：

```text
  Score:    trace=? value=1 [Pass] case=<case id>
     Mind the Goal: GSR=100.0% (1/1 目标达成), 轮次数=1. 失败归因: 无失败
```

{{mtg_reading}}

{{rcof_table}}

L1 表收尾：每个 practice 问题一行，列出 L1 状态、失败的检查、GR 和 Mind the Goal 标签，最后一行汇总（`L1: x/y pass, …`）。

{{l1_statuses}}

{{l1_compare}}

进度和重试提示（进度行由模板的脚本以中文打印）：

{{retry_messages}}

本场景要重点看：

{{what_to_look_for}}

{{note_baseline}}

### 第 13 步 —— `10-optimize-prompt.sh`  ·  _Phase 5_
闭合 ADLC 循环。依据 Phase 4 的诊断（`SQC↓ RQC↑ GR↓` / `RP↓` → 提示词问题），它会：
1. 从 `pack/prompts/optimization-candidate.md` 装上本场景优化后的 System Prompt（和
   [`pack/prompts/baseline.md`](pack/prompts/baseline.md) 对比——候选提示词加上了**抗幻觉约束**），
2. 在已部署的 Harness 上就地更新 System Prompt（`update-harness`，约 30 秒，不重新部署；Memory、工具和技能保持 04/05 的设置），
3. 用**同样的 {{total_questions}} 个 practice 问题**重问一遍，每个问题一个新的优化运行 actor，
4. 重新评估 {{probe_count}} 条新的检索 trace（`09-run-eval.sh --eval-only {{probe_count}}`），L1 表会与基线运行对比。

```bash
./10-optimize-prompt.sh
```

{{contrast_paragraph}}

脚本最后会打印它自己对这个对比的解读：

{{closing_lines}}

{{retrieval_contrast}}

> [!NOTE]
> 优化后的提示词要用知识库文档的语言来写：提示词语言和知识库对齐后，抗幻觉约束才能最有效地落地。LLM-as-judge 的分数在
> 不同次运行间会波动——看趋势和诊断方向，别盯单一绝对值。讲师会用 `13-judge-stability.sh` 测量裁判噪声带；落在裁判噪声带里的
> 变化不算提升；如果 09 和 10 打分的检索 trace 数量不同，均值差只作描述。

{{note_optimize}}

---

## 9. 你应该看到什么

本场景的教学时刻，以及每个时刻应该呈现什么。数字来自评估器；阈值是 workshop 固定的标准。

{{expectations_table}}

{{noise_band_note}}

---

## 10. 可选实验

下面三个是核心约 2 小时主线之外的**可选延伸**。它们复用你已部署好的 Agent 和评估器，所以**要在 `99-cleanup.sh` 之前跑**
——一旦清理，这些资源就没了。

### `11-cost-latency.sh` —— 运营指标（成本与延迟）  ·  _Phase 6_
decision-first agent 开篇承诺了三个维度：**答得好 / 答得快 / 省人力**。THELMA 已经量化了「答得好」，这个脚本补上另外两个
——而且**不新建任何资源**。它从 CloudWatch `aws/spans`（与 `09-run-eval.sh` 同一个 log group）读出你**已经产出的那批
trace**，对**最近 {{recent_n}} 条检索 trace** 算出**端到端延迟**（最大 span 结束时间 − 最小 span 开始时间）、
**输入/输出 token**（取自 `gen_ai.usage.*` span 属性，在 span 层级里只计一次）和**成本**（token 数 × 模型单价）。最终就是
给 CXO 的那张记分牌：质量（GR）+ 速度（延迟）+ 成本（$）并排放。

```bash
./11-cost-latency.sh            # 算最近 {{recent_n}} 条含检索 trace 的延迟 + token + 成本
./11-cost-latency.sh <trace-id> # 只算一条 trace
```

> [!TIP]
> 单价（`PRICE_IN` / `PRICE_OUT`，$/1M token）默认使用 `us-west-2` 下 Nova 2 Lite、Nova Pro 和 Claude Haiku 4.5 的
> 已核实价格快照。其他模型或区域，脚本会以 `Set verified PRICE_IN and PRICE_OUT …` 停止：请以 AWS 官网定价为准导出这两个值。

{{note_cost_latency}}

### 可选实验 A —— `12-compare-models.sh`（多模型对比）
回答每个 CXO 都会问的一句：*「能不能换个更便宜/更快的模型，质量还过得去？」* 它会**非破坏性地**在已部署的 Harness 上
就地切换到对比模型（`update-harness`，约 30 秒，不重新部署）、用同样的 {{total_questions}} 个 practice 问题重问（每个问题一个新的对比运行 actor）、用同一个 THELMA 打分、
和 Phase 4 基线对比质量/成本/延迟，然后**还原回基线模型**。把「换模型」从拍脑袋变成看数据。

```bash
./12-compare-models.sh                          # 默认对比模型 = Nova Pro
./12-compare-models.sh us.amazon.nova-pro-v1:0  # 等价默认值
./12-compare-models.sh us.anthropic.claude-haiku-4-5-20251001-v1:0  # 想试别家也可
```

> [!WARNING]
> 它是在**已部署的 Harness 上**切换模型、退出时再切回——`harness.json` 保持基线模型，也**不会**重跑 `04-deploy.sh`
>（那会 `rm -rf {{agent_name}}`、把评估器一起删掉）。对比模型别用 **Nova Micro** 这一档：在 Strands 严格 ToolUse 协议下它经常报
> `Model produced invalid sequence as part of ToolUse`，对话全失败、拿不到数据。这种不稳本身是个有用的评估结论——
> *该模型与你当前的 Agent 拓扑不兼容*——但不适合放在 demo 首发。

{{note_models}}

### 可选实验 B —— `13-judge-stability.sh`（裁判稳定性）
回答 CXO 必问的后续：*「你这个 AI 裁判（THELMA）自己靠谱吗？会不会瞎打分？」* 这是一个轻量的**可重复性**检验：对
**同一条 trace** 连打 N 次、看分数分布——分数稳定说明裁判可信；分数忽上忽下说明结论要谨慎对待（小模型尤其容易这样）。
默认取最近一条含检索的 trace，在 10 或 12 之后就是最后问的那个问题：{{stability_case}}。

```bash
./13-judge-stability.sh                # 取最近一条含检索 trace，打 {{stability_runs}} 次
./13-judge-stability.sh <trace-id> [N] # 指定 trace，打 N 次（默认 {{stability_runs}}）
```

> [!NOTE]
> 脚本按标准差判定：≤ {{stable_std}} 为 `稳定`，≤ {{moderate_std}} 为 `一般`，否则为 `不稳`；可用分数少于两个会打印
> `数据不足`。裁判默认用 **Nova 2 Lite**（小模型），分数抖动可能偏大——这正是这个实验要暴露的。讲师会把这个抖动换算成
> 第 9 节的裁判噪声带。可重复性只是一项轻量检验；生产推荐的并行做法是**人工样本校准**（拿标注集算 TPR/TNR）。

{{note_judge_stability}}

{{student_experiments}}

---

## 11. 清理 —— `99-cleanup.sh`

> [!CAUTION]
> **只有讲师让你做时才运行它。** 它会删除本场景的整个 Workshop 环境，以免持续计费（知识库、Lambda、NAT 网关等）。

```bash
./99-cleanup.sh
```

脚本会拆掉 Agent `{{agent_name}}`、Gateway `{{gateway_name}}`、Lambda `{{lambda_name}}`、知识库 `{{kb_name}}`、
`{{ssm_prefix}}` 下的 SSM 参数、`~/workshop/eval-runs/{{agent_name}}/` 下的运行记录、Customizer 附加栈（在
`workshop-infra` 之前），最后是基础栈。如果 AgentCore 服务托管的 ENI 仍然挂着，它会打印 `Cleanup pending` 并以状态 75
退出：它们最长可能要 8 小时才释放——稍后重跑同一条命令即可。资源还保留着时它绝不报告成功。脚本是幂等的——重跑是安全的。

{{note_cleanup}}

---

## 12. 数据来源与署名

{{attribution_block}}

两个自定义评估器（THELMA、Mind the Goal）是对已发表研究方法的独立复现——引用和许可见
[`evaluators/README.md`](evaluators/README.md)。文中引用的模型作为托管的 Amazon Bedrock 模型调用，不包含也不分发任何模型权重。

---

## 安全

更多信息见 `aws-samples/sample-eval-first-building-enterprise-agents-with-agentcore` 在提交 `{{template_commit_short}}`
的 CONTRIBUTING。

## 许可

本库基于 MIT-0 许可。见 [LICENSE](LICENSE) 文件。
{{supplement}}
