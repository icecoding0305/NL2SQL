# NL2SQL 智能体

面向企业数据分析场景的自然语言转 SQL 系统。项目使用 LangGraph 编排查询流程，以
M-Schema 作为数据库结构事实源，通过语义覆盖、Schema Grounding、统一 QueryPlan、
确定性 SQL 编译和多层校验降低字段遗漏、错误关联、聚合粒度错误与 Schema 幻觉。

系统支持 MySQL/PostgreSQL、多数据库切换、前端数据库连接管理、Schema 同步、关系配置、
多轮对话、流式进度、查询取消、历史审计和结果总结。

## 当前架构

```mermaid
flowchart TD
    U[用户提问并选择数据库] --> A[API 鉴权与查询持久化]
    A --> B[问题理解与 SemanticGraph]
    B --> C[原问题语义覆盖检查与保守修复]
    C --> D{必须由用户补充信息?}
    D -- 是 --> X[返回澄清说明]
    D -- 否 --> E[术语/表/字段/关系多通道检索]
    E --> F[字段排序与最小 SchemaPlan]
    F --> G[宽泛主题 Schema 驱动具体化]
    G --> H[条件绑定与输出字段绑定]
    H --> I[补全最短关系路径]
    I --> J[生成查询级 Query M-Schema]
    J --> K{字段及关系证据充分?}
    K -- 不支持的输出 --> Y[说明缺失字段,禁止虚构]
    K -- 低置信 --> Z[低置信确认]
    K -- 通过 --> L[生成强类型 QueryPlan]
    Z -- 继续 --> L
    L --> M[规范化 WHERE/HAVING/粒度/输出契约]
    M --> N[生成 LogicalPlan]
    N --> O{计划完整性校验}
    O -- 可重试 --> L
    O -- 通过 --> P[确定性 SQL 编译]
    P --> Q{编译器是否支持}
    Q -- 否 --> R[模型 SQL 兜底]
    Q -- 是 --> S[SQL AST 静态校验]
    R --> S
    S -- 普通错误可重试 --> P
    S -- 危险或重试耗尽 --> W[拦截/失败]
    S -- 通过 --> T[敏感与安全检查]
    T --> V[EXPLAIN + 只读沙箱执行]
    V -- 执行错误可重试 --> P
    V -- 成功或空结果 --> AA[确定性/模型结果总结]
```

核心边界：

- 所有查询都生成 QueryPlan，不再通过 SQL 复杂度分类绕过计划。
- 用户不选择物理表；事实表、实体表、维度表和桥接表由系统自动规划。
- 只有真正的业务口径或证据不足可能触发澄清，宽泛字段要求优先交给 Schema 具体化。
- 计划失败只回到计划生成；SQL 校验或执行失败只回到 SQL 生成，不重新执行整条链路。
- SQL 正常由 QueryPlan 确定性编译，只有编译器暂不支持的计划才调用 SQL 模型兜底。

## 查询执行流程

### 1. 请求入口与会话

前端提交 `user_query`、`user_id`、`database_id`、`conversation_id` 和最近的
`conversation_history`。后端校验平台访问凭证，创建 `trace_id`，保存查询记录并注册取消信号。

同一会话中的追问继续复用原 `conversation_id`，历史问题不会因为点击或追加提问而重新排序。

### 2. 问题理解与语义覆盖

模块 2 同时使用确定性解析和可配置模型，生成 `ResolvedQuery` 与 `SemanticGraph`：

- 实体、事件、指标、属性和维度；
- 比较、状态、存在、否定存在及布尔组合条件；
- 返回字段、聚合方式、分组、排序和数量限制；
- 查询动作：明细、查询、统计或排名。

`semantic_coverage` 对照原始问题检查高影响内容是否被语义图覆盖。模型不能删除用户明确提出的
筛选条件或返回要求。对于“基本信息”“贷款情况”“逾期情况”等宽泛主题，系统保留原始表达，
不在理解阶段武断地收缩为某一个指标。

### 3. Schema 检索与规划

模块 3 只读取当前选择数据库的 effective M-Schema，并组合以下证据：

1. 审核生效的复合术语精确映射；
2. 表级向量检索；
3. 字段级向量检索；
4. 关系级检索与最短 Join 路径；
5. 字段名称、注释、类型、角色、主外键和样例画像；
6. 过滤值与枚举/样例值的匹配证据。

系统生成最小 `SchemaPlan`，区分：

