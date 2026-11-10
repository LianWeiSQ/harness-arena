#!/usr/bin/env node
const [action, ...extra] = process.argv.slice(2);
const methods = { submit: "POST", status: "GET" };
if ((action === 'help' || action === '--help') && !extra.length || methods[action] && extra.length === 1 && extra[0] === '--help') {
  console.log("Usage: arena-order submit|status");
  process.exit(0);
}
if (!methods[action] || extra.length) {
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
