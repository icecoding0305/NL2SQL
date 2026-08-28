# embadding 迁移与代码清洗清单

## 结论

`embadding/wenshu-nl2sql` 不再作为第二套运行系统。主项目是唯一运行入口；旧目录暂作
legacy reference，待迁移验收完成后归档或删除。

## 已由主项目承接

| 旧能力 | 主项目承接位置 |
| --- | --- |
| 数据库连接 | `web/src/pages/DatabasePage.tsx`、数据库 API |
| Schema 同步和 M-Schema | `services/schema_ingest/`、数据库同步 API |
| 表字段注释与审核 | `web/src/pages/SchemaPage.tsx` |
| 表关系治理 | `web/src/pages/RelationsPage.tsx`、`relation_store.py` |
| 术语、同义词、规则、案例 | `web/src/pages/KnowledgePage.tsx`、知识库服务 |
| 多路向量召回和选列 | `nodes/m3_schema_retrieval.py`、`schema_planner.py` |
| 黄金集评测 | `eval/schema_golden_set.yaml`、`SchemaEvaluationService` |
| 召回实验台 | `web/src/pages/EvaluationPage.tsx` |
| 运行历史与审计 | `web/src/pages/HistoryPage.tsx`、查询存储 |

## 不迁移的重复页面

- 旧连接配置、元数据同步、关系维护、静态知识库页面；
- 旧 FastAPI 静态站点和 8765 独立运行入口；
- 旧项目专用的 MySQL 元数据库/Qdrant 双写流程；
- 与当前 M-Schema、SQLite 配置库和向量适配器重复的存储实现。

## 删除旧目录前的验收门槛

1. 后端测试和前端构建持续通过；
2. 主页面可完成连接、同步、注释审核、关系和知识维护；
3. 召回评测页面可运行黄金集并展示失败证据；
4. 确认旧目录没有未迁移的私有黄金集、DDL 或人工业务知识；
5. 将需要长期保留的论文和架构文档移入主项目 `docs/references/`；
6. 创建一次可恢复的 Git 提交或压缩归档后，再删除旧目录。

## 后续结构清洗顺序

1. 将 `api.py` 按 query、database、knowledge、schema-evaluation 路由拆包；
2. 将评测数据模型从页面局部类型收敛到共享 API contract；
3. 合并 legacy 与 multipath 评测入口的公共加载和报告代码；
4. 清理已不再调用的旧向量兼容分支前，先通过黄金集做消融对照；
5. 最后归档 `embadding`，不在功能迁移期间直接删除。
