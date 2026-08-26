"""模块 6:计划校验(拦截业务逻辑错误的关键卡点)。

对照 Schema 检索结果与术语映射表做交叉核对:
- target_tables 和引用字段是否都在 retrieved_schema 范围内
- metric_logic 若引用术语映射表里的复合口径,定义是否与映射表一致

校验不过把具体错误写入 plan_validation_errors,退回模块 5b 重试;
达到上限(max_plan_retries,比 SQL 重试更严格,默认 2)直接结束,提示人工介入。
"""

from __future__ import annotations

from typing import Any

from nl2sql_agent.state import NL2SQLState, QueryPlan, SchemaPlan
from nl2sql_agent.services.semantic_parser import required_atom_ids, semantic_atom_map
from nl2sql_agent.services.logical_planner import validate_logical_plan
from nl2sql_agent.services.schema_planner import output_binding_fields
from nl2sql_agent.services.term_mapping import TermResolutionStatus


def _iter_referenced_fields(plan: QueryPlan):
    for f in plan.filters:
        yield f"{f.table}.{f.column}" if f.table else f.column
    for f in plan.having:
        yield f"{f.table}.{f.column}" if f.table else f.column
    for col in plan.group_by:
        yield str(col)
    if plan.metric_logic:
        yield from plan.metric_logic.columns
    for output in plan.output_fields:
        if output.table and output.column:
            yield f"{output.table}.{output.column}"
    for order in plan.order_by:
        if order.table and order.column:
            yield f"{order.table}.{order.column}"
    yield from plan.output_grain.keys


def _validate_field_ref(
    field: str,
    table_cols: dict[str, set[str]],
    target_tables: set[str],
) -> str | None:
    """校验字段归属；多表同名字段必须显式限定表名。"""
    if not field:
        return None
    if "." in field:
        table, column = field.rsplit(".", 1)
        if table not in target_tables:
            return f"字段 {field} 引用了计划外表 {table}"
        if column not in table_cols.get(table, set()):
            return f"表 {table} 不存在字段 {column}"
        return None
    owners = [table for table in target_tables if field in table_cols.get(table, set())]
    if not owners:
        return f"引用了不存在的字段 {field}"
    if len(owners) > 1:
        return f"字段 {field} 同时存在于多张目标表 {sorted(owners)},必须限定表名"
    return None


