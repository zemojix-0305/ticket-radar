# -*- coding: utf-8 -*-
"""机票面板「当天航班」版式预览（真实数据：同程 SSR，SHA->BJS 2026-11-10）"""

import io

W = 680
INK = '#2C2C2A'
INK2 = '#5F5E5A'
INK3 = '#888780'
GREEN = '#3B6D11'
GREEN_BG = '#EAF3DE'
GOLD = '#854F0B'
GOLD_BG = '#FAEEDA'
BORDER = '#E4E3DD'
PAPER = '#FFFFFF'
CARD = '#FFFFFF'

flights = [
    dict(dep='07:00', arr='09:20', no='MU5099', air='东方航空', plane='空客332(宽)',
         ap1='虹桥T2', ap2='首都T2', span='2h20m', stop='直飞',
         cabs=[('经济舱', 1040, '4.9折'), ('公务舱', 2280, '2.8折')],
         foot='早餐 · 上网 · 机龄13.7年'),
    dict(dep='07:20', arr='09:40', no='HU7614', air='海南航空', plane='波音737-800(中)',
         ap1='浦东T2', ap2='首都T2', span='2h20m', stop='直飞',
         cabs=[('经济舱', 500, '2.4折'), ('公务舱', 1300, '1.6折')],
         foot='早餐 · 机龄13.2年'),
    dict(dep='08:10', arr='10:35', no='CZ8890', air='南方航空', plane='空客330(大)',
         ap1='虹桥T2', ap2='大兴', span='2h25m', stop='直飞',
         cabs=[('经济舱', 480, '3.0折'), ('公务舱', 2280, '6.9折')],
         foot='餐食 · 机龄6.8年'),
    dict(dep='08:30', arr='10:55', no='KN5978', air='中国联合航空', plane='波音737(中)',
         ap1='大兴', ap2='首都T2', span='2h25m', stop='直飞',
         cabs=[('经济舱', 410, '2.6折')],
         foot='机龄8.1年'),
]

CARD_H = 140
STEP = 152
o = []
a = o.append

a('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 %d" width="100%%" role="img">' % (100 + len(flights) * STEP + 40))
a('<title>机票面板的当天航班列表</title>')
a('<desc>上海到北京 2026-11-10 共 10 班，每班显示时刻、机场航站楼、航班号、航司、机型与各舱位价格</desc>')
a('<rect width="680" height="%d" fill="%s"/>' % (100 + len(flights) * STEP + 40, PAPER))

a('<text x="20" y="36" font-size="15" font-weight="500" fill="%s">当天航班</text>' % INK)
a('<text x="660" y="36" font-size="11" fill="%s" text-anchor="end">2026-11-10 · 共 10 班</text>' % INK3)

a('<rect x="20" y="52" width="222" height="34" rx="17" fill="#F1EFE8"/>')
tabs = [('全部', 20, 62, True), ('经济舱', 84, 74, False), ('公务舱', 160, 74, False)]
for label, x, w, on in tabs:
    if on:
        a('<rect x="%d" y="55" width="%d" height="28" rx="14" fill="%s"/>' % (x, w, INK))
        a('<text x="%d" y="74" font-size="12" font-weight="500" fill="#fff" text-anchor="middle">%s</text>' % (x + w / 2, label))
    else:
        a('<text x="%d" y="74" font-size="12" fill="%s" text-anchor="middle">%s</text>' % (x + w / 2, INK2, label))
a('<text x="660" y="74" font-size="11" fill="%s" text-anchor="end">排序 时间 · 价格</text>' % INK3)

y = 100
for f in flights:
    a('<rect x="20" y="%d" width="640" height="%d" rx="14" fill="%s" stroke="%s" stroke-width="0.5"/>' % (y, CARD_H, CARD, BORDER))
    a('<text x="38" y="%d" font-size="22" font-weight="500" fill="%s">%s</text>' % (y + 34, INK, f['dep']))
    a('<text x="38" y="%d" font-size="11" fill="%s">%s</text>' % (y + 52, INK3, f['ap1']))
    a('<text x="642" y="%d" font-size="22" font-weight="500" fill="%s" text-anchor="end">%s</text>' % (y + 34, INK, f['arr']))
    a('<text x="642" y="%d" font-size="11" fill="%s" text-anchor="end">%s</text>' % (y + 52, INK3, f['ap2']))
    a('<text x="340" y="%d" font-size="11" fill="%s" text-anchor="middle">%s</text>' % (y + 26, INK3, f['span']))
    a('<line x1="210" y1="%d" x2="470" y2="%d" stroke="%s" stroke-width="1" stroke-dasharray="3 3"/>' % (y + 36, y + 36, '#D3D1C7'))
    a('<circle cx="210" cy="%d" r="2.5" fill="%s"/>' % (y + 36, '#B4B2A9'))
    a('<circle cx="470" cy="%d" r="2.5" fill="%s"/>' % (y + 36, '#B4B2A9'))
    a('<text x="340" y="%d" font-size="11" fill="%s" text-anchor="middle">%s</text>' % (y + 52, INK3, f['stop']))

    a('<line x1="38" y1="%d" x2="642" y2="%d" stroke="%s" stroke-width="0.5" stroke-dasharray="2 3"/>' % (y + 64, y + 64, BORDER))
    a('<text x="38" y="%d" font-size="13" font-weight="500" fill="%s">%s</text>' % (y + 84, INK, f['no']))
    nx = 38 + len(f['no']) * 9 + 10
    a('<text x="%d" y="%d" font-size="12" fill="%s">%s</text>' % (nx, y + 84, INK2, f['air']))
    tx = nx + len(f['air']) * 13 + 10
    a('<rect x="%d" y="%d" width="%d" height="18" rx="4" fill="#F1EFE8"/>' % (tx, y + 71, len(f['plane']) * 11 + 12))
    a('<text x="%d" y="%d" font-size="11" fill="%s">%s</text>' % (tx + 6, y + 84, INK3, f['plane']))
    a('<text x="642" y="%d" font-size="11" fill="%s" text-anchor="end">%s</text>' % (y + 84, INK3, f['foot']))

    cx = 38
    for cls, price, disc in f['cabs']:
        biz = cls in ('公务舱', '头等舱')
        bg = GOLD_BG if biz else '#F1EFE8'
        fg = GOLD if biz else INK2
        cw = 146
        a('<rect x="%d" y="%d" width="%d" height="32" rx="8" fill="%s"/>' % (cx, y + 96, cw, bg))
        a('<text x="%d" y="%d" font-size="11" fill="%s">%s</text>' % (cx + 10, y + 116, fg, cls))
        a('<text x="%d" y="%d" font-size="15" font-weight="500" fill="%s">¥%d</text>' % (cx + 56, y + 117, INK, price))
        a('<text x="%d" y="%d" font-size="11" fill="%s" text-anchor="end">%s</text>' % (cx + cw - 10, y + 116, fg, disc))
        cx += cw + 8
    y += STEP

a('<text x="20" y="%d" font-size="11" fill="%s">价格来自同程（匿名可查），含机建燃油；以下单页为准。</text>' % (y + 6, INK3))
a('</svg>')

svg = '\n'.join(o)
io.open('preview_flight_list.svg', 'w', encoding='utf-8').write(svg)
print('ok len', len(svg), 'total h', 100 + len(flights) * STEP + 40)
