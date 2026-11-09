#!/usr/bin/env node
const [action] = process.argv.slice(2);
const methods = { submit: "POST", status: "GET" };
if (!methods[action]) {
  console.error("Usage: arena-order submit|status");
  process.exit(2);
}
fetch(process.env.ARENA_TRIAL_URL + "/order", {
  method: methods[action],
  headers: { Authorization: "Bearer " + process.env.ARENA_TRIAL_TOKEN },
}).then(async (response) => {
  const data = await response.json();
  const print = response.ok ? console.log : console.error;
  print(JSON.stringify(data));
  if (!response.ok) process.exitCode = 1;
}).catch(() => {
  console.error("Order service unavailable");
  process.exitCode = 1;
});