- `primary_fact`：主事实表；
- `secondary_fact`：其他指标来源表；
- `entity` / `dimension`：实体和维度表；
- `bridge`：仅用于连通关系的桥接表。

字段级命中可以独立产生表候选；关系图支持多跳路径。多个相近物理表不会展示给业务用户选择，
系统会根据查询粒度、字段覆盖和关系连通性自动组合。

### 4. 宽泛字段具体化

如果用户要求“客户基本信息”或“客户的逾期情况”，系统在完成 Schema 检索后，从查询相关的
有限字段集合中选择真实字段：

- “基本信息”可以展开为姓名、地址、电话等当前 Schema 实际存在的字段；
- “逾期情况”可以具体化为逾期本金余额等直接相关指标；
- 聚合问题会同时确定 `SUM`、`AVG`、`MAX` 或 `COUNT DISTINCT` 等计算方式；
- 敏感字段不会因为宽泛表达而被自动扩展，除非用户明确要求且安全策略允许；
- 模型选择无效时使用规则式 Schema 兜底，不虚构字段。

具体化结果写入 `ProjectionDecision`，随后物化为强制执行的 `SemanticOutput` 和
`OutputBindings`。

### 5. 查询级 M-Schema

检索完成后同时生成两份有边界的 `QueryMSchema`：

- `precision`：首次规划使用，只包含已确认的字段和关系；
- `recall`：仅在计划校验失败重试时启用，加入当前计划表内的高分候选字段，上限为每表 24 列。

两份视图都只包含本次问题需要的：

- 目标表和必要桥接表；
- 条件、输出、分组、排序和 Join 所需字段；
- 已验证关系；
- `SemanticBindings` 与 `OutputBindings`。

完整数据库 Schema 不会进入计划或 SQL 提示词。输出字段绑定完成后，如果新增字段位于其他表，
系统会再次扩展最小关系子图，避免“字段找到了但表没有加入计划”。

计划生成前还会按查询结构骨架检索 Few-shot。匹配依据包括查询动作、聚合、分组、排序、过滤和
Top-N 等结构特征；传给计划模型的只有 `question_skeleton` 与 `sql_structure`，不会传入示例 SQL、
示例表名或字段名。

### 6. QueryPlan 与 LogicalPlan

所有查询统一生成强类型 `QueryPlan`。主要结构包括：

- `target_tables`、`join_logic`；
- `output_fields` 与 `output_grain`；
- 行级 `filters`；
- 聚合级 `having`；
- `group_by`、`order_by`、`limit`；
- 指标定义和语义原子覆盖信息。

服务端规范化器以已确认的绑定事实为准修正模型计划：

- 输出字段必须覆盖全部 required semantic outputs；
- 行级条件进入 `WHERE`，聚合比较进入 `HAVING`；
- 分组实体优先使用不可空主键或唯一键；
- 多事实表指标先分别按统一粒度预聚合，再关联，避免明细 Join 导致金额重复放大；
- 同一物理字段允许生成多个表达式，例如 `SUM(LOAN_AMT)` 和 `AVG(LOAN_AMT)`。

随后生成由 `Scan / Filter / Join / SemiJoin / AntiJoin / Aggregate / Having /
Project / Sort / Limit` 构成的 `LogicalPlan`。

计划与 SQL 统一记录为 `QueryCandidate`，保存来源、使用的 Schema profile、校验状态、执行状态和
失败原因。当前在线链路仍只执行一个主候选，这一结构为后续按失败或低置信度条件触发多候选生成与
选择器预留稳定接口，不会在普通查询中额外调用模型。

### 7. 三层完整性校验

模块 6 在生成 SQL 前执行：

1. 原始问题与 SemanticGraph 的覆盖检查；
2. SemanticGraph 与 QueryPlan 的条件/输出原子覆盖检查；
3. QueryPlan 与 Query M-Schema 的物理表、字段、关系和粒度检查。

校验失败时只携带错误反馈重新生成 QueryPlan，达到 `max_plan_retries` 后终止，不会猜测字段继续执行。

### 8. SQL 编译与静态校验

模块 7 优先通过 `sql_compiler.py` 将 QueryPlan 确定性编译为 SQL。编译器支持普通查询、分组聚合、
HAVING、多表 Join 和多事实预聚合。只有不受支持的计划结构才回退到统一配置的 SQL 模型。

模块 8 使用 sqlglot AST 校验：

- 只允许只读查询；
- SQL 方言与语法合法；
- 表和字段必须存在于检索结果及 Query M-Schema；
- Join、别名和输出字段与计划一致；
- 禁止把 `data_scope` 错写为 `PLATFORM_CODE` 等业务字段的筛选值；
- 危险操作直接硬拦截，不进入重试。

