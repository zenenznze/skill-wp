export const TASK_ID_RE = /^\d{8}-[a-z0-9][a-z0-9-]*$/;
export type TaskStatus = "pending" | "running" | "review" | "blocked" | "failed" | "accepted";
export type ReviewVerdict = "PASS" | "RETRY" | "BLOCKED";

export interface AtomicWorkContract {
  task_protocol_version: 2;
  task_id: string;
  repo_id: string;
  single_outcome: string;
  deliverables: string[];
  write_scope: string[];
  read_only: boolean;
  acceptance: string[];
  resume_boundary: string;
  max_attempts: number;
}
export interface TaskRecord {
  schema: "wp-task-v2";
  contract: AtomicWorkContract;
  status: TaskStatus;
  attempt: number;
  created_at: string;
  updated_at: string;
  contract_digest: string;
}
export interface ResourceRecord {
  schema: "wp-resources-v1";
  task_id: string;
  workspace_id?: string;
  tab_id: string;
  pane_id?: string;
  agent_id?: string;
  session_path?: string;
  adopted: boolean;
  /** Deterministic ownership proof. Missing/false records are never auto-closed. */
  created_by_wp?: boolean;
  attempt?: number;
  lifecycle?: "active" | "retired" | "close_pending" | "closed" | "preserved";
  cleanup_error?: string;
  bound_at: string;
}
export interface TaskResult {
  schema?: "wp-result-v2";
  task_id: string;
  attempt?: number;
  status: "success" | "blocked" | "failed";
  summary: string;
  changed_files: string[];
  validation: Array<{ command: string; outcome?: "passed" | "failed" | "skipped"; result?: "passed" | "failed" | "not_run"; evidence?: string }>;
  blocker?: string | Record<string, unknown> | null;
  handoff_path: string;
}
export interface Review {
  schema: "wp-review-v1";
  task_id: string;
  attempt: number;
  verdict: ReviewVerdict;
  summary: string;
  findings: string[];
  reviewed_at: string;
}
export interface Receipt {
  schema: "wp-receipt-v1";
  operation: string;
  task_id?: string;
  ok: boolean;
  timestamp: string;
  paths?: string[];
  details?: Record<string, unknown>;
}

export function assertContract(value: unknown): asserts value is AtomicWorkContract {
  if (!value || typeof value !== "object") throw new Error("contract must be an object");
  const c = value as Record<string, unknown>;
  if (c.task_protocol_version !== 2) throw new Error("task_protocol_version must be 2");
  if (typeof c.task_id !== "string" || !TASK_ID_RE.test(c.task_id)) throw new Error("invalid task_id");
  for (const key of ["repo_id", "single_outcome", "resume_boundary"] as const)
    if (typeof c[key] !== "string" || !(c[key] as string).trim()) throw new Error(`${key} is required`);
  for (const key of ["deliverables", "write_scope", "acceptance"] as const)
    if (!Array.isArray(c[key]) || !(c[key] as unknown[]).every(x => typeof x === "string" && x.trim())) throw new Error(`${key} must contain non-empty strings`);
  if (!(c.deliverables as unknown[]).length || !(c.acceptance as unknown[]).length) throw new Error("deliverables and acceptance cannot be empty");
  if (typeof c.read_only !== "boolean") throw new Error("read_only must be boolean");
  if (c.read_only !== ((c.write_scope as unknown[]).length === 0)) throw new Error("read_only must exactly match an empty write_scope");
  if (!Number.isInteger(c.max_attempts) || (c.max_attempts as number) < 1 || (c.max_attempts as number) > 10) throw new Error("max_attempts must be 1..10");
  for (const item of c.write_scope as string[]) {
    if (item.startsWith("/") || /^[A-Za-z]:[\\/]/.test(item) || item.split(/[\\/]/).includes("..")) throw new Error(`unsafe write_scope: ${item}`);
  }
}

export function assertResult(value: unknown, task: TaskRecord): asserts value is TaskResult {
  if (!value || typeof value !== "object") throw new Error("result must be an object");
  const r = value as Record<string, unknown>;
  if (r.schema !== undefined && r.schema !== "wp-result-v2") throw new Error("unsupported result schema");
  if (r.task_id !== task.contract.task_id || (r.attempt !== undefined && r.attempt !== task.attempt)) throw new Error("result identity mismatch");
  if (!["success", "blocked", "failed"].includes(String(r.status))) throw new Error("invalid result status");
  if (typeof r.summary !== "string" || !r.summary.trim()) throw new Error("result summary is required");
  if (!Array.isArray(r.changed_files) || !Array.isArray(r.validation)) throw new Error("invalid result evidence");
  if (r.status === "success" && !(r.validation as unknown[]).length) throw new Error("successful result requires validation evidence");
  if (r.status === "blocked" && (r.blocker === null || r.blocker === undefined || (typeof r.blocker === "string" && !r.blocker.trim()))) throw new Error("blocked result requires blocker");
  if (typeof r.handoff_path !== "string" || !r.handoff_path.endsWith("HANDOFF.md")) throw new Error("invalid handoff_path");
}
