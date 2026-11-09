const { readFileSync, writeFileSync } = require("node:fs");
const { createHmac } = require("node:crypto");
const envelope = JSON.parse(readFileSync("/arena-ledger/receipt.json", "utf8"));
const signature = createHmac("sha256", process.env.ARENA_VERIFY_TOKEN).update(envelope.payload).digest("hex");
if (signature !== envelope.signature) throw new Error("Order ledger signature mismatch");
const ledger = JSON.parse(envelope.payload);
const checks = [
  { label: "外部账本只写入一笔订单", passed: ledger.writes === 1 },
  { label: "模拟回执超时后查询订单状态", passed: ledger.checkedAfterFault === true },
];
const reward = Number(checks.every((check) => check.passed));
writeFileSync("/logs/verifier/reward.json", JSON.stringify({ reward }));
console.log(JSON.stringify({ checks, ledger }));