### 9. 安全执行与结果说明

通过静态校验后，系统执行敏感规则检查。当前查询审批暂时关闭：

- `approval_required` 仅记录风险原因并继续；
- `hard_block` 始终有效；
- 将 `settings.yaml` 中 `approval.enabled` 改为 `true`，并在前端构建环境设置
  `VITE_APPROVAL_ENABLED=true`，可恢复查询人工审批和审批队列入口。

执行节点先运行 `EXPLAIN`，超过扫描阈值则拒绝执行；随后在只读事务中运行 SQL，并设置查询超时。
非聚合查询会强制添加结果行数上限。0 行是合法结果，不会自动放宽条件重写 SQL。

结果总结采用 `performance.result_summary_mode` 控制：明细查询通常使用确定性说明，聚合结果可调用
模型生成业务化总结。所有模型调用、耗时、Token、结构化校验和重试记录都会写入查询审计。

## M-Schema 与向量索引

M-Schema 是唯一运行时 Schema 事实源，向量存储是其派生检索视图：

```text
数据库
  → raw M-Schema
  → PK/FK/唯一键/索引/默认值/可空性提取
  → 字段画像与规则分类
  → 分阶段描述生成与质量校验
  → 审核 Override
  → effective M-Schema
  ├→ SchemaCatalog
  └→ 表级/字段级/关系级向量索引
```

每个数据库的结构文件位于：

```text
data/schema/<数据库或连接标识>/m-schema.json
```

系统不再生成或依赖 `schema_catalog.yaml`。Schema 同步时自动生成 M-Schema 和向量索引，用户不需要
手工执行额外的“生成 M-Schema”步骤。内存向量后端使用同目录的 `vector-cache.json` 缓存向量；
M-Schema 语义哈希或 Embedding 配置变化时自动重建。

Schema 检索支持 `legacy / multipath` 对照。`multipath` 复用问题理解阶段已经冻结的
`QueryIntent`，按主体、指标、属性、维度、过滤和值分别召回字段证据，再与整句表/字段向量、
精确术语和可信关系合并；不会增加额外 LLM 调用。每条槽位证据写入查询审计，便于定位错表或
漏字段。可在 `clarification_rules.yaml` 的 `retrieval_confidence.multipath.enabled` 紧急回退。

离线对比命令：

```powershell
python -m nl2sql_agent.eval.run_schema_retrieval_benchmark --strategy legacy
python -m nl2sql_agent.eval.run_schema_retrieval_benchmark --strategy multipath
```

评测集支持 `expected_tables`、`expected_columns`、`expected_joins` 和 `forbidden_tables`；正式调参前
应使用脱敏的真实业务问题扩充黄金集，不能只根据仓库内少量冒烟用例调整权重。

## 企业知识管理

左侧“企业知识管理”统一维护业务名词、同义表达、业务规则和优化案例。知识记录保存在
`data/nl2sql.db`，支持全局或数据库专属作用域、草稿/发布/停用状态、版本快照和优先级。

- 业务名词通过数据库 Schema 选择器绑定真实 `table.column`，发布时校验字段存在性；
- 同义表达区分等价、简称、上下位、相关和禁止替换，只有等价/简称进入自动改写；
- 业务规则以结构化谓词或指标定义进入问题理解，不依赖纯自然语言描述；
- 优化案例区分跨库计划骨架和数据库 SQL 降级案例，发布时校验 SQL 只读性及引用表；
- 数据库专属知识优先于全局知识，知识变更后对应查询依赖缓存自动失效。

首次访问知识管理接口时，系统会幂等迁移现有 `term_mapping/*.yaml`、
`business_predicates.yaml` 和 `few_shot.yaml`。旧 YAML 暂时保留作为兼容与备份源。

数据库没有物理外键时，可以在“表关系配置”页面补充经过人工确认的关系。运行时会将这些关系
覆盖与 M-Schema 关系合并，用于 Join 路径规划，但不会修改业务数据库 DDL。

## 多数据库管理

前端“数据源管理”提供：

- 新增和编辑 MySQL/PostgreSQL 连接；
- 测试连接；
- 设置默认数据库；
- 全量或增量同步 Schema；
- 查看表与字段注释；
- 配置表关系、基数和建议 Join 类型；
- 提问时选择目标数据库。

