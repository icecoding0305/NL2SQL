# https://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanhttps://bailian.console.aliyun.com/xiyanNL2SQL 智能体(LangGraph)

面向企业内部数据分析场景(多系统、字段量大、对生成准确性和权限安全要求较高)的
自然语言 → SQL 智能体。使用类型化语义图、Schema Grounding 与统一计划链路执行;
检索后按置信度路由(模块 3.5)让"术语库之外的新指标"也能被处理。

## 架构总览

```
模块1 用户提问(注入 user_id / data_scope)
  → 模块2 问题理解与改写(SemanticGraph,比较/状态/存在/布尔结构)
  → 模块3 Schema检索(复合术语 + 字段召回 + 锚点/实体/桥接规划)
  → 模块3.5 检索置信度路由
       ├─ 同一业务槽位多字段 → 口径澄清(interrupt,用户选业务字段)
       ├─ 物理表近分 → 系统自动规划,不要求用户选表
       ├─ 低置信 → 低置信提示(interrupt,是否继续)
       └─ 高置信 → 放行
  → 模块5b 所有查询生成计划 → 模块6 语义覆盖/字段/关系校验 ─(不过)→ 回 5b(上限 max_plan_retries=2)
                                                          └─(通过)→ 模块7 SQL生成 → 模块8 静态校验
  → 模块9 风险三态判定 → 模块10 沙箱执行 → 模块11 结果解释
模块9:pass → 执行；approval_required → 人工确认；hard_block → 直接结束
模块3.5 澄清 → 人工确认(interrupt_before) → 通过才继续
```

**两条重试回路的边界(硬约束):**
- 计划校验失败(模块 6)只在模块 5b 内部打转,不退回模块 3(Schema 检索)
- 执行报错(模块 10)只退回模块 7(SQL 生成),不退回模块 5b(计划)
- 每种错误在离它最近的节点被消化,不允许一次报错把整条链路从头推倒

## 技术栈

- Python 3.11+ / LangGraph(状态图 + SQLite 持久化 checkpointer + interrupt 人工确认)
- Pydantic v2(state 与结构化输出) / sqlglot(SQL 解析、AST 校验、多方言)
- 真语义 embedding(sentence-transformers,本地模型)+ 可插拔向量后端(memory / pgvector)
- LLM:Anthropic / DeepSeek(OpenAI 兼容)/ 任意 OpenAI 兼容端点,按节点可配不同模型
- 数据库:MySQL / Postgres(按 `DATABASE_URL` 自动选择执行器)
- 前端:React + TypeScript + antd,WebSocket 流式

## 目录结构

```
nl2sql_agent/
  state.py                 # NL2SQLState / SchemaHit / 强类型 QueryPlan(pydantic v2)
  graph.py                 # StateGraph 编排 + interrupt_before
  main.py                  # FastAPI 入口(WebSocket /ws/query + REST /api/*)
  api.py                   # WebSocket 流式事件 + REST(查询/审批/恢复/历史/反馈/配置)
  testing.py               # FakeLLM / InMemoryExecutor 等测试双打
  nodes/
    m1_entry.py            # 模块1 用户提问(注入身份与权限)
    m2_query_resolution.py    # 模块2 问题理解、改写与 SemanticGraph
    m3_schema_retrieval.py    # 模块3 Schema检索(术语 + 向量补充)
    m3_5_retrieval_confidence_router.py  # 模块3.5 置信度路由(候选/低置信澄清)
    m5b_plan_generation.py / m6_plan_validation.py  # 所有查询统一计划与语义覆盖校验
    m7_sql_generation.py / m8_static_validation.py / m9_sensitive_check.py
    m10_sandbox_execution.py / m11_result_interpretation.py / human_review.py
  config/
    term_mapping/_global.yaml   # 全局术语映射
    settings.yaml               # 运行时参数与 M-Schema 自动定位配置
    clarification_rules.yaml    # 澄清/置信度/补充关联表阈值
    business_predicates.yaml / sensitive_rules.yaml / settings.yaml / few_shot.yaml
    schema_ingest.yaml          # 表结构入库质量规则
    model_config.yaml           # embedding + 各节点模型
    vector_store.yaml           # 向量后端显式配置(memory/pgvector)
  services/
    embedding/router.py         # embedding 适配层(统一 embed 签名)
    vector_store/               # base(适配器) / memory / pg
    schema_ingest/              # MySQL/Postgres 提取器 / profiler / M-Schema /
                                # 分阶段描述生成 / 审核 / 增量同步 / 多层向量文本
    llm.py / semantic_parser.py / term_mapping.py / schema_catalog.py / schema_importer.py / checkpoint.py
    sql_dialect.py / executor.py / few_shot_store.py / query_store.py
    config_loader.py / deps.py
  eval/                        # regression_set.yaml + run_eval.py + 检索/指标基准
  tests/                       # pytest:节点级 + 端到端路由(87 用例)
scripts/
  ingest_schema.py             # 表结构入库(full/incremental)
  review_schema_comments.py    # 注释人工审核 CLI
  seed_data.py                 # 生成 ~1000 条真实感业务数据
  dev.ps1                      # 一键启停后端+前端
sql/                           # 真实 Hive 表定义 + mysql_*.sql 转换建表脚本
web/                           # React + TS + antd 前端
data/                          # 运行数据(SQLite 历史/审核/快照)
.env                           # 本地配置(LLM key、DATABASE_URL、模型)
```

