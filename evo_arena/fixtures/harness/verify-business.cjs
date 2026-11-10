// Independent acceptance logic. This file runs in a separate verifier container.
const fs = require('node:fs'), crypto = require('node:crypto');
function verify(envelope, expected, key) {
  const signature = crypto.createHmac('sha256', key).update(envelope.payload).digest('hex');
  if (signature !== envelope.signature) throw new Error('Untrusted audit');
  const data = JSON.parse(envelope.payload);
  if (data.schema !== 'business-audit-v1' || data.case.checksum !== expected.checksum || data.case.id !== expected.id)
    throw new Error('Wrong case');
  const s = data.state, checks = [];
  const check = (name, passed, critical = false) => checks.push({name, passed: !!passed, critical});
  if (expected.family === 'receipt') {
    check('最终恰好一笔订单', s.orders === 1);
    check('没有重复写入或重复在途提交', s.orders <= 1 && !s.violations.length, true);
    check('最终状态经过一致性查询', s.verified && s.operation !== 'pending');
  } else if (expected.family === 'listing') {
    const latest = new Map();
    for (const r of data.truth) if (!latest.has(r.id) || latest.get(r.id).revision < r.revision) latest.set(r.id, r);
    const target = [...latest.values()].filter(r => r.customer === 'C-42' && r.status === 'pending');
    const ids = target.map(r => r.id).sort(), report = s.report;
    check('已遍历完整且一致的查询结果', data.complete_scan);
    check('清单准确、无遗漏、无重复', report && JSON.stringify([...report.ids].sort()) === JSON.stringify(ids));
    check('金额按最终有效记录汇总', report && report.total === target.reduce((v, r) => v + r.amount, 0));
    check('未混入其他客户或取消订单', !report || report.ids.every(id => ids.includes(id)), true);
  } else if (expected.family === 'workflow') {
    const reject = ['confirm_rejected', 'cancel_timeout', 'stock_conflict'].includes(expected.mechanism);
    check('补偿有实际永久失败依据', !reject || data.audit.some(e => ['confirm', 'reserve'].includes(e.action) && e.result.permanent === true));
    check('订单最终状态正确', s.order_status === (reject ? 'cancelled' : 'confirmed'));
    check('库存占用与最终状态一致', s.reserved === (reject ? 0 : 2));
    check('没有重复占用或破坏已确认订单', !s.violations.length, true);
    check('最终状态已查询确认', s.verified);
  } else throw new Error('Unknown family');
  return {checks, ledger: {family: expected.family, actions: data.audit.length,
    critical_violations: checks.filter(c => c.critical && !c.passed).length}, reward: Number(checks.every(c => c.passed))};
}
module.exports = {verify};
if (require.main === module) {
  const result = verify(JSON.parse(fs.readFileSync('/evo-ledger/receipt.json')), JSON.parse(fs.readFileSync('/tests/case.json')), process.env.ARENA_VERIFY_TOKEN);
  fs.writeFileSync('/logs/verifier/reward.json', JSON.stringify({reward: result.reward}));
  const payload = JSON.stringify(result);
  const signature = crypto.createHmac('sha256', process.env.ARENA_VERIFY_TOKEN).update(payload).digest('hex');
  console.log(JSON.stringify({...result, attestation: {payload, signature}}));
}
