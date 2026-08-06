"""模块 1:用户提问(入口)。

接收原始自然语言问题;user_id 与 data_scope(可访问业务线)由调用方注入到 state,
从这一步开始就写入 state,后续权限过滤与敏感判定直接读取,不再重新查权限。
"""

from __future__ import annotations

import time

from nl2sql_agent.state import NL2SQLState


def make_entry_node(deps):  # noqa: ARG001 - 入口节点无需服务,保持签名统一
    def entry_node(state: NL2SQLState) -> NL2SQLState | dict:
        if not state.user_id:
            raise ValueError("user_id 必须提供")
        if not state.data_scope:
            raise ValueError("data_scope 必须提供(用户可访问的业务线列表)")
        query = (state.user_query or "").strip()
        if not query:
            raise ValueError("user_query 不能为空")
        trace_id = state.trace_id or f"trace-{int(time.time() * 1000)}"
        # 权限信息已存在于 state,入口只做校验与规范化,不重新查权限
        return {"user_query": query, "trace_id": trace_id}

    return entry_node