## 关键设计

- **QueryPlan 结构上不可表达非 SELECT 操作**:没有 `operation` 字段；连接、过滤和指标分别使用
  `JoinSpec` / `FilterSpec` / `MetricSpec` 强类型，连接类型、过滤运算符和置信度都有类型约束。
- **Query M-Schema + LogicalPlan**:检索结果会先投影为本次查询所需的最小 Schema（字段、关系、基数、
  语义绑定），再把兼容层 QueryPlan 转换为 `Scan / Filter / Join / SemiJoin / AntiJoin /
  Aggregate / Project / Sort / Limit` 关系算子 DAG。计划显式记录最终输出字段与每行结果粒度，
  校验器会拦截聚合粒度不一致以及实体查询中的一对多普通 JOIN 放大风险。
- **低延迟执行路径**:规划和 SQL 翻译只携带 Query M-Schema，不再重复发送完整宽表 Schema；
  表/字段/关系检索复用同一查询向量，并在服务启动时预热本地 Embedding 和磁盘索引。
  高置信通用问题使用确定性语义解析；包含显式输出字段的计划直接编译为 sqlglot AST，
  仅在计划表达不完整时回退 SQL 模型。明细列表使用确定性摘要，避免无价值的结果润色调用。
- **业务线按系统维度**:`data_scope` 是用户可访问的**系统命名空间**(如 `risk_mart` 风险数据集市 /
  `dw` 数仓 / `core` 网贷核心),入口注入后下游只读。表按系统分组,**系统级隔离**,
  `PLATFORM_CODE`(平台)是表内普通数据维度,绝不从 `data_scope` 推导。行级平台过滤默认关闭；
  如需启用，由服务端鉴权层通过独立的 `row_level_filters` 注入真实平台编码。
- **字段驱动 Schema 规划**:普通问题先抽取度量、过滤、实体、属性和分组槽位，再按字段短语、
  向量、数据类型、语义角色和实体亲和度评分；随后确定核心事实表、实体/维度表，并从
  M-Schema 关系图补充最短路径桥接表。复合术语继续使用审核口径，普通字段不要求逐项维护指标映射。
- **角色化澄清(模块 3.5)**:事实表、实体表和桥接表是互补角色，不再作为多选一候选；只有
  同一业务槽位存在多个高证据近分字段时才询问字段口径。低证据结果继续走低置信提示。
- **字段幻觉拦截**:模块 8 用 sqlglot AST 交叉比对(不用正则),解析表别名与 SELECT 别名;
  危险操作(非 SELECT)硬失败不重试;行级过滤注入按别名限定。
- **复合口径分离**:模块 5b 生成结构化 QueryPlan(非自由文本),模块 6 与术语映射交叉校验口径。
- **风险三态决策**:证件/姓名/金额聚合/低置信等可审批风险输出 `approval_required` 并暂停；
  超扫描阈值等硬风险输出 `hard_block` 直接结束；其余输出 `pass`。
  当前 `settings.yaml` 中 `approval.enabled: false`，软风险仅记录原因并直接执行，不进入人工审批；
  `hard_block` 仍然生效。恢复审批时把该开关改为 `true` 即可。
