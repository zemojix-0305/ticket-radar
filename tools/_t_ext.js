/* 外部推送通道的离线验证：用一个本地 HTTP 服务器冒充 ntfy、一个本地 SMTP
 * 服务器冒充 126 邮箱，端到端跑一遍。放在 tools/ 而不是云函数目录里——
 * 云函数上传是整包上传，测试脚本不该混进去。
 * 运行：NODE_PATH=<workspace>/node_modules node tools/_t_ext.js
 *   （需要 nodemailer；本地从隔离工作区注入，云端由 package.json 装）
 */
'use strict';

const http = require('http');
const net = require('net');

let lastReq = null; // 本地 HTTP 服务器收到的最后一次请求
let lastMail = null; // 本地 SMTP 服务器收到的最后一封信
let port = 0;
let smtpPort = 0;

function makeDb(subs) {
  const rows = () => subs;
  const match = (q) => rows().filter((r) => Object.keys(q).every((k) => r[k] === q[k]));
  return {
    createCollection: async () => true,
    collection() {
      return {
        where(q) {
          return {
            limit() {
              return { get: async () => ({ data: match(q) }) };
            },
            get: async () => ({ data: match(q) }),
          };
        },
        doc(id) {
          const one = () => rows().find((r) => r._id === id);
          return {
            update: async ({ data }) => {
              const r = one();
              if (r) Object.assign(r, data);
              return {};
            },
            set: async ({ data }) => {
              const r = one();
              if (r) Object.assign(r, data);
              return {};
            },
          };
        },
        add: async ({ data }) => {
          rows().push(Object.assign({ _id: 'n' + rows().length }, data));
          return { _id: 'n' };
        },
      };
    },
  };
}

