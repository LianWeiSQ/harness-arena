import { spawn, execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { setTimeout as delay } from "node:timers/promises";
import { resolve } from "node:path";

const files = ["smoke-provider-recovery-ui.mjs", "smoke-session-lifecycle-ui.mjs"];
const testProcess = `
const { spawn } = require("node:child_process");
const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
console.log(child.pid);
if (process.argv[1] === "running") setInterval(() => {}, 1000);
else child.unref();
`;
const alive = (pid) => {
  try {
    const state = readFileSync(`/proc/${pid}/stat`, "utf8").split(") ").at(-1)[0];
    return state !== "Z" && state !== "X";
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return false;
    throw error;
  }
};
const waitUntil = async (condition) => {
  for (let attempt = 0; attempt < 40; attempt++) {
    if (condition()) return true;
    await delay(35);
  }
  return false;
};
const results = [];
for (const file of files) {
  const path = resolve(process.argv[2] ?? "/submission/scripts", file);
  let setup;
  try {
    execFileSync(process.execPath, ["--check", path], { timeout: 2000, stdio: "pipe" });
    const source = readFileSync(path, "utf8");
    const start = source.indexOf("function spawnLogged(");
    const end = source.indexOf("async function waitForHttp(", start);
    if (start < 0 || end < 0) throw new Error("Cannot locate cleanup helpers");
    setup = new Function("spawn", source.slice(start, end) + "\nreturn { spawnLogged, stopChild };");
  } catch {
    results.push({ file, scenario: "脚本可加载", passed: false });
    continue;
  }
  for (const scenario of ["running", "already-exited"]) {
    let parent, descendant;
    try {
      const { spawnLogged, stopChild } = setup(spawn);
      parent = spawnLogged(process.execPath, ["-e", testProcess, scenario]);
      if (!await waitUntil(() => /^\d+$/.test(parent.outputText()))) throw new Error("Child PID unavailable");
      descendant = Number(parent.outputText());
      if (!alive(descendant)) throw new Error("Descendant did not start");
      if (scenario === "already-exited" && !await waitUntil(() => parent.exitCode !== null))
        throw new Error("Parent did not exit");
      await stopChild(parent);
      const passed = await waitUntil(() => !alive(parent.pid) && !alive(descendant));
      results.push({ file, scenario, passed });
    } catch {
      results.push({ file, scenario, passed: false });
    } finally {
      for (const pid of [parent?.pid, descendant]) {
        if (Number.isInteger(pid) && pid > 1 && alive(pid)) process.kill(pid, "SIGKILL");
      }
    }
  }
}
console.log(JSON.stringify(results));
