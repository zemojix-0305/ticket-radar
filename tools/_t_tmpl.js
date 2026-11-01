/* 多模板字段映射的离线验证。
 * 5 个订阅消息模板字段名各不相同（thing5 在「余票提醒」是放票机构、在「回流票提醒」
 * 是价格类型），配错一个就是 47003 硬失败，且线上才发现。这里拿后台查到的真实
 * 字段清单当断言基准，逐模板比对生成出来的 key 集合。
 *
 * 运行：node tools/_t_tmpl.js
 */
'use strict';

// 后台 /wxaapi/newtmpl/gettemplate 实测得到的字段清单（2026-11-01 第二次查证）
const REAL = {
  YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4: ['thing6', 'thing8', 'time7', 'thing5', 'thing4'],
  '9w9j3Po1BnAGkoJQj6Xfql-wcRaRbyuXccxr7plsjdI': ['thing1', 'thing4', 'time2', 'thing5'],
  'PB9i3a7YZGOh-kvWKZ4Ey_tB5unlgHuI6U1ng8eCeOY': ['thing1', 'thing2', 'thing3', 'thing4', 'thing5'],
  'V0Cau9DqnX6xpflWxF50X7mdYZKd99TdeKoaELzM55M': ['thing1', 'time2', 'thing6'],
  // 出行提醒：服务名称 / 备注 / 目的地 / 出行日期
  'dLZJDaUZi-1u1nSlQ2EZqYqWNlu3QjtbAcD8XUqRFnw': ['thing1', 'thing4', 'thing8', 'time2'],
};

let pass = 0;
let fail = 0;
const ck = (name, cond, extra) => {
  if (cond) {
    pass++;
  } else {
    fail++;
    console.log('  FAIL ' + name + (extra ? '  -> ' + JSON.stringify(extra) : ''));
  }
};

function makeDb(subs) {
  return {
    createCollection: async () => true,
    collection() {
      return {
        where(q) {
          const m = () => subs.filter((r) => Object.keys(q).every((k) => r[k] === q[k]));
          return { limit: () => ({ get: async () => ({ data: m() }) }), get: async () => ({ data: m() }) };
        },
        doc(id) {
          const one = () => subs.find((r) => r._id === id);
          return {
            update: async ({ data }) => {
              const r = one();
              if (r) Object.assign(r, data);
              return {};
            },
          };
        },
      };
    },
  };
}

