{{marker}}
{{instructor_banner}}
{{draft_banner}}
# 讲师指南：{{title}}

{{meta_table}}

学员用的 Guide 是 release 里的 `README.md`（副本在 `pack/labs/student-guide.md`）。本指南永远不会进入 release：
它只随 App 的讲师包分发。

## 目录

{{toc}}

---

## 0. 课前：就绪检查

### 来源

{{provenance_matrix}}

{{waived_line}}

{{provenance_banner}}

### 客户确认的锚点

{{anchors_table}}

{{anchors_note}}

{{classification_line}}

### 教学声明

{{declarations_table}}

### 校验器警告

{{warnings_list}}

### 彩排签核清单

{{checklist}}

---

## 1. 运行单

每个 Guide 步骤一行。App 的 Guided Run 通过固定的 SSM runner 跑从 `00-setup.sh` 到 `13-judge-stability.sh` 的
15 个步骤，并绑定到当前生效的 release；学员从 `~/workshop/current/static/scripts` 运行同样的脚本。

{{runsheet_table}}

---

## 2. 事实表

### 2.1 客户已确认的事实

可以作为该组织的真实规则讲授。

{{truth_customer_facts}}

### 2.2 合成教学设定

只能作为合成教学设定讲授，绝不能当作该组织的制度。

{{truth_synthetic_facts}}

### 2.3 知识库文档

{{truth_documents}}

{{noise_plan_text}}

### 2.4 工具

{{truth_tools}}

### 2.5 提示词

{{defects_table}}

{{prompt_diff}}

{{design_rationale}}

---

## 3. 教学对比

### 3.1 Memory（第一次对话）

{{memory_contrast}}

### 3.2 改提示词 vs 修检索

{{retrieval_contrast}}

如果课堂上没有复现：先核对 09 找到的检索 trace 数量是否为 {{probe_count}}；看 `quality.comparisonWarnings`
（样本数不同时均值差只作描述）；把变化和裁判噪声带比较；重跑一次 09/10。绝不要在课堂中途修改场景包——任何修改都需要新的
release、新的同步和新的彩排。

### 3.3 工具调用、拒绝与转交（L1 vs L2）

{{l1_contrast}}

这些问题由 L1 确定性地判定；当某项检查只能交出去时（没有标记的拒绝或转交、否定语境里的禁用词），由 Mind the Goal 的结论
解决。Mind the Goal 按场景规则评判，所以规则要求的拒绝或转交算成功，而不是 RCOF E2。

### 3.4 噪声、模型对比与裁判噪声

{{noise_contrast}}

`12-compare-models.sh` 用对比模型重问每个 practice 问题，然后还原基线模型；把质量、延迟和成本并排看。
`13-judge-stability.sh` 把最后一条检索 trace（{{stability_case}}）重复打 {{stability_runs}} 次；除非校准了
`evaluation.noiseBand`，报告按 max(2σ, 极差, 0.02) 推出裁判噪声带。裁判噪声带高于 {{band_max}} 时，由 THELMA 判定的对比都算
证据不足（`JUDGE_TOO_NOISY`）。

---

## 4. 答案表

### 4.1 Practice 问题

按 09 和 10 的提问顺序列出。

{{answer_practice}}

### 4.2 留出集问题

{{answer_holdout}}

### 4.3 如何使用留出集

{{holdout_use}}

---

## 5. 读懂结果

控制台输出的说明在学员 Guide 的第 12 步；这里重复这些表。

{{thelma_legend}}

{{diagnosis_table}}

{{diagnosis_levels}}

{{mtg_reading}}

{{rcof_table}}

{{l1_statuses}}

{{l1_compare}}

{{retry_messages}}

App 报告（`run/report.json`）读取的是本次运行所用 release 的构建快照：

{{report_fields}}

---

## 6. 结果不一致时

报告的 `teachingContrast` 会给每个问题一个代码。怎么说、怎么做：

{{facilitation_table}}

---

## 7. 排障

{{troubleshooting_table}}

---

## 8. 讨论实验

{{instructor_experiments}}

---

## 9. 彩排证据

{{rehearsal_block}}

---

## 10. 清理、回退与回滚

`99-cleanup.sh` 从不自动运行，也不属于 Guided Run；只在课后运行它（或让学员运行）。它会删除 `{{agent_name}}`、
`{{gateway_name}}`、`{{lambda_name}}`、`{{kb_name}}`、`{{ssm_prefix}}` 下的 SSM 参数、评估运行记录、
`workshop-customizer-addons` 栈，然后是 `workshop-infra`；退出状态 75 表示 AgentCore ENI 仍然挂着——稍后重跑。
课前要把改过的 pack 重新同步到这个环境（彩排后的新 release），改用 `./99-cleanup.sh --scenario-only`（或
`WORKSHOP_CLEANUP_SCENARIO_ONLY=1`）：它只删除本场景的资源和评估运行记录，保留 `workshop-infra` 和附加栈；然后
Preflight → Apply → Commit，并完整重置 Guided Run。
要回退，从 App 同步上一个 release（回滚会把 `~/workshop/current` 切回去）；回滚只替换 release 文件，永远不会撤销脚本
创建的 AWS 资源。

{{fallback_line}}

---

## 附录 A. 名称与过滤条件

{{names_table}}

09/10/11/13 的检索 trace 过滤条件：`{{retrieval_span}}`。运行记录：
`~/workshop/eval-runs/{{agent_name}}/<baseline|optimized|comparison>-<epoch>/`（`sessions.tsv`、`q<i>.out`、
`scores.tsv`、`l1.json`）。

## 附录 B. 实验观察

{{observations}}

## 附录 C. 讲师补充说明（场景作者）

{{supplement}}