- **有反馈的局部重试**:计划或 SQL 重试时会带入上一轮产物和校验/执行错误，要求模型只修错、不改变原查询语义。
- **结构语义确定性归并**:当正向 `exists` 的全部高影响子条件已经进入计划，且相应 Join 或同表记录过滤存在时，
  服务端自动把父级存在原子绑定到物理操作并重算覆盖集合，避免仅因模型漏写父 atom_id 重试；`not_exists` 仍必须显式规划反连接。
- **会话内多轮**:前端维护同一会话最近几轮已完成对话的 <问题,最终答案>，作为 `conversation_history`
  传给后端；m5b/m7 的提示词注入 `<CONVERSATION_DATA>` 上文，使追问能理解"那""这个"等指代。
- **空结果是合法结果**:数据库返回 0 行时直接进入结果解释，不通过重写 SQL 放宽条件。
- **中断状态持久化**:生产入口使用 SQLite checkpointer，服务重启后仍可恢复待审批/待澄清流程。
- **配置热更新**:所有规则经 `ConfigLoader` 按 mtime 缓存,改配置无需重启服务。
- **LLM 可插拔、按节点**:`build_llm()`(主模型,计划/解释)、`build_sql_llm()`(SQL 专用,
  可指向任意 OpenAI 兼容端点如千问)、`get_model_for_node()`(离线任务如注释生成)。
  model 名一律从环境变量/配置读取,不硬编码。
  模型统一由 `config/model_config.yaml` 的 `runtime` 配置。默认开启 `unified: true`，
  问题理解、计划生成、结果解释、离线 Schema 描述以及 SQL 生成兜底均继承同一个模型。
  切换模型只需修改 provider、model、base_url、api_key_env 和 supports_tool_calling；
  API Key 本身仍保存在 `.env` 对应的环境变量中，不得写入 YAML 或代码。
- **数据库可插拔**:按 `DATABASE_URL` scheme 选 MySQL / Postgres 执行器,方言随 sqlglot 切换。

## 快速开始(uv)

```bash
cd d:\code\NL2SQL
uv sync --extra dev          # 安装依赖
copy .env.example .env       # 配置 LLM key / DATABASE_URL(见 .env.example)

# 测试与回归(无需 LLM/DB,测试双打)
uv run pytest
uv run python -m nl2sql_agent.eval.run_eval

# 生产模式(真实 LLM + 真实数据库)
uv run uvicorn nl2sql_agent.main:app --port 8000

# 演示模式(无外部依赖,FakeLLM + InMemoryExecutor)
# 在 .env 设 NL2SQL_DEMO=1 后同上启动
```