async function main() {
  const sent = [];
  let calls = 0; // send 被调了几次（成功失败都算）
  let rejectFirst = 0; // 前 N 次调用强制失败，用来验 fallback

  global.cloud = {
    init() {},
    database: () => makeDb([{ _id: 's1', openid: 'oTest', quota: 3 }]),
    getWXContext: () => ({ OPENID: 'oTest' }),
    openapi: {
      subscribeMessage: {
        send: async (p) => {
          const n = calls++;
          if (n < rejectFirst) {
            const e = new Error('mock 43101 user refuse');
            e.errCode = 43101;
            throw e;
          }
          sent.push(p);
          return { errCode: 0 };
        },
      },
    },
  };

  const p = require.resolve('../miniprogram/cloudfunctions/radar/index.js');
  delete require.cache[p];
  const I = require(p)._internals;

  // 1) 每个模板生成的 key 必须和后台真实字段**完全一致**（多一个少一个都不行）
  for (const id of Object.keys(REAL)) {
    const d = I.tmplData(id, {
      title: '郑州 ELLA 陈嘉桦演唱会',
      status: '看台 280',
      org: '猫眼',
      tip: '剩 3 张，尽快下单',
      when: '2026-11-07 周六 19:00',
      place: '奥体中心体育馆',
      price: '¥280',
    });
    const got = Object.keys(d).sort();
    const want = REAL[id].slice().sort();
    ck('字段集合 ' + id.slice(0, 8), JSON.stringify(got) === JSON.stringify(want), { got, want });
    for (const k of got) {
      const v = d[k].value;
      ck('非空 ' + id.slice(0, 6) + '.' + k, typeof v === 'string' && v.length > 0, v);
      if (k.slice(0, 4) === 'thing') ck('20字内 ' + k, v.length <= 20, v.length);
    }
  }

  // 2) time 字段必须是微信认的格式，带"周六"的原串要能规整掉
  ck('normTime 带星期', I.normTime('2026-11-07 周六 19:00') === '2026-11-07 19:00:00', I.normTime('2026-11-07 周六 19:00'));
  ck('normTime 中文年月日', I.normTime('2026年11月7日 19:00') === '2026-11-07 19:00:00', I.normTime('2026年11月7日 19:00'));
  ck('normTime 抽不出就退回当前', /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(I.normTime('待定')));
  // 机票 / 火车的出行日期只有「YYYY-MM-DD」，不能退成推送时刻
  ck('normTime 只有日期', I.normTime('2026-11-07') === '2026-11-07 00:00:00', I.normTime('2026-11-07'));

  // 3) alert 字段缺失时不能抛、不能出现空串（微信不收空 value）
  const bare = I.tmplData(I.TMPL_IDS[0], {});
  for (const k of Object.keys(bare)) ck('兜底非空 ' + k, !!bare[k].value);

  // 4) 未知模板 ID 不能崩（退回第一个模板的映射）
  ck('未知 ID 不崩', !!I.tmplData('deadbeef', { title: 'x' }).thing6);

  // 5) sendAlert：前 4 个失败、第 5 个成功 → 整体应算成功，且只发一次
  rejectFirst = 4;
  calls = 0;
  sent.length = 0;
  const showOrder = I.tmplOrder({ kind: 'restock', cat: 'show' });
  const r1 = await I.sendAlert('oTest', { kind: 'restock', cat: 'show', title: 'T', status: 'S', org: '猫眼', tip: 'P' }, global.cloud.database(), false);
  ck('前 4 个失败仍走通', r1.wx === 'ok', r1);
  ck('只发了一条', sent.length === 1, sent.length);
  ck('命中的是排序里的第 5 个', r1.tmpl === showOrder[4], r1.tmpl);

  // 5b) 车票类必须优先用「出行提醒」，不能硬塞进「演出名称」
  const travelOrder = I.tmplOrder({ kind: 'restock', cat: 'travel' });
  ck('出行优先 出行提醒', travelOrder[0] === 'dLZJDaUZi-1u1nSlQ2EZqYqWNlu3QjtbAcD8XUqRFnw', travelOrder[0]);
  ck('出行仍有 5 个兜底模板', travelOrder.length === 5, travelOrder.length);
  ck('演出优先 余票提醒', showOrder[0] === 'YnMbqU_UqlWb1eUOSMOp1RwxfGJozlrlXYtSaXFnsF4', showOrder[0]);

  // 5c) 出行提醒渲染出来要看得懂：目的地单独一格，日期是出行日不是推送时刻
  const fd = I.tmplData('dLZJDaUZi-1u1nSlQ2EZqYqWNlu3QjtbAcD8XUqRFnw', {
    cat: 'travel',
    title: '上海 → 东京 · 同程',
    org: '同程',
    tip: '现价 ¥1350 · 心理价 ¥1360',
    when: '2026-11-20',
    svc: '机票盯价 · 同程',
    dest: '东京',
  });
  ck('出行·服务名称', fd.thing1.value === '机票盯价 · 同程', fd.thing1.value);
  ck('出行·目的地', fd.thing8.value === '东京', fd.thing8.value);
  ck('出行·日期零点', fd.time2.value === '2026-11-20 00:00:00', fd.time2.value);
  ck('出行·备注是价格', fd.thing4.value.indexOf('心理价') >= 0, fd.thing4.value);

  // 6) 全部失败 → 必须把刚扣的额度还回去
  rejectFirst = 99;
  calls = 0;
  sent.length = 0;
  const subs = [{ _id: 's1', openid: 'oTest', quota: 1 }];
  const db3 = makeDb(subs);
  let threw = false;
  try {
    await I.sendAlert('oTest', { kind: 'restock', title: 'T' }, db3, false);
  } catch (e) {
    threw = true;
  }
  ck('全失败会抛（让上层记日志）', threw);
  ck('扣掉又还回 = 原值', subs[0].quota === 1, subs[0].quota);

  // 7) 模板 ID 本身合法：微信订阅消息模板 ID 是 43 位
  for (const id of I.TMPL_IDS) ck('ID 长度 43 ' + id.slice(0, 6), id.length === 43, id.length);

  console.log('\n' + (fail === 0 ? '全部通过' : '有失败') + '：' + pass + ' 通过 / ' + fail + ' 失败');
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
