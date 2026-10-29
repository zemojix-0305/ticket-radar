# -*- coding: utf-8 -*-
"""机票面板预览 SVG：数据为上海→北京真实 30 天最低价"""
import io

BARS = [("10-29", 1626, 150), ("10-30", 510, 137), ("10-31", 374, 45), ("11-01", 423, 78),
        ("11-02", 373, 45), ("11-03", 364, 39), ("11-04", 373, 45), ("11-05", 500, 130),
        ("11-06", 710, 150), ("11-07", 380, 49), ("11-08", 409, 69), ("11-09", 380, 49),
        ("11-10", 370, 43), ("11-11", 427, 81), ("11-12", 399, 62), ("11-13", 530, 150),
        ("11-14", 386, 53), ("11-15", 380, 49), ("11-16", 373, 45), ("11-17", 370, 43),
        ("11-18", 373, 45), ("11-19", 373, 45), ("11-20", 493, 125), ("11-21", 373, 45),
        ("11-22", 360, 36), ("11-23", 373, 45), ("11-24", 370, 43), ("11-25", 370, 43),
        ("11-26", 386, 53), ("11-27", 432, 84)]

INK, INK2, INK3 = "#1C1C1E", "#5A5A60", "#6C6C75"
GREEN, PAPER, CARD = "#2F6B3A", "#F7F7FA", "#FFFFFF"
o = []
a = o.append

a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 830" width="100%">')
a('<defs><filter id="sd" x="-30%" y="-30%" width="160%" height="180%">'
  '<feDropShadow dx="0" dy="3" stdDeviation="7" flood-color="#1C1C1E" flood-opacity="0.07"/>'
  '</filter></defs>')
a('<rect width="680" height="830" fill="%s"/>' % PAPER)
a('<text x="20" y="40" font-size="25" font-weight="600" fill="%s">找演出</text>' % INK)

for label, x, w, on in [("12306", 20, 48, False), ("机票", 86, 54, True), ("猫眼", 158, 50, False),
                        ("摩天轮", 226, 62, False), ("大麦", 306, 46, False), ("B站", 370, 46, False)]:
    cx = x + w / 2.0
    if on:
        a('<rect x="%d" y="58" width="%d" height="32" rx="16" fill="%s"/>' % (x, w, INK))
        a('<text x="%.1f" y="79" font-size="14" font-weight="600" fill="#FFFFFF" text-anchor="middle">%s</text>' % (cx, label))
        a('<rect x="%d" y="96" width="%d" height="3" rx="1.5" fill="%s"/>' % (x + 10, w - 20, INK))
    else:
        a('<text x="%.1f" y="79" font-size="14" fill="%s" text-anchor="middle">%s</text>' % (cx, INK3, label))

a('<rect x="16" y="114" width="648" height="104" rx="22" fill="%s" filter="url(#sd)"/>' % CARD)
a('<text x="42" y="166" font-size="29" font-weight="600" fill="%s">上海</text>' % INK)
a('<text x="42" y="188" font-size="11" fill="%s" letter-spacing="2">SHA</text>' % INK3)
a('<text x="638" y="166" font-size="29" font-weight="600" fill="%s" text-anchor="end">北京</text>' % INK)
a('<text x="638" y="188" font-size="11" fill="%s" letter-spacing="2" text-anchor="end">BJS</text>' % INK3)
a('<circle cx="340" cy="168" r="18" fill="#F1F1F4"/>')
a('<text x="340" y="175" font-size="16" fill="%s" text-anchor="middle">⇌</text>' % INK2)

for label, x, w, on in [("今天", 20, 58, False), ("明天", 86, 58, True), ("后天", 152, 58, False), ("10-31", 218, 66, False)]:
    a('<rect x="%d" y="234" width="%d" height="30" rx="15" fill="%s" filter="url(#sd)"/>' % (x, w, INK if on else CARD))
    a('<text x="%.1f" y="254" font-size="12" fill="%s" text-anchor="middle">%s</text>'
      % (x + w / 2.0, "#FFFFFF" if on else INK2, label))
a('<rect x="548" y="234" width="116" height="30" rx="15" fill="#F1F1F4"/>')
a('<rect x="551" y="237" width="54" height="24" rx="12" fill="%s"/>' % CARD)
a('<text x="578" y="254" font-size="11" font-weight="600" fill="%s" text-anchor="middle">30天</text>' % INK)
a('<text x="634" y="254" font-size="11" fill="%s" text-anchor="middle">60天</text>' % INK3)