def validate_plan(
    plan: QueryPlan,
    retrieved_schema,
    term_mapping,
    data_scope: list[str],
    schema_plan: SchemaPlan | None = None,
    semantic_graph=None,
    semantic_bindings: dict | None = None,
    output_bindings: dict | None = None,
    semantic_coverage: dict | None = None,
) -> list[str]:
    errors: list[str] = []
    known_tables = {h.table_name for h in retrieved_schema}
    table_cols = {h.table_name: {c["name"] for c in h.columns} for h in retrieved_schema}
    target_tables = set(plan.target_tables)

    uncovered = list((semantic_coverage or {}).get("uncovered_mentions") or [])
    if uncovered:
        errors.append(f"原问题仍有高影响内容未被语义契约覆盖：{uncovered}")

    if semantic_graph is not None and semantic_graph.query_action == "aggregate":
        if not any(field.aggregation for field in plan.output_fields):
            errors.append("用户要求统计/汇总，但 QueryPlan 没有任何聚合输出")
        if semantic_graph.group_by and not plan.group_by:
            errors.append("统计问题已声明业务分组粒度，但 QueryPlan 缺少 GROUP BY")

    # 0. 所有高影响语义原子必须显式进入计划，禁止静默漏掉用户条件。
    required_atoms = required_atom_ids(semantic_graph)
    implemented_atoms: set[str] = set()
    for item in plan.filters:
        implemented_atoms.update(item.source_atom_ids)
    for item in plan.having:
        implemented_atoms.update(item.source_atom_ids)
    for item in plan.join_logic:
        implemented_atoms.update(item.source_atom_ids)
    if plan.metric_logic:
        implemented_atoms.update(plan.metric_logic.source_atom_ids)
    declared_atoms = set(plan.covered_atom_ids)
    missing_atoms = required_atoms - implemented_atoms
    unknown_atoms = implemented_atoms - required_atoms
    if missing_atoms:
        errors.append(f"QueryPlan 遗漏高影响语义条件 {sorted(missing_atoms)}")
    if unknown_atoms:
        errors.append(f"QueryPlan 声明了不存在的语义条件 {sorted(unknown_atoms)}")
    if declared_atoms != implemented_atoms:
        errors.append(
            "covered_atom_ids 必须与实际 filter/join/metric 的 source_atom_ids 完全一致: "
            f"covered={sorted(declared_atoms)}, implemented={sorted(implemented_atoms)}"
        )
    atom_map = semantic_atom_map(semantic_graph)
    semantic_bindings = semantic_bindings or {}
    for filter_spec in [*plan.filters, *plan.having]:
        if semantic_graph is not None and not filter_spec.source_atom_ids:
            errors.append(
                f"过滤条件 {filter_spec.table or '?'}.{filter_spec.column} "
                "没有用户语义或可信规则来源，禁止静默增加条件"
            )
        for atom_id in filter_spec.source_atom_ids:
            atom = atom_map.get(atom_id)
            binding = semantic_bindings.get(atom_id)
            # Schema 绑定可能基于字段值域对原始值进行规范化（上海 → 上海市）。
            # 此时绑定是语义在物理库中的最终实现，不能再同时要求原始字面值。
            if (
                not binding
                and atom
                and atom.predicate_type in {"comparison", "aggregate_comparison", "status"}
            ):
                if filter_spec.operator != atom.operator or filter_spec.value != atom.value:
                    errors.append(
                        f"语义条件 {atom_id} 的操作符/值与计划不一致: "
                        f"语义={atom.operator} {atom.value!r}, "
                        f"计划={filter_spec.operator} {filter_spec.value!r}"
                    )
            if binding and (
                filter_spec.table != binding.get("table_name")
                or filter_spec.column != binding.get("column_name")
                or filter_spec.operator != binding.get("operator")
                or filter_spec.value != binding.get("value")
            ):
                errors.append(
                    f"语义条件 {atom_id} 未采用已确认的 Schema 绑定: "
                    f"期望={binding.get('table_name')}.{binding.get('column_name')} "
                    f"{binding.get('operator')} {binding.get('value')!r}"
                )

    if any(item.aggregation for item in plan.filters):
        errors.append("普通 WHERE filters 不得包含聚合表达式，聚合条件必须放入 having")
    if any(not item.aggregation for item in plan.having):
        errors.append("HAVING 条件必须声明 aggregation")
    atom_types = {
        atom_id: atom.predicate_type for atom_id, atom in atom_map.items()
    }
    for item in plan.filters:
        if any(atom_types.get(atom_id) == "aggregate_comparison" for atom_id in item.source_atom_ids):
            errors.append("aggregate_comparison 被错误放入 WHERE，必须使用 HAVING")
    for item in plan.having:
        if any(atom_types.get(atom_id) != "aggregate_comparison" for atom_id in item.source_atom_ids):
            errors.append("HAVING 引用了非聚合比较语义条件")

    # 0.5 所有明确返回要求必须进入 output_fields，不能用实体主键替代属性。
    required_outputs = {
        output.id for output in (semantic_graph.outputs if semantic_graph else [])
        if output.required
    }
    implemented_outputs = {
        output_id
        for field in plan.output_fields
        for output_id in field.source_output_ids
    }
    declared_outputs = set(plan.covered_output_ids)
    missing_outputs = required_outputs - implemented_outputs
    unknown_outputs = implemented_outputs - required_outputs
    if missing_outputs:
        errors.append(f"QueryPlan 遗漏用户明确要求的返回内容 {sorted(missing_outputs)}")
    if unknown_outputs:
        errors.append(f"QueryPlan 声明了不存在的返回要求 {sorted(unknown_outputs)}")
    if declared_outputs != implemented_outputs:
        errors.append(
            "covered_output_ids 必须与 output_fields.source_output_ids 完全一致: "
            f"covered={sorted(declared_outputs)}, implemented={sorted(implemented_outputs)}"
        )
    output_bindings = output_bindings or {}
    for output_id in required_outputs:
        if output_id not in output_bindings:
            errors.append(f"返回要求 {output_id} 尚未完成 Schema 字段绑定")
            continue
        expected_fields = {
            (item.get("table_name"), item.get("column_name"))
            for item in output_binding_fields(output_bindings[output_id])
        }
        implemented_fields = {
            (field.table, field.column)
            for field in plan.output_fields
            if output_id in field.source_output_ids
        }
        missing_fields = expected_fields - implemented_fields
        unexpected_fields = implemented_fields - expected_fields
        if missing_fields:
            errors.append(
                f"返回要求 {output_id} 遗漏已确认字段 "
                f"{sorted(f'{table}.{column}' for table, column in missing_fields)}"
            )
        if unexpected_fields:
            errors.append(
                f"返回要求 {output_id} 使用了绑定外字段 "
                f"{sorted(f'{table}.{column}' for table, column in unexpected_fields)}"
            )
        if output_bindings[output_id].get("binding_mode") == "expanded":
            labels = {
                (item.get("table_name"), item.get("column_name")): item.get("label")
                for item in output_binding_fields(output_bindings[output_id])
            }
            for field in plan.output_fields:
                key = (field.table, field.column)
                if (
                    output_id in field.source_output_ids
                    and key in labels
                    and field.alias != labels[key]
                ):
                    errors.append(
                        f"展开返回字段 {field.table}.{field.column} 必须使用业务别名 "
                        f"{labels[key]!r}"
                    )

        semantic_output = next((
            item for item in (semantic_graph.outputs if semantic_graph else [])
            if item.id == output_id
        ), None)
        expected_aggregation = (
            semantic_output.aggregation if semantic_output is not None
            else output_bindings[output_id].get("aggregation")
        )
        for field in plan.output_fields:
            if output_id in field.source_output_ids and field.aggregation != expected_aggregation:
                errors.append(
                    f"返回要求 {output_id} 聚合方式不一致："
                    f"期望={expected_aggregation!r}，计划={field.aggregation!r}"
                )

    if semantic_graph is not None:
        if semantic_graph.limit != plan.limit:
            errors.append(
                f"用户要求的 TopN/LIMIT 未完整实现："
                f"期望={semantic_graph.limit!r}，计划={plan.limit!r}"
            )
        if len(semantic_graph.order_by) != len(plan.order_by):
            errors.append("用户要求的排序数量与 QueryPlan 不一致")
        else:
            for expected, actual in zip(semantic_graph.order_by, plan.order_by):
                if expected.direction != actual.direction:
                    errors.append(
                        f"排序方向不一致：{expected.concept} 期望 {expected.direction}，"
                        f"计划为 {actual.direction}"
                    )
        if semantic_graph.group_by:
            if plan.output_grain.level != "aggregate" or not plan.group_by:
                errors.append("用户要求按维度统计，但 QueryPlan 缺少分组聚合")

    aggregate_outputs = [field for field in plan.output_fields if field.aggregation]
    detail_outputs = [
        field for field in plan.output_fields
        if not field.aggregation and field.table and field.column
    ]
    group_refs = set(plan.group_by)
    if aggregate_outputs and detail_outputs:
        missing_group_fields = {
            f"{field.table}.{field.column}" for field in detail_outputs
            if f"{field.table}.{field.column}" not in group_refs
            and field.column not in group_refs
        }
        if missing_group_fields:
            errors.append(
                "聚合输出与非聚合输出混用时，非聚合字段必须进入 GROUP BY: "
                f"{sorted(missing_group_fields)}"
            )

    grain_keys = set(plan.output_grain.keys)
    if plan.output_grain.level in {"entity", "record", "aggregate"} and grain_keys:
        returned_refs = {
            f"{field.table}.{field.column}" for field in detail_outputs
        } | {field.column for field in detail_outputs if field.column}
        missing_returned_keys = {
            key for key in grain_keys
            if key not in returned_refs and key.rsplit(".", 1)[-1] not in returned_refs
        }
        if missing_returned_keys:
            errors.append(
                "结果粒度必须返回用于区分每行数据的键字段: "
                f"{sorted(missing_returned_keys)}"
            )
        if plan.output_grain.level in {"entity", "aggregate"} and aggregate_outputs:
            missing_group_keys = {
                key for key in grain_keys
                if key not in group_refs and key.rsplit(".", 1)[-1] not in group_refs
            }
            if missing_group_keys:
                errors.append(
                    "实体/聚合粒度必须按粒度键 GROUP BY: "
                    f"{sorted(missing_group_keys)}"
                )

    # Aggregating measures from multiple fact/detail tables after a flat JOIN can
    # multiply amounts.  Only the deterministic pre-aggregation compiler may
    # handle these plans, and it currently requires one shared grain with no
    # unscoped row filters.
    aggregate_tables = {
        field.table for field in plan.output_fields
        if field.aggregation and field.table
    }
    if len(aggregate_tables) > 1:
        if len(plan.group_by) != 1 or "." not in plan.group_by[0]:
            errors.append("多事实指标必须声明一个明确的共同分组粒度，禁止直接 JOIN 后聚合")
        if plan.group_by:
            group_table, group_column = plan.group_by[0].rsplit(".", 1)
            dimensions = [field for field in plan.output_fields if not field.aggregation]
            if not dimensions or any(
                field.table != group_table or field.column != group_column
                for field in dimensions
            ):
                errors.append("多事实指标的非聚合输出必须与共同分组字段一致")

    # 1. target_tables 必须都在检索结果内
    for t in plan.target_tables:
        if t not in known_tables:
            errors.append(f"target_tables 中的表 {t} 不在检索到的 schema 内")

    # 2. join 引用的表
    for j in plan.join_logic:
        for tbl in (j.left_table, j.right_table):
            if tbl not in known_tables:
                errors.append(f"join 引用了未检索到的表 {tbl}")
            elif tbl not in target_tables:
                errors.append(f"join 引用了 target_tables 未声明的表 {tbl}")
        if j.left_column not in table_cols.get(j.left_table, set()):
            errors.append(f"join 左表 {j.left_table} 不存在字段 {j.left_column}")
        if j.right_column not in table_cols.get(j.right_table, set()):
            errors.append(f"join 右表 {j.right_table} 不存在字段 {j.right_column}")

        if schema_plan is not None:
            allowed = any(
                (
                    relation.get("source_table") == j.left_table
                    and relation.get("target_table") == j.right_table
                    and j.left_column in relation.get("source_columns", [])
                    and j.right_column in relation.get("target_columns", [])
                ) or (
                    relation.get("source_table") == j.right_table
                    and relation.get("target_table") == j.left_table
                    and j.right_column in relation.get("source_columns", [])
                    and j.left_column in relation.get("target_columns", [])
                )
                for relation in schema_plan.relations
            )
            if not allowed:
                errors.append(
                    f"join {j.left_table}.{j.left_column} -> "
                    f"{j.right_table}.{j.right_column} 不在已规划关系子图中"
                )

    # 3. 引用字段必须存在于某个检索表
    for field in _iter_referenced_fields(plan):
        error = _validate_field_ref(field, table_cols, target_tables)
        if error:
            errors.append(error)

    # 4. SchemaPlan 中的事实、实体和桥接表必须完整落入 QueryPlan。
    if schema_plan is not None:
        required_tables = {
            item.table_name
            for item in [
                *schema_plan.anchor_tables,
                *schema_plan.dimension_tables,
                *schema_plan.bridge_tables,
            ]
        }
        missing = required_tables - target_tables
        if missing:
            errors.append(f"QueryPlan 缺少 SchemaPlan 必需表 {sorted(missing)}")

    # 5. metric_logic 的复合口径必须与术语映射一致
    ml = plan.metric_logic
    if ml and ml.metric_name:
        res = term_mapping.resolve(ml.metric_name, data_scope)
        if res.status == TermResolutionStatus.FOUND:
            entry = res.entries[0]
            if entry.composite_metric and ml.definition != entry.definition:
                errors.append(
                    f"指标 {ml.metric_name} 的 definition 与术语映射不一致: "
                    f"计划={ml.definition!r}, 映射={entry.definition!r}"
                )

    return errors


