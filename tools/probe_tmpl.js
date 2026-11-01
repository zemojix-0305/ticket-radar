/* 查订阅消息私有模板的字段名（不需要用户截图）。只在本地跑，不随云函数上传。
 * 用法：node tools/probe_tmpl.js [模板ID ...]
 */
const fs = require('fs');
const https = require('https');

const ENV = 'D:/Amyapp/ticket-radar/.env';
const env = {};
for (const line of fs.readFileSync(ENV, 'utf8').split(/\r?\n/)) {
  const m = line.match(/^([A-Za-z_0-9]+)\s*=\s*(.*)$/);
  if (m) env[m[1]] = m[2].trim().replace(/^["']|["']$/g, '');
}

function get(url) {
  return new Promise((res, rej) => {
    https.get(url, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => res(d));
    }).on('error', rej);
  });
}

(async () => {
  const appid = env.WX_APPID;
  const secret = env.WX_APPSECRET;
  if (!appid || !secret) return console.log('缺 WX_APPID / WX_APPSECRET');

  const tk = JSON.parse(
    await get(
      'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid=' +
        appid +
        '&secret=' +
        secret
    )
  );
  if (!tk.access_token) return console.log('取 token 失败：', JSON.stringify(tk));

  const r = JSON.parse(
    await get('https://api.weixin.qq.com/wxaapi/newtmpl/gettemplate?access_token=' + tk.access_token)
  );
  if (!r.data) return console.log('查模板失败：', JSON.stringify(r));

  const want = process.argv.slice(2);
  const list = want.length ? r.data.filter((t) => want.includes(t.priTmplId || t.tmplId)) : r.data;
  console.log('共 ' + r.data.length + ' 个模板，匹配到 ' + list.length + ' 个\n');
  for (const t of list) {
    const id = t.priTmplId || t.tmplId;
    console.log('标题：' + t.title);
    console.log('ID  ：' + id);
    console.log('类型：' + (Number(t.type) === 2 ? '一次性订阅' : '长期订阅'));
    const kws = t.keywordEnumValueList || [];
    if (kws.length) {
      kws.forEach((k, i) =>
        console.log('   [' + (i + 1) + '] ' + (k.keywordCode || '?') + ' = ' + (k.keywordName || ''))
      );
    } else {
      console.log('   内容模板：' + t.content);
    }
    console.log('');
  }
  if (want.length && list.length < want.length) {
    const got = list.map((t) => t.tmplId);
    console.log('没查到的 ID：', want.filter((w) => !got.includes(w)).join('\n  '));
  }
})();
