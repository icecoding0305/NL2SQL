# NL2SQL 智能体

面向企业数据分析场景的自然语言转 SQL 系统。用户选择数据库并用中文提问，系统完成问题理解、Schema 召回、关系规划、查询计划生成、安全校验、只读执行和结果解释。

项目采用 FastAPI + LangGraph 后端与 React + Vite 前端，支持 MySQL、PostgreSQL、多数据库管理、企业知识治理、M-Schema、向量检索、查询审计和召回评测。

> 本项目会连接模型服务和业务数据库。生产环境使用前，请配置只读数据库账号、访问控制和敏感字段规则。

## 功能概览

- 自然语言问数：聚合、筛选、分组、排序、Top-N、明细查询和多表关联。
- 多数据库管理：连接测试、默认库切换、Schema 同步及增量更新。
- Schema Grounding：表、字段、关系、术语和值的多通道召回。
- 企业知识管理：业务名词、同义表达、业务规则和优化案例。
- 关系治理：发现、审核和补充数据库中缺失的业务关系。
- 查询安全：只读 AST 校验、敏感规则、EXPLAIN 和沙箱执行。
- 流式交互：WebSocket 推送查询阶段、重试、澄清和最终结果。
- 系统管理：历史审计、模型调用记录和 Schema 召回评测。
- Excel 黄金集：上传 `.xlsx`，选择目标 Schema，支持整集或单条评测。

## 系统架构

```mermaid
flowchart LR
    UI[React / Vite] -->|REST + WebSocket| API[FastAPI]
    API --> GRAPH[LangGraph 查询流程]
    GRAPH --> LLM[DeepSeek / OpenAI 兼容模型]
    GRAPH --> KNOWLEDGE[企业知识与术语]
    GRAPH --> SCHEMA[M-Schema 与向量索引]
    GRAPH --> DB[(MySQL / PostgreSQL)]
    API --> SQLITE[(SQLite 配置、历史、检查点)]
```

一次查询的主要流程：

```text
访问鉴权
  → 问题理解与 SemanticGraph
  → 必要的业务澄清
  → Schema/字段/关系多路召回
  → 最小 SchemaPlan 与 Query M-Schema
  → QueryPlan / LogicalPlan
  → 确定性 SQL 编译或模型兜底
  → SQL AST 与敏感规则校验
  → EXPLAIN 和只读执行
  → 结果解释与审计落库
```

设计原则：

- M-Schema 是运行时 Schema 事实源，向量索引是其派生检索视图。
- 用户不需要选择物理表，系统根据字段覆盖和可信关系自动规划。
- 明确且可解释的默认假设不阻断查询，实质性业务歧义才请求澄清。
- SQL 中的表、字段和 Join 必须有 Schema 证据，禁止虚构。
- 普通 SQL 优先由强类型 QueryPlan 确定性编译。
- 查询计划、模型调用、重试、SQL 和执行结果均可审计。

更完整的设计说明见 [系统架构与流程](docs/系统架构与流程.md) 和 [业务流程](BUSINESS_FLOW.md)。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 后端 | Python 3.11+、FastAPI、LangGraph、Pydantic v2 |
| SQL | sqlglot、MySQL、PostgreSQL、只读执行器 |
| 模型 | DeepSeek、Anthropic、OpenAI 兼容接口 |
| 检索 | sentence-transformers、memory / pgvector |
| 持久化 | SQLite、LangGraph SQLite Checkpointer |
| 前端 | React 18、TypeScript、Vite、Ant Design |
| 工具链 | uv、pytest、npm |

推荐使用 Python 3.12 和 Node.js LTS。

## 快速开始

### 1. 安装运行时

确保以下命令可以在终端中使用：

```powershell
uv --version
python --version
node --version
npm --version
```

### 2. 安装依赖

在项目根目录执行：

```powershell
uv sync --extra dev

cd web
npm ci
cd ..
```

`uv sync` 会创建或更新 `.venv`。前端优先使用 `npm ci`，确保依赖与 `web/package-lock.json` 一致。

### 3. 配置环境变量

在项目根目录创建或维护 `.env`。不要提交真实密钥：

```env
# 模型服务
DEEPSEEK_API_KEY=replace-with-your-key

# Web 平台访问密码
PLATFORM_ACCESS_TOKEN=replace-with-a-strong-random-token

# 可选：管理操作使用独立密码；未配置时回退到平台访问密码
ADMIN_TOKEN=replace-with-an-admin-token

# 可选：数据库管理页面尚无记录时，用于初始化默认连接
DATABASE_URL=mysql://readonly_user:password@127.0.0.1:3306/database_name

# 可选：仅本地演示
NL2SQL_DEMO=0
```