a('<rect x="16" y="280" width="648" height="78" rx="22" fill="%s" filter="url(#sd)"/>' % CARD)
for val, lab, cx, fs, col in [("¥360", "最低价", 130, 26, GREEN), ("2026-11-22", "周日最便宜", 340, 15, INK),
                              ("¥452", "均价", 552, 26, INK)]:
    a('<text x="%d" y="313" font-size="%d" font-weight="600" fill="%s" text-anchor="middle">%s</text>' % (cx, fs, col, val))
    a('<text x="%d" y="334" font-size="11" fill="%s" text-anchor="middle">%s</text>' % (cx, INK3, lab))
a('<line x1="225" y1="292" x2="225" y2="346" stroke="#ECECEC"/>')
a('<line x1="452" y1="292" x2="452" y2="346" stroke="#ECECEC"/>')
a('<rect x="16" y="368" width="648" height="30" rx="12" fill="#EAF4EC"/>')
a('<text x="32" y="388" font-size="12" fill="#3E6B48">挑对日子最多省 '
  '<tspan font-weight="600" fill="%s">21%%</tspan>（¥1626 → ¥360）</text>' % GREEN)

a('<text x="18" y="430" font-size="17" font-weight="600" fill="%s">价格走势</text>' % INK)
a('<text x="648" y="430" font-size="11" fill="%s" text-anchor="end">30 天 · 逐日最低</text>' % INK3)
a('<rect x="16" y="444" width="648" height="146" rx="22" fill="%s" filter="url(#sd)"/>' % CARD)
x0, base = 36, 572
for d, price, h in BARS:
    hh = int(round(h * 0.62))
    y = base - hh
    col = GREEN if price == 360 else "#DDDDE2"
    a('<rect x="%d" y="%d" width="12" height="%d" rx="6" fill="%s"/>' % (x0, y, hh, col))
    if price == 360:
        a('<text x="%.1f" y="%d" font-size="9" font-weight="600" fill="%s" text-anchor="middle">360</text>'
          % (x0 + 6, y - 6, GREEN))
        a('<text x="%.1f" y="%d" font-size="9" font-weight="600" fill="%s" text-anchor="middle">11-22</text>'
          % (x0 + 6, base + 14, GREEN))
    x0 += 21
a('<text x="36" y="%d" font-size="9" fill="%s">10-29</text>' % (base + 14, INK3))

a('<text x="18" y="622" font-size="17" font-weight="600" fill="%s">捡漏日</text>' % INK)
a('<text x="648" y="622" font-size="11" fill="%s" text-anchor="end">比均价低 20%% 以上</text>' % INK3)
a('<rect x="16" y="636" width="648" height="62" rx="16" fill="%s" filter="url(#sd)"/>' % CARD)
a('<text x="34" y="662" font-size="13" font-weight="600" fill="%s">2026-11-22</text>' % INK)
a('<text x="34" y="680" font-size="11" fill="%s">周日</text>' % INK3)
a('<text x="185" y="662" font-size="13" fill="%s">CZ8881</text>' % INK)
a('<text x="185" y="680" font-size="11" fill="%s">09:15 → 11:40</text>' % INK3)
a('<text x="640" y="663" font-size="17" font-weight="600" fill="%s" text-anchor="end">¥360</text>' % GREEN)
a('<text x="640" y="681" font-size="10" fill="%s" text-anchor="end">省 21%%</text>' % GREEN)

a('<text x="18" y="730" font-size="17" font-weight="600" fill="%s">去别的平台核对</text>' % INK)
a('<text x="648" y="730" font-size="11" fill="%s" text-anchor="end">点一下复制查询链接</text>' % INK3)
for i, (nm, nt) in enumerate([("同程", "当前价格来源"), ("携程", "需登录才返价"), ("去哪儿", "需登录才返价")]):
    px = 16 + i * 219
    a('<rect x="%d" y="744" width="209" height="66" rx="16" fill="%s" filter="url(#sd)"/>' % (px, CARD))
    a('<text x="%d" y="772" font-size="13" font-weight="600" fill="%s" text-anchor="middle">%s</text>' % (px + 104, INK, nm))
    a('<text x="%d" y="792" font-size="10" fill="%s" text-anchor="middle">%s</text>' % (px + 104, INK3, nt))

a('</svg>')
io.open("preview_flight_panel.svg", "w", encoding="utf-8").write("\n".join(o))
print("ok")
