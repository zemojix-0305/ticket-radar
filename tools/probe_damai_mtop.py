# -*- coding: utf-8 -*-
"""探测大麦 H5 mtop 匿名搜索链路是否可用（不登录、不带用户凭据）。

流程：任意 mtop 请求会种下匿名令牌 _m_h5_tk；
第二次请求用 md5(token&t&appKey&data) 签名即可调用搜索接口。
"""
import hashlib
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
APPKEY = "12574478"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [("User-Agent", UA), ("Referer", "https://m.damai.cn/")]


def mtop(api, data, v="1.0"):
    t = str(int(time.time() * 1000))
    token = ""
    for c in cj:
        if c.name == "_m_h5_tk":
            token = c.value.split("_")[0]
    sign = hashlib.md5(f"{token}&{t}&{APPKEY}&{data}".encode()).hexdigest()
    url = (f"https://h5api.m.taobao.com/h5/{api}/{v}/?jsv=2.7.2&appKey={APPKEY}"
           f"&t={t}&sign={sign}&api={api}&v={v}&type=originaljson&dataType=json"
           f"&valueType=string&timeout=10000&data={urllib.parse.quote(data)}")
    r = opener.open(url, timeout=15)
    return r.read().decode("utf-8", "ignore")


DATA = json.dumps({"keyword": "孙燕姿", "pageNo": 1, "pageSize": 10},
                  ensure_ascii=True, separators=(",", ":"))
ret1 = mtop("mtop.taobao.damai.wireless.search", DATA)
print("round1:", ret1[:200])
ret2 = mtop("mtop.taobao.damai.wireless.search", DATA)
print("round2:", ret2[:1200])