模型路由配置位于 [model_config.yaml](nl2sql_agent/config/model_config.yaml)。密钥应通过环境变量提供，不要直接写入 YAML。

### 4. 启动项目

```powershell
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 start
```

服务地址：

- 前端：<http://localhost:5173>
- 后端：<http://localhost:8000>
- API 文档：<http://localhost:8000/docs>

常用命令：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 status
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 restart
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 stop
```

日志位置：

```text
logs/backend.log
logs/backend.err.log
logs/frontend.log
logs/frontend.err.log
```

也可以分别启动：

```powershell
uv run uvicorn nl2sql_agent.main:app --port 8000

cd web
npm run dev
```

## 首次使用

1. 使用 `.env` 中配置的平台访问密码进入系统。
2. 打开“数据源管理 → 数据库连接”。
3. 添加 MySQL 或 PostgreSQL 连接，建议使用只读账号。
4. 测试连接并设置默认数据库。
5. 执行 Schema 同步，生成 M-Schema 和向量索引。
6. 在“表与注释”检查自动生成的表、字段描述。
7. 数据库没有物理外键时，在“表关系配置”补充并审核关系。
8. 在“数据问答”选择数据库并提问。

每个数据库的 Schema 资产默认保存在：

```text
data/schema/<数据库或连接标识>/m-schema.json
data/schema/<数据库或连接标识>/vector-cache.json
```

## 前端导航

```text
数据问答
数据源管理
  ├─ 数据库连接
  ├─ 表与注释
  └─ 表关系配置
企业知识管理
  ├─ 知识概览
  ├─ 业务名词
  ├─ 同义表达
  ├─ 业务规则
  └─ 优化案例
系统管理
  ├─ 历史与审计
  └─ 召回评测
```

审批功能默认关闭。启用时还会显示“审批队列”，相关开关位于后端配置和前端构建环境。

## 企业知识与 Schema 治理

企业知识保存在 `data/nl2sql.db`，支持全局作用域和数据库专属作用域：

- 业务名词：将业务概念绑定到真实 `table.column`。
- 同义表达：维护等价词、简称、上下位关系和禁止替换。
- 业务规则：配置指标定义、结构化谓词和默认口径。
- 优化案例：沉淀可复用的查询结构和数据库专属案例。

Schema 同步流程：

```text
数据库元数据
  → raw M-Schema
  → 字段画像与描述生成
  → 审核覆盖
  → effective M-Schema
  → 表级、字段级、关系级向量索引