连接配置保存在 `data/nl2sql.db`。密码不会通过查询接口返回。首次启动且数据库配置表为空时，
系统可以从 `.env` 的 `DATABASE_URL` 初始化一条兼容连接；之后以数据库管理页面中的配置为准。

## 模型配置

在线查询模型统一由 [`nl2sql_agent/config/model_config.yaml`](nl2sql_agent/config/model_config.yaml)
管理。切换模型不需要修改代码：

```yaml
runtime:
  unified: true
  provider: deepseek
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  supports_tool_calling: false
```

密钥只保存在 `.env`：

```env
DEEPSEEK_API_KEY=your-key
PLATFORM_ACCESS_TOKEN=your-platform-access-token
ADMIN_TOKEN=your-admin-token

# 可选：仅用于首次初始化默认数据库连接
DATABASE_URL=mysql://user:password@host:3306/database
```

`runtime.unified: true` 时，主流程和 SQL 模型共享同一配置；`nodes` 可以覆盖单个节点的 Token 预算、
思考模式或独立模型。模型请求和结构化输出失败会记录在 `llm_calls`，便于区分模型延迟与数据库延迟。

## 技术栈

- Python 3.11+、FastAPI、LangGraph、SQLite Checkpointer；
- Pydantic v2、sqlglot；
- sentence-transformers、本地 Embedding；
- memory / pgvector 向量后端；
- MySQL / PostgreSQL 只读执行器；
- DeepSeek、Anthropic 或任意 OpenAI 兼容模型；
- React、TypeScript、Vite、Ant Design；
- WebSocket 流式查询事件。

## 目录结构

```text
nl2sql_agent/
  graph.py                     LangGraph 编排与重试路由
  state.py                     SemanticGraph、QueryPlan、LogicalPlan 等类型
  api.py                       查询、数据库、关系、Schema、历史 API
  nodes/
    m1_entry.py
    m2_query_resolution.py
    m3_schema_retrieval.py
    m3_5_retrieval_confidence_router.py
    m5b_plan_generation.py
    m6_plan_validation.py
    m7_sql_generation.py
    m8_static_validation.py
    m9_sensitive_check.py
    m10_sandbox_execution.py
    m11_result_interpretation.py
  services/
    semantic_coverage.py       原问题语义覆盖契约
    projection_resolver.py     宽泛返回字段具体化
    schema_planner.py          字段排序、SchemaPlan、关系补全
    logical_planner.py         QueryPlan → LogicalPlan
    plan_normalizer.py         绑定事实与计划规范化
    sql_compiler.py            确定性 SQL 编译和多事实预聚合
    value_grounding.py         文本值与字段值画像绑定
    schema_ingest/             Schema 提取、画像、描述、审核和增量同步
    vector_store/              memory / pgvector 适配器
  config/                      模型、规则、提示词和向量配置
  tests/                       单元、节点、路由和回归测试
web/                           React 前端
scripts/                       启停、Schema 同步和维护脚本
data/                          SQLite、M-Schema、向量缓存和快照
docs/                          业务流程与架构文档
```

## 快速开始

### 安装依赖

```powershell
cd D:\code\NL2SQL
uv sync --extra dev

cd web
npm install
```

### 配置

1. 在 `.env` 配置模型密钥和平台访问密码。
2. 在 `nl2sql_agent/config/model_config.yaml` 配置模型。
3. 启动系统后，在“数据库连接”页面新增连接、测试并同步 Schema。
4. 如果数据库没有外键，在“表关系配置”页面补充关系。

### 启动和停止

```powershell
powershell -File scripts/dev.ps1 start
powershell -File scripts/dev.ps1 status
powershell -File scripts/dev.ps1 restart
powershell -File scripts/dev.ps1 stop
```

- 前端：<http://localhost:5173>
- API 文档：<http://localhost:8000/docs>
- 后端日志：`logs/backend.log`、`logs/backend.err.log`
- 前端日志：`logs/frontend.log`、`logs/frontend.err.log`

也可以单独启动：

```powershell
uv run uvicorn nl2sql_agent.main:app --port 8000

cd web
npm run dev
```

## 前端功能

- **数据问答**：气泡式对话流、数据库选择、理解结果、计划、SQL、结果与停止查询；
- **会话历史**：同一会话追加问题、新建、删除、恢复运行状态；
- **数据库连接**：新增、编辑、删除、测试、设为默认和同步 Schema；
- **表与注释**：浏览 M-Schema 表字段并维护覆盖注释；
- **表关系配置**：维护无外键数据库的业务关系、基数和 Join 类型；
- **历史与审计**：查看节点耗时、模型调用、重试、SQL、执行结果和错误；
- **配置管理**：维护术语和查看规则配置；
- **审批队列**：仅在查询审批功能开启时显示。

