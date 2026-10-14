/**
 * 离线冒烟测试：用真实 API 验证云函数的解析逻辑。
 *
 * 为什么必须这么做：云函数部署上去要等审核、配环境、传 AppID，
 * 一旦解析逻辑有错，排查链路长到令人绝望。**在本地先跑一遍真接口**，
 * 能把「代码对不对」和「云 IP 能不能过」这两个问题彻底分开。
 *
 * 跑法：node cloudfunctions/radar/smoke.js
 * 需要网络。这不是单元测试，是「这个平台的接口现在还认不认我们」的活体检查。
 */

'use strict';

// 云函数里有几处用 cloud.getStorage 缓存站名表，本地给它个空实现
global.cloud = {
  getStorage: async () => null,
  setStorage: async () => {},
  database: () => ({ collection: () => ({ where: () => ({ get: async () => ({ data: [] }) }) }) }),
};

const {
  pollMaoyan, pollRail, pollMoretickets, parseSeat, errorKind,
  filterItemsByTiers, wantedTiers,
} = require('./index.js')._internals;

let pass = 0;
let fail = 0;

function check(name, ok, detail) {
  if (ok) {
    pass++;
    console.log('  ✅ ' + name + (detail ? '  ' + detail : ''));
  } else {
    fail++;
    console.log('  ❌ ' + name + (detail ? '  ' + detail : ''));
  }
}

// --- 纯函数：三态解析（不联网，必须永远对）--------------------------------
console.log('\n[1] 余票三态解析');
check('「有」= 有票、数量未知', (() => {
  const s = parseSeat('二等座', '有');
  return s && s.available === true && s.count === null;
})());
check('「12」= 12 张', (() => {
  const s = parseSeat('二等座', '12');
  return s && s.available === true && s.count === 12;
})());
check('「无」= 0 张', (() => {
  const s = parseSeat('二等座', '无');
  return s && s.available === false && s.count === 0;
})());
check('「--」= 本次不提供该席别', parseSeat('商务座', '--') === null);
check('乱码 = 跳过而不是瞎猜', parseSeat('二等座', '?????') === null);
check('错误分档：HTML 风控页', errorKind(new Error('返回了 HTML——通常是风控拦截页')) === 'blocked');
check('错误分档：平台自己报错', errorKind(new Error('statusCode=12123')) === 'platform');

// --- 纯函数：价位多选过滤（不联网，必须永远对）----------------------------
// 这段决定「用户勾了 399，是不是真的只盯 399」。踩过两个坑，都留在这儿当警钟：
//   ① 前端发的是数组 price_labels，服务端读的单数 price_label → 多选静默失效
//      （用户勾了三个价位，实际一个都没过滤，看起来「多选没用」）；
//   ② 子串匹配把 399 当成 1399 的一部分 → 勾 399 看台，1399 内场放票也推醒你。
console.log('\n[2] 价位多选过滤');
const FIX = [
  { label: '看台 399', price: '399' },
  { label: '看台 1399', price: '1399' },
  { label: '内场 1699', price: '1699' },
  { label: 'VIP 699', price: '699' },
];
check('多选 = 并集（399 + 1699）', (() => {
  const out = filterItemsByTiers(FIX, { price_labels: ['399', '1699'] });
  return out.length === 2 && out[0].price === '399' && out[1].price === '1699';
})());
check('数字整体相等：399 不该命中 1399',
  filterItemsByTiers(FIX, { price_labels: ['399'] }).length === 1);
check('旧字段（单数 price_label）仍兼容',
  filterItemsByTiers(FIX, { price_label: '699' }).length === 1);
check('非数字关键字走子串（看台 → 2 条）',
  filterItemsByTiers(FIX, { price_labels: ['看台'] }).length === 2);
check('一个价位都不勾 = 整场（不过滤）',
  filterItemsByTiers(FIX, {}).length === FIX.length);
check('price_labels 优先于 price_label',
  wantedTiers({ price_labels: ['1'], price_label: '2' }).join(',') === '1');

// --- 猫眼（真实接口）------------------------------------------------------
console.log('\n[3] 猫眼 509862（汪苏泷·阜阳斑马音乐节）');
pollMaoyan({ performance_id: "509862" })
  .then((items) => {
    check('抓到数据', items.length > 0, items.length + ' 条');
    if (items.length) {
      console.log('     ' + JSON.stringify(items[0], null, 0).slice(0, 150));
    }
    return pollRail({
      from: 'IZQ',
      to: 'CSQ',
      date: futureDate(3),
      seat_types: ['二等座'],
    });
  })
  .then((items) => {
    check('12306 广州南→长沙南', items.length > 0, items.length + ' 趟有票');
    if (items.length) {
      console.log('     ' + JSON.stringify(items[0], null, 0).slice(0, 150));
      const withCount = items.filter((i) => i.status.indexOf('张') >= 0);
      console.log('     带张数的：' + withCount.length + ' / ' + items.length);
    }
    return pollMoretickets({ tour_id: '67357b7aaca3af00010caba6', city: '广州' });
  })
  .then((items) => {
    check('摩天轮孙燕姿广州（POST 接口）', items.length > 0, items.length + ' 条');
  })
  .catch((e) => {
    fail++;
    console.log('  ❌ 真实接口调用失败：' + (e.message || e));
  })
  .then(() => {
    console.log('\n──────────────');
    console.log(fail === 0 ? `全部通过：${pass} 项` : `通过 ${pass} 项，失败 ${fail} 项`);
    process.exit(fail === 0 ? 0 : 1);
  });

function futureDate(days) {
  const d = new Date(Date.now() + days * 86400000);
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}