前后端一键启停:`powershell -File scripts/dev.ps1 start|stop|restart|status`
(前端 http://localhost:5173,后端 API http://localhost:8000/docs)

## 数据接入

### 1. 建表
把业务表的 Hive/MySQL 定义建到库中(`sql/mysql_*.sql` 为转换后的建表脚本)。

### 2. 导入表结构 → 向量索引

```bash
# 全量入库:注释齐全的表直接写入向量库;注释缺失的表进审核队列
uv run python scripts/ingest_schema.py --mode full --datasource risk_mart --business-line risk_mart

# 每日增量(配合定时任务)
uv run python scripts/ingest_schema.py --mode incremental --datasource risk_mart
```

- **扩展元数据**:按 MySQL/Postgres 方言提取 PK、FK、唯一键、索引、默认值、可空性和原始类型。
- **轻量字段画像**:变化表每表只执行一次受限采样，生成空值率、近似基数、范围/长度和脱敏样例；
  规则优先分类为 code/enum/datetime/text/numeric 与 dimension/measure。
- **质量检查**:除表注释长度与覆盖率外，默认要求每个字段都有有效描述；“字段/数据/相关字段”等
  泛化注释不能通过。未达标 → 不入库，进入审核队列。
- **XiYan 式分阶段描述**:数据库理解 → 表初步理解 → 同类字段辨析 → 宽表分批生成字段描述
  → 根据字段反向生成表描述；生成结果经过字段存在性、敏感泄漏、重复描述和长度校验。
- **LLM 候选安全**:样例值先按敏感字段脱敏(身份证打码)，候选带证据、校验问题与置信度；
  不经人工审核不会成为有效描述或进入向量库。
- **人工审核**:`scripts/review_schema_comments.py list/show/approve/reject`
  通过后写入系统覆盖层(schema_metadata_override),**不改数据库 DDL**;后续入库用覆盖注释
- **增量同步**:按结构 hash 只处理变化的表;被删的表从向量库清理(无幽灵表)
- **Enterprise M-Schema**:每次同步生成 `data/schema/{datasource}/m-schema.json`，并保存
  `raw-m-schema.json` / `effective-m-schema.json` / `manifest.json` 内容寻址快照。
- **多层索引**:由审核生效的 M-Schema 派生表级、按语义类别分组的字段级、外键关系级向量文档。

M-Schema 是唯一运行时 Schema 事实源，向量库是它的派生检索视图：

```text
数据库 → raw M-Schema → 画像/分类/描述 → 审核 Override → effective M-Schema
                                                    └→ 表/字段/关系向量索引
```

运行 `scripts/ingest_schema.py` 时会落盘 effective M-Schema。运行时根据
`DATABASE_URL` 的数据库名自动定位 `data/schema/<database>/m-schema.json`，并直接
构建内存 SchemaCatalog；不再生成或依赖 `schema_catalog.yaml`。表级、字段级和
关系级向量文档同样只从 effective M-Schema 生成，并携带 `snapshot_id`、
`semantic_hash` 和表级语义哈希。

模块 3 对复合指标保留术语精确映射；普通问题使用字段级证据构建 `QueryIntent` 和
`SchemaPlan`。SchemaPlan 明确 `primary_fact/secondary_fact/entity/dimension/bridge`
角色，并只采用审核生效关系构造最小连通子图。无法结构化的问题回退原表级/字段级
向量链路，融合权重与阈值统一在 `clarification_rules.yaml` 配置。

召回层还支持：领域短语查询扩展、跨表字段集合覆盖、按查询特征动态调整表/字段
权重、相对候选差距，以及候选表之间最多 3 跳的最短 FK 路径补全。宽表的表级文本
按 PK/FK/指标/时间字段选择核心字段，并额外保留全部字段名。内存后端会把向量缓存到
M-Schema 同目录的 `vector-cache.json`；只有 M-Schema 语义哈希和 Embedding 配置签名
都一致时才复用，否则自动重新生成。

领域 Embedding 不直接凭经验替换，可运行
`python -m nl2sql_agent.eval.run_schema_retrieval_benchmark` 测试默认模型，或用
`--model-path <本地模型目录>` 对候选模型运行同一组金融 Schema Recall@K 用例。

`nl2sql_agent.eval.schema_metrics.evaluate_schema_cases` 可用于对比 legacy/xiyan 快照，统一计算
表 Recall@K、字段召回率、Join 路径正确率、SQL Execution Accuracy、澄清率、人工修改率及
每表画像/LLM 成本。

### 3. 生成演示数据(可选)

```bash
uv run python scripts/seed_data.py --count 1000   # 6 张表插入 ~1000 条真实感关联数据
```

### 4. 新系统接入
数仓/网贷核心等新系统:建表 → 导入(`--business-line dw` / `--business-line core`)。
表自动归入对应系统命名空间,只有 `data_scope` 含该系统的用户能检索。

## LLM 统一模型配置

模型路由只修改 `nl2sql_agent/config/model_config.yaml`：

```yaml
runtime:
  unified: true
  provider: deepseek
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  supports_tool_calling: false
```

`.env` 只保存密钥和数据库连接，不再保存模型名称或模型路由：

```env
DEEPSEEK_API_KEY=sk-xxx
# 数据库(按 scheme 自动选执行器)
DATABASE_URL=mysql://user:pass@host:port/db
```

离线任务模型(如注释生成)在 `config/model_config.yaml` 的 `nodes` 下配置。

## 语义检索与向量存储

- **Embedding**:`config/model_config.yaml`。`provider: local` 用 sentence-transformers
  (默认 `paraphrase-multilingual-MiniLM-L12-v2`);huggingface 不可达时从 ModelScope 下载后配 `model_path`。
  `provider: fake` 仅测试。
- **向量后端**:`config/vector_store.yaml` 显式 `backend: memory / pgvector`。
  MySQL 环境用 `memory`(重启后需重新 ingest);切换 embedding 后必须全量重建索引,新旧向量不能混用。
- **检索两层**:术语映射精确命中 → 向量语义兜底 + 补充关联表(阈值在 `clarification_rules.yaml`)。

## 前端(React + antd)

```bash
cd web && npm install && npm run dev   # http://localhost:5173
```

页面:
- **数据问答**:对话流 + 分阶段步骤卡片(检索/计划/SQL/审批/结果),WebSocket 流式推进,
  运行中显示当前步骤加载指示;候选/低置信/字段口径澄清在页面内点选;断线重连与页面切换后
  通过 localStorage 活动会话 + `GET /api/query/{trace_id}` 恢复进度;同一对话内连续追问支持多轮上下文。
- **表与注释**:按系统浏览表结构,前端直接补充/修改表或字段注释(写入覆盖层,不改 DDL);
  查看并处理 LLM 生成的待审核注释(通过/驳回/重新入库)。
- **审批队列**:待审批敏感查询 + 完整 pipeline 详情,通过/驳回(驳回必须填原因,写入反馈闭环)
- **历史与审计**:按用户/系统/时间筛选,完整 trace(各节点耗时、重试、审批人),CSV 导出
- **配置管理**:术语映射可视化编辑(需 `ADMIN_TOKEN`,热更新)

界面采用低饱和莫兰迪配色(米白底 + 紫灰主色),语义色(信息/成功/警告/错误)统一映射到主题色板。

## API 契约

WebSocket `/api/ws/query`:连接后发送 `{"query","user_id","data_scope","trace_id?"}`,
服务端按 `node_start / node_complete / retry / interrupt / final / error` 流式推送节点事件(节点名与后端一致)。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/query` | 非流式提交查询,返回 trace_id + 结果/待审批状态 |
| GET | `/api/query/{trace_id}` | 断线重连/刷新后恢复当前阶段 |
| POST | `/api/query/{trace_id}/approve` | 审批 `{approved, reason, approver}` |
| POST | `/api/query/{trace_id}/resume` | 澄清恢复 `{resume: {table} / {continue}}` |
| GET | `/api/approvals` | 待审批队列 |
| GET | `/api/history` | 历史查询(筛选 user_id / business_line / 日期) |
| GET | `/api/audit/{trace_id}` | 完整 trace 详情(含反馈) |
| POST | `/api/feedback` | 反馈闭环 `{trace_id, node, feedback_type, comment}` |
| GET/PUT | `/api/config/term-mapping` | 术语映射读写(PUT 需 `X-Admin-Token`) |
| GET | `/api/config/rules` | 规则配置查看 |

## 测试与验收

87 个 pytest 用例覆盖:

| 验收项 | 测试 |
| --- | --- |
| 原单表简单查询也生成并校验 QueryPlan | `test_acceptance_1_simple_path_executes` |
| 复合口径正确路由 5b,metric_logic 与术语定义一致并通过模块 6 | `test_acceptance_2_composite_metric_plan_path` |
| 引用不存在字段被模块 8 拦截并退回模块 7 重试 | `test_acceptance_3_hallucinated_field_retries_sql` |
| 系统隔离:仅可访问系统的表被检索 | `test_acceptance_4_system_isolation` |
| 敏感查询在 human_review 暂停,不自动执行 | `test_acceptance_5_sensitive_pauses_for_human` |
| 模块 10 执行报错退回模块 7 而非模块 5b | `test_acceptance_6_execution_error_retries_sql_generation` |
| 计划失败只在计划路径内打转,不退回模块 3 | `test_plan_retry_loop_stays_inside_plan_path` |
| 新指标不被模块 2 拒答,走到检索/3.5 判定 | `test_acceptance_1_new_metric_not_blocked_at_module2` |
| 多相近候选 → 候选澄清,选定后不重复触发 | `test_acceptance_2/3_multi_candidate_*` |
| 低置信 → 继续 → 强制计划路径 + 强制人工确认 | `test_acceptance_4_low_confidence_*` |
| 表结构入库:质量门禁/审核/override 重入库/脱敏/增量/删表清理 | `tests/test_schema_ingest.py` |
| 重试反馈/空结果/风险三态/强类型计划/重启恢复 | `tests/test_architecture_hardening.py` |

## 预留扩展点(暂不实现,接口已留)

- **反馈闭环**:人工确认通过的案例回流 few-shot —— `FewShotStore.add_example` + `Deps.feedback_sink`
- **语义缓存**:相同语义复用已校验 SQL —— `Deps.semantic_cache`
- **结果多模态输出**:`execution_result` 保留原始数据,可扩展图表/导出
- **跨会话用户偏好**:`conversation_history` / `user_id` 已在 state,可扩展持久化