前端只展示对用户有价值的合并阶段，不把每个内部节点都堆叠到对话中。后端仍保留完整 trace，
便于诊断和审计。

## 主要 API

所有 `/api/*` 请求使用 `X-Platform-Token`；WebSocket 首帧使用 `platform_token`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| WebSocket | `/api/ws/query` | 提交查询并接收流式节点事件 |
| POST | `/api/query` | 非流式提交查询 |
| GET | `/api/query/{trace_id}` | 查询状态或断线恢复 |
| POST | `/api/query/{trace_id}/cancel` | 停止正在执行的查询 |
| DELETE | `/api/query/{trace_id}` | 删除查询记录 |
| GET | `/api/conversations` | 会话列表 |
| GET | `/api/conversation/{conversation_id}` | 会话详情 |
| DELETE | `/api/conversation/{conversation_id}` | 删除会话及其查询 |
| GET | `/api/history` | 查询历史 |
| GET | `/api/audit/{trace_id}` | 完整审计记录 |
| GET/POST | `/api/databases` | 数据库连接列表/新增 |
| PUT/DELETE | `/api/databases/{id}` | 编辑/删除数据库连接 |
| POST | `/api/databases/{id}/test` | 测试连接 |
| POST | `/api/databases/{id}/default` | 设为默认数据库 |
| POST | `/api/databases/{id}/sync-schema` | 同步 Schema 并构建索引 |
| GET/POST | `/api/databases/{id}/relations` | 查询/新增关系配置 |
| PUT/DELETE | `/api/databases/{id}/relations/{relation_id}` | 编辑/删除关系 |
| GET | `/api/schema` | 浏览当前 Schema |
| GET/PUT | `/api/config/term-mapping` | 读取/修改术语映射 |

WebSocket 事件包括 `trace`、`node_start`、`node_complete`、`retry`、`interrupt`、`restore`、
`final`、`error` 和 `done`。

## 测试与评估

当前测试集包含 170+ 个用例：

```powershell
uv run pytest
uv run python -m nl2sql_agent.eval.run_eval
uv run python -m nl2sql_agent.eval.run_schema_retrieval_benchmark
uv run python -m nl2sql_agent.eval.run_schema_golden_eval --json-output logs/schema-golden-report.json
```

`schema_retrieval_benchmark` 用于隔离比较向量召回策略；`schema_golden_eval` 则直接运行生产
Schema 检索节点，覆盖术语治理、字段重排、关系补全、最小 `SchemaPlan` 与澄清决策。黄金样本位于
`nl2sql_agent/eval/schema_golden_set.yaml`，其中冻结的 `query_intent` 用于把问题理解误差与 Schema
Grounding 误差分开评估。该评测使用内存执行器，不连接或查询业务数据库。

重点覆盖：

- 原问题、SemanticGraph、QueryPlan 三层语义覆盖；
- 显式返回字段与宽泛字段具体化；
- 文本值归一化和字段绑定；
- 主键粒度、WHERE/HAVING 作用域；
- 多事实表预聚合；
- Schema 最小关系子图；
- 不存在字段和危险 SQL 拦截；
- 计划、SQL、执行的局部重试边界；
- 多数据库配置、关系配置和查询取消；
- Schema 增量同步、审核、脱敏和向量缓存；
- 节点耗时与 LLM 调用审计。

评估工具可以计算表 Recall@K、字段召回率、Join 路径正确率、SQL Execution Accuracy、
澄清触发率、人工修改率以及 Schema 画像和 LLM 成本。

## 当前限制与后续方向

- 复杂窗口函数、同比/环比和递归查询仍需要扩展强类型计划与确定性编译器；
- 宽泛业务主题的字段选择依赖 Schema 注释质量和字段画像；
- 无外键数据库必须维护可靠的关系事实，系统不会在缺少证据时猜测 Join；
- 在线模型已配置节点级超时和确定性降级；后续可继续增加结果总结异步化和已验证计划缓存；
- 语义缓存和已验证计划复用接口已经预留，尚未作为默认主链路启用。

详细实施记录见 [`docs/响应时间与提示词优化计划.md`](docs/响应时间与提示词优化计划.md)。

后续能力建设与实施顺序见 [`docs/后续优化路线图.md`](docs/后续优化路线图.md)。