def make_plan_validation_node(deps):
    def plan_validation_node(state: NL2SQLState) -> NL2SQLState | dict:
        if state.query_plan is None:
            # 上一轮结构化解析失败(query_plan 为空),视为计划未通过
            errors = [*(state.plan_validation_errors or []), "计划未生成,重新生成"]
        else:
            errors = validate_plan(
                state.query_plan,
                state.retrieved_schema,
                deps.term_mapping,
                state.data_scope,
                state.schema_plan,
                state.semantic_graph,
                state.semantic_bindings,
                state.output_bindings,
                state.semantic_coverage,
            )
            if state.logical_plan is None:
                errors.append("LogicalPlan 未生成")
            elif state.query_mschema is None:
                errors.append("Query M-Schema 未生成")
            else:
                errors.extend(validate_logical_plan(state.logical_plan, state.query_mschema))

        # retry_count 只统计失败后的重试，不把首次成功校验计为一次重试。
        non_retryable = state.plan_generation_error_kind in {
            "output_truncated", "empty_response", "provider_non_retryable",
            "schema_context_incomplete",
        }
        new_count = (
            state.max_plan_retries
            if errors and non_retryable
            else state.plan_retry_count + (1 if errors else 0)
        )
        out: dict[str, Any] = {
            "plan_validation_errors": errors,
            "plan_retry_count": new_count,
            "query_candidates": [
                candidate.model_copy(update={
                    "status": "rejected" if errors else "validated",
                    "validation_errors": list(errors),
                })
                if candidate.stage == "plan" and candidate.selected else candidate
                for candidate in state.query_candidates
            ],
        }
        if errors and new_count >= state.max_plan_retries:
            out["terminal_status"] = "error"
            if state.plan_generation_error_kind == "output_truncated":
                headline = "模型生成查询计划时输出被截断，未能生成可执行 SQL。"
            elif state.plan_generation_error_kind == "empty_response":
                headline = "模型没有返回有效的查询计划正文，未能生成可执行 SQL。"
            elif state.plan_generation_error_kind == "generation_error":
                headline = "模型服务或结构化计划生成失败，未能生成可执行 SQL。"
            elif state.plan_generation_error_kind == "provider_non_retryable":
                headline = "模型服务拒绝了查询计划请求，请检查模型配置、权限或账户状态。"
            elif state.plan_generation_error_kind == "schema_context_incomplete":
                headline = "当前问题所需字段或可信关联不完整，系统已停止生成，避免猜测 SQL。"
            else:
                headline = "查询计划未通过完整性校验，未能生成可执行 SQL。"
            out["final_answer"] = headline + "\n" + "；".join(errors[:5])
        return out

    return plan_validation_node
