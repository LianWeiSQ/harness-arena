#!/usr/bin/env node
const usage = `evo-business inspect|status|submit|reserve|confirm|release|cancel|refresh
evo-business list [--cursor VALUE] [--snapshot VALUE]
evo-business report --ids ID,ID,... --total INTEGER_CENTS
Commands operate only on the task's isolated business account. inspect describes the contract.
help and --help are local, have no effects, and need no service connection.`;
const [action, ...rest] = process.argv.slice(2);
if ((!action || action === 'help' || action === '--help') && !rest.length || rest.length === 1 && rest[0] === '--help') {
  console.log(usage); process.exit(0);
}
const allowed = {inspect: [], status: [], submit: [], reserve: [], confirm: [], release: [], cancel: [], refresh: [],
  list: ['cursor', 'snapshot'], report: ['ids', 'total']};
const args = {};
let valid = Object.hasOwn(allowed, action) && rest.length % 2 === 0;
for (let i = 0; valid && i < rest.length; i += 2) {
  const key = rest[i].slice(2);
  if (!rest[i].startsWith('--') || !allowed[action].includes(key) || Object.hasOwn(args, key) || rest[i + 1].startsWith('--')) valid = false;
  else args[key] = rest[i + 1];
}
if (valid && action === 'report') {
  valid = Object.hasOwn(args, 'ids') && /^\d+$/.test(args.total || '') && Number.isSafeInteger(Number(args.total));
  args.ids = args.ids ? args.ids.split(',') : [];
  args.total = Number(args.total);
}
if (!valid) { console.error('Invalid arguments; no request sent.\n' + usage); process.exit(2); }
fetch((process.env.ARENA_TRIAL_URL || 'http://127.0.0.1:18765') + '/business', {
  method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action, args}),
  signal: AbortSignal.timeout(10000)
}).then(async response => {
  console.log(JSON.stringify({http: response.status, ...await response.json()}));
  if (!response.ok) process.exitCode = 1;
}).catch(() => { console.error('Transport failed; outcome unknown'); process.exitCode = 1; });
