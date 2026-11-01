/* 机票提醒模式（both / drop / target）的离线验证。
 *
 * 为什么值得单独测：模式判断错了，表现是「用户选了只报到价，却一直被降价消息烦」
 * 或者反过来「选了只报降价，票价跌穿心理价也没动静」——两种都属于最难被发现的
 * 静默错误（不报错、只是不提醒），只能靠用例钉住。
 *
 * 运行：node tools/_t_flightmode.js
 */
'use strict';

let pass = 0;
let fail = 0;
const ck = (name, cond, extra) => {
  if (cond) pass++;
  else {
    fail++;
    console.log('  FAIL ' + name + (extra ? '  -> ' + JSON.stringify(extra) : ''));
  }
};

// 只 mock 到能被 require 的程度：flightAlert 本身不碰 cloud
global.cloud = { init() {}, database: () => ({}), getWXContext: () => ({ OPENID: 'oT' }) };

const I = require('../miniprogram/cloudfunctions/radar/index.js')._internals;

const V = I.PRICE_LOGIC_V;

/** 造一份上一轮基线 */
function prevOf(items) {
  return { ok: true, logicV: V, items: items.map((it) => ({ label: it.label, price: it.price })) };
}

/** 造一份本轮结果 */
function recOf(items) {
  return { ok: true, name: '上海 → 东京 · 同程', items };
}

const OLD = [{ label: '上海 → 东京 · 同程', price: 2000 }];
const DROP_ONLY = [{ label: '上海 → 东京 · 同程', price: 1900 }]; // -100，算降价，未到 1500
const TARGET = [{ label: '上海 → 东京 · 同程', price: 1450 }]; // 跌进 1500

function run(mode, budget, prevItems, nowItems) {
  return I.flightAlert(
    prevOf(prevItems),
    recOf(nowItems),
    { budget: budget, alert_mode: mode },
    '上海 → 东京 · 同程',
  );
}

// 1) 默认（没存 alert_mode）= both，跟老任务行为一致
const def = run(undefined, 1500, OLD, TARGET);
ck('缺省模式下到价能报', def && def.kind === 'target', def && def.kind);

const def2 = run(undefined, 1500, OLD, DROP_ONLY);
ck('缺省模式下降价能报', def2 && def2.kind === 'drop', def2 && def2.kind);

// 2) 只报降价：跌穿心理价也不发到价消息，但仍然算一次降价
const d1 = run('drop', 1500, OLD, TARGET);
ck('drop 模式不报到价', d1 && d1.kind !== 'target', d1 && d1.kind);
ck('drop 模式跌穿心理价当降价报', d1 && d1.kind === 'drop', d1 && d1.kind);

// 3) 只报到价：没跌进心理价时，降价不报
const t1 = run('target', 1500, OLD, DROP_ONLY);
ck('target 模式不报降价', t1 === null, t1 && t1.kind);

const t2 = run('target', 1500, OLD, TARGET);
ck('target 模式跌进心理价才报', t2 && t2.kind === 'target', t2 && t2.kind);

// 4) 只报到价但没填心理价 = 什么都不报（前端会拦住，后端也不能乱报）
const t3 = run('target', 0, OLD, TARGET);
ck('target 无心理价 → 静默', t3 === null, t3 && t3.kind);

// 5) 已经在心理价以下再跌，不再重复报到价
const t4 = I.flightAlert(
  prevOf([{ label: '上海 → 东京 · 同程', price: 1400 }]),
  recOf([{ label: '上海 → 东京 · 同程', price: 1300 }]),
  { budget: 1500, alert_mode: 'target' },
  '上海 → 东京 · 同程',
);
ck('已在价内再跌不重复报到价', t4 === null, t4 && t4.kind);

// 6) 提示文案能分得清两种：到价说「心理价」，降价说「省」
ck('到价文案带心理价', def && /心理价/.test(def.status + def.tip), def && def.status);
ck('降价文案带省多少', def2 && /省/.test(def2.status), def2 && def2.status);

// 7) 出行模板要用到的语义位都齐了
ck('alert 带 cat=travel', def && def.cat === 'travel', def && def.cat);
ck('alert 带目的地', def && def.dest === '东京', def && def.dest);
ck('alert 带服务名称', def && /机票盯价/.test(def.svc || ''), def && def.svc);
ck('alert 回传模式', def && def.alertMode === 'both', def && def.alertMode);

console.log('\n' + (fail === 0 ? '全部通过' : '有失败') + '：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail === 0 ? 0 : 1);
