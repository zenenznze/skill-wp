import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export interface HerdrResourceState {
  reachable: boolean;
  tab?: Record<string, unknown>;
  agent?: Record<string, unknown>;
}

export function deepString(value: unknown, ...keys: string[]): string | undefined {
  if (!value || typeof value !== "object") return undefined;
  const object = value as Record<string, unknown>;
  for (const key of keys) if (typeof object[key] === "string") return object[key] as string;
  for (const child of Object.values(object)) {
    const found = deepString(child, ...keys);
    if (found) return found;
  }
  return undefined;
}

export class HerdrClient {
  constructor(readonly cwd: string) {}

  async call(args: string[]): Promise<Record<string, unknown>> {
    const { stdout } = await execFileAsync("herdr", args, {
      cwd: this.cwd,
      env: { ...process.env, AGENT_BRIEF_SUPPRESS: "1" },
      maxBuffer: 4 * 1024 * 1024,
    });
    const text=stdout.trim();
    if(!text)return {};
    try{
      const parsed=JSON.parse(text);
      if(parsed&&typeof parsed==="object")return parsed as Record<string,unknown>;
    }catch{}
    return {text};
  }

  async createTab(label: string, workspaceId?: string): Promise<{ tabId: string; paneId: string; raw: Record<string, unknown> }> {
    const args = ["tab", "create", "--cwd", this.cwd, "--label", label, "--no-focus"];
    if (workspaceId) args.push("--workspace", workspaceId);
    const raw = await this.call(args);
    const tabId = deepString(raw, "tab_id");
    const paneId = deepString(raw, "pane_id");
    if (!tabId || !paneId) throw new Error("Herdr did not return tab_id and pane_id");
    return { tabId, paneId, raw };
  }

  async startPi(name: string, paneId: string): Promise<{ agentId: string; sessionPath?: string; raw: Record<string, unknown> }> {
    const raw = await this.call(["agent", "start", name, "--kind", "pi", "--pane", paneId]);
    const agent = await this.call(["agent", "get", name]);
    return { agentId: name, sessionPath: deepString(agent, "value", "session_path"), raw: { ...raw, agent } };
  }

  async prompt(agentId: string, prompt: string): Promise<Record<string, unknown>> { return this.call(["agent", "prompt", agentId, prompt]); }
  async wait(agentId: string, timeoutMs: number): Promise<Record<string, unknown>> {
    return this.call(["agent", "wait", agentId, "--timeout", String(timeoutMs)]);
  }
  async read(agentId: string, lines = 400): Promise<Record<string, unknown>> {
    return this.call(["agent", "read", agentId, "--source", "recent-unwrapped", "--lines", String(lines)]);
  }
  async renameTab(tabId: string, label: string): Promise<Record<string, unknown>> { return this.call(["tab", "rename", tabId, label]); }
  async closeTab(tabId: string): Promise<Record<string, unknown>> { return this.call(["tab", "close", tabId]); }
  async probe(tabId?: string, agentId?: string): Promise<HerdrResourceState> {
    try {
      const tab = tabId ? await this.call(["tab", "get", tabId]) : undefined;
      const agent = agentId ? await this.call(["agent", "get", agentId]) : undefined;
      return { reachable: true, tab, agent };
    } catch { return { reachable: false }; }
  }
  async snapshot(): Promise<Record<string, unknown>> { return this.call(["api", "snapshot"]); }
}
