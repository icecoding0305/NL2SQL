import type { QueryRecord } from "../types";

/**
 * 活动会话持久化:当前对话/正在进行/待审批的会话 trace_id 存到 localStorage,
 * 页面切换或刷新后按 trace_id 从后端恢复进度(不重新发起查询)。
 */
const KEY = "nl2sql.activeSessions";

export function loadActiveSessions(): QueryRecord[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as QueryRecord[]) : [];
  } catch {
    return [];
  }
}

export function saveActiveSession(rec: QueryRecord): void {
  const list = loadActiveSessions().filter((x) => x.trace_id !== rec.trace_id);
  list.unshift(rec);
  try {
    localStorage.setItem(KEY, JSON.stringify(list.slice(0, 20)));
  } catch {
    /* 存储满或不可用时忽略 */
  }
}

export function removeActiveSession(traceId: string): void {
  const list = loadActiveSessions().filter((x) => x.trace_id !== traceId);
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch {
    /* ignore */
  }
}
