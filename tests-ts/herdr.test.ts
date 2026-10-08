import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HerdrClient } from "../src/herdr/client.js";
import { supervise } from "../src/core/supervisor.js";
import { createTask } from "../src/core/controller.js";
import { taskDir } from "../src/core/state.js";

test("tab creation explicitly selects workspace and preserves default arguments", async () => {
  const client = new HerdrClient("/repo");
  const calls: string[][] = [];
  client.call = async args => { calls.push(args); return { tab_id: "w16:t7", pane_id: "w16:p7" }; };
  assert.equal((await client.createTab("worker", "w16")).paneId, "w16:p7");
  await client.createTab("worker");
  const defaults = ["tab", "create", "--cwd", "/repo", "--label", "worker", "--no-focus"];
  assert.deepEqual(calls, [[...defaults, "--workspace", "w16"], defaults]);
});

test("supervisor routes new resources to controller workspace", async () => {
  const root = mkdtempSync(join(tmpdir(), "wp-workspace-"));
  execFileSync("git", ["init", "-q"], { cwd: root });
  const original = HerdrClient.prototype.call;
  const calls: string[][] = [];
  HerdrClient.prototype.call = async function(args) {
    calls.push(args);
    if (args[0] === "tab" && args[1] === "create")
      return { tab_id: "w16:t7", pane_id: "w16:p7" };
    return {};
  };
  try {
    createTask({ repo: root, task_id: "20260923-workspace", single_outcome: "Check routing",
      deliverables: ["report"], write_scope: [], read_only: true, acceptance: ["routing passes"],
      resume_boundary: "Read HANDOFF", max_attempts: 1 }, root);
    const dir = taskDir(root, "20260923-workspace", root);
    await supervise(root, dir, { controllerTabId: "w16:t1", waitTimeoutMs: 0 });
    assert.deepEqual(calls.find(args => args[1] === "create"),
      ["tab", "create", "--cwd", root, "--label", "WP-20260923-workspace-A1", "--no-focus", "--workspace", "w16"]);
    assert.ok(calls.some(args => args[0] === "agent" && args[1] === "start" && args.at(-1) === "w16:p7"));
    const resources = JSON.parse(readFileSync(join(dir, "resources.json"), "utf8"));
    assert.equal(resources.workspace_id, "w16");
    assert.equal(resources.tab_id, "w16:t7");
  } finally {
    HerdrClient.prototype.call = original;
    rmSync(root, { recursive: true, force: true });
  }
});
