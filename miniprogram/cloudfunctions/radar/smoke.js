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

const { pollMaoyan, pollRail, pollMoretickets, parseSeat, errorKind } =
  require('./index.js')._internals;

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

// --- 猫眼（真实接口）------------------------------------------------------
console.log('\n[2] 猫眼 509862（汪苏泷·阜阳斑马音乐节）');
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
