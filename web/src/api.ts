import type { PipelineEvent, QueryRecord } from "./types";

const ACCESS_TOKEN_KEY = "nl2sql-platform-token";

export const getAccessToken = () => localStorage.getItem(ACCESS_TOKEN_KEY) || "";
export const setAccessToken = (token: string) => localStorage.setItem(ACCESS_TOKEN_KEY, token);
export const clearAccessToken = () => localStorage.removeItem(ACCESS_TOKEN_KEY);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAccessToken();
  const resp = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Platform-Token": token } : {}),
      ...(init?.headers || {}),
    },
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail || `HTTP ${resp.status}`);
  }
  return resp.json() as Promise<T>;
}

export const apiGet = <T>(path: string) => request<T>(path);
export const apiPost = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
export const apiPut = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) });
export const apiDelete = <T>(path: string) => request<T>(path, { method: "DELETE" });

export interface QueryInput {
  user_query: string;
  user_id: string;
  data_scope: string[];
  database_id: string;
  trace_id?: string;
  conversation_id?: string;
  conversation_history?: { role: "user" | "assistant"; content: string }[];
}

export interface QueryController {
  close: () => void;
}

/** 建立 WebSocket 提交查询,onEvent 收到每个 pipeline 事件。 */
export function submitQuery(
  input: QueryInput,
  onEvent: (e: PipelineEvent) => void,
  onClose: () => void,
): QueryController {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws/query`);
  ws.onopen = () => ws.send(JSON.stringify({ ...input, platform_token: getAccessToken() }));
  ws.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as PipelineEvent);
    } catch {
      /* 忽略非法帧 */
    }
  };
  ws.onclose = onClose;
  ws.onerror = () => ws.close();
  return { close: () => ws.close() };
}