/** 极简 SMTP 服务器：只做到能收下一封信的程度（明文、不做 TLS） */
function startSmtpMock() {
  return new Promise((resolve) => {
    const srv = net.createServer((sock) => {
      let buf = '';
      let inData = false;
      let mail = { from: '', to: [], data: '' };
      sock.write('220 mock ESMTP\r\n');
      sock.on('data', (chunk) => {
        buf += chunk.toString('utf8');
        let idx;
        while ((idx = buf.indexOf('\r\n')) >= 0) {
          const line = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (inData) {
            if (line === '.') {
              inData = false;
              lastMail = mail;
              mail = { from: '', to: [], data: '' };
              sock.write('250 OK queued\r\n');
            } else {
              mail.data += line + '\n';
            }
            continue;
          }
          const up = line.toUpperCase();
          if (up.startsWith('EHLO') || up.startsWith('HELO')) {
            sock.write('250-mock\r\n250-AUTH PLAIN LOGIN\r\n250 SIZE 10485760\r\n');
          } else if (up.startsWith('AUTH PLAIN')) {
            sock.write('235 Authentication successful\r\n');
          } else if (up === 'AUTH LOGIN') {
            sock.write('334 VXNlcm5hbWU6\r\n');
          } else if (up.startsWith('MAIL FROM')) {
            mail.from = line.slice(10).trim();
            sock.write('250 OK\r\n');
          } else if (up.startsWith('RCPT TO')) {
            mail.to.push(line.slice(8).trim());
            sock.write('250 OK\r\n');
          } else if (up === 'DATA') {
            inData = true;
            sock.write('354 End data with <CR><LF>.<CR><LF>\r\n');
          } else if (up === 'QUIT') {
            sock.write('221 Bye\r\n');
            sock.end();
          } else {
            sock.write('250 OK\r\n');
          }
        }
      });
      sock.on('error', () => {});
    });
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

/** 拿到一个全新加载的云函数实例（清掉 nodemailer transport 单例缓存） */
function freshIndex() {
  const p = require.resolve('../miniprogram/cloudfunctions/radar/index.js');
  delete require.cache[p];
  return require(p);
}

async function main() {
  // 1) 本地服务器冒充 ntfy
  const srv = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      lastReq = { url: req.url, body: body };
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"id":"ok"}');
    });
  });
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  port = srv.address().port;

  const smtp = await startSmtpMock();
  smtpPort = smtp.address().port;

  const subs = [
    { _id: 's1', openid: 'oTest', quota: 0, ext: { kind: 'ntfy', target: 'http://127.0.0.1:' + port + '/t1' } },
  ];
  const db = makeDb(subs);

  global.cloud = {
    init() {},
    database: () => db,
    getWXContext: () => ({ OPENID: 'oTest' }),
    openapi: { subscribeMessage: { send: async () => ({ errCode: 0 }) } },
  };

  let fn = freshIndex();
  let I = fn._internals;

  let pass = 0;
  let fail = 0;
  const ck = (name, cond, extra) => {
    if (cond) {
      pass++;
      console.log('  ok   ' + name);
    } else {
      fail++;
      console.log('  FAIL ' + name + (extra ? '  -> ' + JSON.stringify(extra) : ''));
    }
  };

  // 2) 地址识别
  console.log('\n[地址识别]');
  ck('企业微信 webhook',
    I.parseExt('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123').kind === 'wecom');
  ck('企业微信 key 提取',
    I.parseExt('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123').target === 'abc-123');
  ck('Server酱 SendKey',
    I.parseExt('SCT123456Tabcdef').kind === 'sct');
  ck('Server酱 完整地址',
    I.parseExt('https://sctapi.ftqq.com/SCT123abc.send').target === 'SCT123abc');
  ck('ntfy 地址',
    I.parseExt('https://ntfy.sh/zemo-ticket-9x2').kind === 'ntfy');
  ck('ntfy 去尾斜杠',
    I.parseExt('https://ntfy.sh/zemo-ticket-9x2/').target === 'https://ntfy.sh/zemo-ticket-9x2');
  ck('PushPlus token',
    I.parseExt('0123456789abcdef0123456789abcdef').kind === 'pushplus');
  ck('ntfy 地址不被当成 token',
    I.parseExt('https://ntfy.sh/topic').kind !== 'pushplus');

  console.log('\n[邮箱识别]');
  const m1 = I.parseExt('ticketradar@126.com');
  ck('126 邮箱', m1 && m1.kind === 'mail', m1);
  ck('邮箱原样保留', m1 && m1.target === 'ticketradar@126.com', m1);
  ck('大写归一', I.parseExt('TicketRadar@126.COM').target === 'ticketradar@126.com');
  ck('带点的本地名', I.parseExt('a.b+c@qq.com').kind === 'mail');
  ck('邮箱不会被 PushPlus 抢', I.parseExt('ticketradar@126.com').kind === 'mail');
  ck('缺域名不认', I.parseExt('abc@126') === null);
  ck('缺本地名不认', I.parseExt('@126.com') === null);
  ck('乱码不认', I.parseExt('这不是地址') === null);
  ck('空串不认', I.parseExt('') === null);

  // 3) 真的发出去（本地服务器当 ntfy）
  console.log('\n[真实发送 · ntfy]');
  const r1 = await I.sendExternal(db, 'oTest', {
    kind: 'drop',
    title: '上海→东京',
    status: '¥1530→¥1400 省130',
    tip: '降价提醒',
  });
  ck('ntfy 返回成功', r1[0] && r1[0].ok === true, r1);
  ck('服务器收到 POST', lastReq && lastReq.url.indexOf('/t1') === 0, lastReq);
  ck('标题带【降价】', lastReq && decodeURIComponent(lastReq.url).indexOf('【降价】') > 0, lastReq && lastReq.url);
  ck('正文含价格', lastReq && lastReq.body.indexOf('1530') > 0, lastReq && lastReq.body);

  const r2 = await I.sendExternal(db, 'oTest', { kind: 'target', title: '上海→大阪', status: '现价 ¥980' });
  ck('到价标题', decodeURIComponent(lastReq.url).indexOf('【到心理价】') > 0, lastReq && lastReq.url);
  ck('到价也算成功', r2[0] && r2[0].ok === true);

  // 4) 微信没额度时，外部通道必须顶住（这是这次改动的核心）
  console.log('\n[降级逻辑]');
  const r3 = await I.sendAlert('oTest', { kind: 'drop', title: 't', status: 's' }, db, false);
  ck('额度 0 时不抛错', !!r3, r3);
  ck('wx 标记 no_quota', r3.wx === 'no_quota', r3);
  ck('ext 已发出', r3.ext.some((x) => x.ok), r3.ext);

  // 5) 两条路都断才抛错
  subs[0].ext = { kind: 'ntfy', target: 'http://127.0.0.1:1/dead' };
  let threw = false;
  try {
    await I.sendAlert('oTest', { kind: 'drop', title: 't', status: 's' }, db, false);
  } catch (e) {
    threw = true;
  }
  ck('全断时抛错', threw);

  // ---------------------------------------------------------------------
  // 6) 邮件通道：换成真的 SMTP（本地 mock），验证分流
  // ---------------------------------------------------------------------
  console.log('\n[邮件通道]');
  process.env.SMTP_HOST = '127.0.0.1';
  process.env.SMTP_PORT = String(smtpPort);
  process.env.SMTP_USER = 'ticketradar@126.com';
  process.env.SMTP_PASS = 'fake-auth-code';
  fn = freshIndex();
  I = fn._internals;

  subs[0].ext = { kind: 'mail', target: 'me@qq.com' };
  const r4 = await I.sendExternal(db, 'oTest', {
    kind: 'drop',
    chan: 'mail',
    title: '上海→东京',
    status: '¥1530→¥1400 省130',
    org: '同程',
    tip: '比上次便宜了',
  });
  ck('邮件返回成功', r4[0] && r4[0].ok === true, r4);
  ck('SMTP 收到信', !!lastMail, lastMail);
  ck('RCPT 是绑定的邮箱', lastMail && lastMail.to.join(',').indexOf('me@qq.com') >= 0, lastMail && lastMail.to);
  ck('MAIL FROM 是发件账号',
    lastMail && lastMail.from.indexOf('ticketradar@126.com') >= 0, lastMail && lastMail.from);
  ck('信件含主题头', lastMail && /^Subject:/m.test(lastMail.data), lastMail && lastMail.data.slice(0, 200));
  ck('信件含项目名（base64 编码）', lastMail && lastMail.data.length > 200);

  // 演出类提醒（没有 chan:'mail'）不该走邮箱
  lastMail = null;
  const r5 = await I.sendExternal(db, 'oTest', {
    kind: 'restock', title: '某演唱会', status: '有回流票',
  });
  ck('演出不走邮箱', r5.length === 0, r5);
  ck('演出确实没发信', lastMail === null);

  // 7) 分流：机票 + 邮件成功 → 不扣微信额度
  console.log('\n[机票走邮件 · 省微信额度]');
  subs[0].quota = 3;
  const r6 = await I.sendAlert('oTest', {
    kind: 'drop', chan: 'mail', title: '上海→东京', status: '¥1530→¥1400',
  }, db, false);
  ck('wx 被跳过', r6.wx === 'skip', r6);
  ck('额度没动', subs[0].quota === 3, subs[0].quota);
  ck('邮件已发出', r6.ext.some((x) => x.kind === 'mail' && x.ok), r6.ext);

  // 8) 邮件挂了 → 回落微信，一条都不能丢
  console.log('\n[邮件挂了回落微信]');
  const keepPort = process.env.SMTP_PORT;
  process.env.SMTP_PORT = '1'; // 连接立刻被拒，不用等 8 秒超时
  fn = freshIndex();
  I = fn._internals;
  subs[0].quota = 2;
  const r7 = await I.sendAlert('oTest', {
    kind: 'drop', chan: 'mail', title: '上海→东京', status: '¥1530',
  }, db, false);
  ck('邮件失败但不抛错（微信兜住了）', !!r7, r7);
  ck('wx 走到成功', r7.wx === 'ok', r7);
  ck('额度扣掉 1 条', subs[0].quota === 1, subs[0].quota);
  process.env.SMTP_PORT = keepPort;

  // 9) 没配环境变量时，邮件通道要给明确错误而不是崩掉
  console.log('\n[邮件未配置]');
  delete process.env.SMTP_USER;
  delete process.env.SMTP_PASS;
  fn = freshIndex();
  I = fn._internals;
  const r8 = await I.sendExternal(db, 'oTest', {
    kind: 'drop', chan: 'mail', title: 't', status: 's',
  });
  ck('未配置时不抛错', r8[0] && r8[0].ok === false, r8);
  ck('错误信息说清缺什么',
    r8[0] && /SMTP_USER/.test(r8[0].error || ''), r8[0] && r8[0].error);

  // 10) 脱敏
  console.log('\n[脱敏]');
  ck('短串不留全貌', I.maskTarget('SCT123') === 'SCT1****', I.maskTarget('SCT123'));
  ck('长串留头尾', I.maskTarget('abcdefghijklmnop') === 'abcdefgh****mnop', I.maskTarget('abcdefghijklmnop'));
  ck('邮箱脱敏', I.maskTarget('ticketradar@126.com') === 'ticketra****.com', I.maskTarget('ticketradar@126.com'));

  srv.close();
  smtp.close();
  console.log('\n' + (fail ? 'FAILED ' + fail + ' / ' : 'ALL PASS ') + (pass + fail) + ' checks');
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