```

数据库中不存在外键时，人工审核的关系配置会与 M-Schema 关系合并，但不会修改数据库 DDL。

## 召回评测

进入“系统管理 → 召回评测”，选择数据库 Schema 后运行评测。

支持两种模式：

- 稳定基线：使用黄金集冻结的 QueryIntent，隔离评估 Schema Grounding。
- 在线影子：运行真实问题理解和 Schema 召回链路，在生成 SQL 前停止。

页面支持：

- 上传 `.xlsx` 黄金集并持久化；
- 下载 Excel 模板；
- 选择用于评测的数据库 Schema；
- 运行全部用例；
- 选择一条用例进行单条测试；
- 查看表召回、字段召回、Join 准确率、SchemaPlan 精确率和澄清准确率。

上传后的活动数据集保存在：

```text
data/evaluation/active_dataset.json
```

Excel 必填列：

| 列 | 说明 |
| --- | --- |
| `id` | 用例唯一标识 |
| `question` | 自然语言问题 |
| `expected_tables` | 期望表，JSON 数组 |

常用可选列包括 `query_intent`、`expected_columns`、`expected_joins`、`expected_plan_tables`、`forbidden_tables` 和 `expected_clarification`。数组和对象字段必须填写合法 JSON。

仓库内置评测集位于 [schema_golden_set.yaml](nl2sql_agent/eval/schema_golden_set.yaml)，在没有上传 Excel 时作为默认数据集。

命令行评测：

```powershell
uv run python -m nl2sql_agent.eval.run_schema_golden_eval
uv run python -m nl2sql_agent.eval.run_schema_retrieval_benchmark --strategy multipath
```

## 测试和构建

后端测试：

```powershell
uv run pytest
```

当前仓库包含 267 项自动化测试，覆盖问题理解、Schema 检索、输出契约、Join 规划、SQL 编译、安全校验、多数据库、查询取消和评测集导入等场景。

前端生产构建：

```powershell
cd web
npm run build
```

建议提交代码前至少执行：

```powershell
uv run pytest
cd web
npm run build
```

## 目录结构

```text
NL2SQL/
├─ nl2sql_agent/
│  ├─ main.py                 FastAPI 应用入口和访问鉴权
│  ├─ api.py                  REST / WebSocket API
│  ├─ graph.py                LangGraph 查询编排
│  ├─ state.py                SemanticGraph、QueryPlan 等类型
│  ├─ nodes/                  查询流程节点
│  ├─ services/               Schema、计划、SQL、知识和存储服务
│  ├─ config/                 模型、提示词和治理规则
│  ├─ eval/                   黄金集与离线评测工具
│  └─ tests/                  自动化测试
├─ web/                       React / Vite 前端
├─ scripts/                   启停、Schema 导入和维护脚本
├─ data/                      本地配置、历史、M-Schema 和向量缓存
├─ docs/                      架构、优化路线与迁移文档
├─ logs/                      开发环境运行日志
├─ pyproject.toml             Python 项目与依赖
└─ uv.lock                    Python 锁文件
```

核心服务：

- `semantic_parser.py` / `semantic_query.py`：确定性语义理解与 Top-N、聚合等结构修复。
- `m3_schema_retrieval.py`：生产 Schema 召回节点。
- `schema_planner.py`：字段排序、最小关系子图和 SchemaPlan。
- `plan_normalizer.py`：将绑定事实规范化为可执行计划。
- `logical_planner.py`：QueryPlan 到 LogicalPlan。
- `sql_compiler.py`：确定性 SQL 编译。
- `schema_evaluation.py`：Excel 黄金集和双模式评测。

## 主要 API

所有受保护的 `/api/*` 请求使用 `X-Platform-Token`。WebSocket 首帧使用 `platform_token`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| WebSocket | `/api/ws/query` | 提交查询并接收流式事件 |
| POST | `/api/query` | 非流式提交查询 |
| GET | `/api/query/{trace_id}` | 查询状态与断线恢复 |
| POST | `/api/query/{trace_id}/cancel` | 取消查询 |
| GET | `/api/history` | 查询历史 |
| GET | `/api/audit/{trace_id}` | 完整审计记录 |
| GET/POST | `/api/databases` | 数据库列表与新增连接 |
| POST | `/api/databases/{id}/sync-schema` | 同步 Schema 和索引 |
| GET/POST | `/api/databases/{id}/relations` | 查询和维护关系 |
| GET/POST | `/api/knowledge/items` | 查询和新增企业知识 |
| GET | `/api/schema-evaluation` | 获取评测状态 |
| POST | `/api/schema-evaluation/run` | 运行整集或单条评测 |
| POST | `/api/schema-evaluation/dataset` | 上传 Excel 评测集 |
| GET | `/api/schema-evaluation/template` | 下载 Excel 模板 |

完整接口和参数以运行中的 <http://localhost:8000/docs> 为准。

## 安全建议

- 业务数据库使用只读、最小权限账号。
- 为 `PLATFORM_ACCESS_TOKEN` 和 `ADMIN_TOKEN` 使用不同的随机强密码。
- `.env`、`data/`、日志和数据库快照不得提交到公共仓库。
- 定期轮换已经出现在终端、截图、IDE 或聊天上下文中的密钥。
- 生产环境应使用 HTTPS、反向代理和更完善的身份认证。
- 上线前检查 [sensitive_rules.yaml](nl2sql_agent/config/sensitive_rules.yaml) 与执行扫描阈值。

## 常见问题

### 端口没有启动

```powershell
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 status
Get-Content logs/backend.err.log -Tail 100
Get-Content logs/frontend.err.log -Tail 100
```

### `uv`、`npm` 不在 PATH

安装 uv、Python 3.12 和 Node.js LTS 后重新打开终端。也可以使用其绝对路径执行等价命令。

### 数据库连接成功但无法召回字段

确认已经同步 Schema，并检查：

- `data/schema/<标识>/m-schema.json` 是否存在；
- 表和字段注释是否准确；
- 无外键表之间是否配置了可信关系；
- 企业术语是否绑定到当前数据库的真实字段。

### 问题被要求澄清

系统只应在口径选择会显著改变答案时阻断。明确的“累计金额”“前 10”“按客户汇总”等表达会作为聚合、排序和分组处理；如果仍出现不合理澄清，可在“历史与审计”查看 `resolved_query`、`unresolved_business_slots` 和节点日志。

### Git 提交失败

检查本地身份配置：

```powershell
git config user.name
git config user.email
```

如为空，在当前仓库配置正确的提交身份后重试。

## 已知限制

- 复杂窗口函数、同比/环比和递归查询仍需要扩展强类型计划与编译器。
- Schema 注释和字段画像质量会直接影响宽泛问题的字段选择。
- 缺少外键或可信关系时，系统不会猜测高风险 Join。
- 在线影子评测可能调用模型，耗时和成本明显高于稳定基线。
- 当前开发脚本以 Windows PowerShell 为主。

后续工作见 [后续优化路线图](docs/后续优化路线图.md)。
