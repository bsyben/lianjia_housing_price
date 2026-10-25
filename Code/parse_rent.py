"""批量解析链家/贝壳「出租」列表页 HTML，自动生成 CSV。

用法：
    python3 Code/parse_rent.py                    # 扫描项目根目录下所有 html
    python3 Code/parse_rent.py 目录A 目录B          # 只扫描指定目录（可传多个）

规则：
    - 只处理含 div.content__list--item 的出租列表页，其他 html 自动跳过
    - 所有地区、所有分页合并为一张表，按房源编号去重
    - CSV 统一输出到项目根目录的 output/出租房源明细.csv
"""
import os
import re
import sys
from collections import defaultdict

import pandas as pd
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(DEFAULT_ROOT, 'output')
OUT_NAME = '出租房源明细.csv'

TAG_MAP = {
    'gov_certification': '是否权属核验',
    'ziying_sxz': '是否自营省心租',
    'is_new': '是否新上',
    'rent_period_month': '是否月租',
    'is_subway_house': '是否近地铁',
    'decoration': '是否精装',
    'deposit_1_pay_1': '是否押一付一',
    'is_key': '是否随时看房',
    'first_rent': '是否首次出租',
    'two_bathroom': '是否双卫生间',
}


def find_html(paths):
    """递归收集 html 文件；未指定路径时扫描项目根目录"""
    targets = paths or [DEFAULT_ROOT]
    files = []
    for p in targets:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            files.append(p)
            continue
        for root, _, names in os.walk(p):
            for name in names:
                if name.lower().endswith(('.html', '.htm')):
                    files.append(os.path.join(root, name))
    return files


def is_rent_page(soup):
    return soup.select_one('div.content__list--item') is not None


def extract_city(soup):
    """城市：优先 JS 里的 cityName，其次页头「成都租房」这类城市入口链接"""
    m = re.search(r"cityName:\s*'([^']+)'", str(soup))
    if m:
        return m.group(1)
    for a in soup.find_all('a', href=True):
        if re.match(r'^(//|https?://)[a-z]+\.lianjia\.com(/[a-z]*)?/?$', a['href']):
            text = a.get_text(strip=True)
            m2 = re.match(r'^(.{2,6}?)(租房|二手房)$', text)
            if m2:
                return m2.group(1)
    return ''


def extract_host(html, default='cd'):
    """城市站点域名前缀：cd / bj / sh ..."""
    hits = re.findall(r'(?:https?:)?//([a-z]+)\.lianjia\.com', html)
    hits = [h for h in hits if h not in ('s1', 'www', 'image1', 'image', 'ajax')]
    return max(set(hits), key=hits.count) if hits else default


def page_no(path):
    """分页序号：xxx-2.html / xxx第2页.html / xxx_2.html，均无则为第1页"""
    name = os.path.basename(path)
    for pat in (r'-(\d+)\.html?$', r'第(\d+)页', r'_(\d+)\.html?$'):
        m = re.search(pat, name)
        if m:
            return int(m.group(1))
    return 1


def parse_des(des):
    """从描述行拆出 城区/商圈/小区/面积/朝向/户型/楼层"""
    out = dict.fromkeys(['城区', '商圈', '小区', '面积', '朝向', '户型', '楼层'], '')
    if des is None:
        return out
    anchors = [a.get_text(strip=True) for a in des.select('a')]
    for key, val in zip(['城区', '商圈', '小区'], anchors[:3]):
        out[key] = val

    tokens = [t.strip() for t in des.get_text(separator='|').split('|')
              if t.strip() and t.strip() not in ('/', '-', '·')]
    # 去掉已由 <a> 承载的城区/商圈/小区
    for a_text in anchors[:3]:
        if a_text in tokens:
            tokens.remove(a_text)
    for t in tokens:
        if '㎡' in t or '平米' in t:
            out['面积'] = re.sub(r'[^\d.]', '', t)
        elif re.search(r'\d+室\d*厅', t):
            out['户型'] = t
        elif '楼层' in t or re.search(r'[（(]\s*\d+\s*层', t):
            out['楼层'] = t
        elif re.fullmatch(r'[东南西北/ ]{1,20}', t):
            out['朝向'] = t
    return out


def parse_item(item, page, city, host):
    r = {'列表页': page, '城市': city}
    r['房源编号'] = item.get('data-house_code', '')
    r['列表位次'] = item.get('data-position', '')

    title_a = item.select_one('p.content__list--item--title a')
    title = title_a.get_text(strip=True) if title_a else ''
    r['标题'] = title
    href = title_a['href'] if title_a else ''
    r['房源链接'] = href if href.startswith('http') else f'https://{host}.lianjia.com{href}'

    tags_html = item.select('p.content__list--item--bottom i')
    tag_texts = [t.get_text(strip=True) for t in tags_html]
    tag_classes = [c.replace('content__item__tag--', '')
                   for t in tags_html for c in t.get('class', [])
                   if c.startswith('content__item__tag--')]
    # 独栋/集中式公寓是广告卡片，没有城区、商圈、小区、楼层、朝向等字段
    is_apartment = '独栋公寓' in tag_texts or title.startswith('独栋·')
    r['房源类型'] = item.get('data-bu_desc', '') or ('独栋公寓' if is_apartment else '')
    r['是否独栋公寓'] = '是' if is_apartment else '否'
    r['标签汇总'] = ' / '.join(tag_texts)
    for key, col in TAG_MAP.items():
        r[col] = '是' if key in tag_classes else '否'

    aside = item.select_one('a.content__list--item--aside')
    img = aside.select_one('img') if aside else None
    r['封面图'] = (img.get('data-src') or img.get('src')) if img else ''
    r['是否有VR'] = '是' if aside and aside.select_one('i.vr-logo') else '否'
    r['是否省心租标识'] = '是' if aside and aside.select_one('i.sxz-logo') else '否'

    badge = item.select_one('p.content__list--item--title img')
    r['好房标签'] = badge.get('alt', '') if badge else ''
    r['是否跃层'] = '是' if '跃层' in title else '否'

    m = re.match(r'^(整租|合租)·(.+?)\s+(\d+室\d*厅(?:\s*跃层)?)\s+(.+)$', title)
    if m:
        r['租赁方式'] = m.group(1)
        r['标题小区'] = m.group(2)
        r['标题户型'] = m.group(3).replace(' ', '')
        r['标题朝向'] = m.group(4)
    elif title.startswith('独栋·'):
        last = title.rsplit(' ', 1)[-1]
        r['租赁方式'] = '独栋'
        r['标题小区'] = ''
        r['标题户型'] = last if re.fullmatch(r'开间|\d+室\d*厅', last) else ''
        r['标题朝向'] = ''
    else:
        r['租赁方式'] = r['标题小区'] = r['标题户型'] = r['标题朝向'] = ''

    des = parse_des(item.select_one('p.content__list--item--des'))
    r['城区'] = des['城区']
    r['商圈'] = des['商圈']
    r['小区'] = des['小区']
    r['建筑面积(㎡)'] = des['面积']
    r['朝向'] = des['朝向']
    r['户型'] = des['户型']
    r['朝向数量'] = len([x for x in re.split(r'[ /]+', des['朝向']) if x])

    lm = re.search(r'(\d+)室(\d*)厅(?:(\d+)卫)?', des['户型'])
    if lm:
        r['室'], r['厅'], r['卫'] = lm.group(1), lm.group(2) or '0', lm.group(3) or ''
    else:
        r['室'] = r['厅'] = r['卫'] = ''

    fm = re.search(r'(低楼层|中楼层|高楼层|地下室|顶层)\s*[（(]\s*(\d+)\s*层\s*[)）]', des['楼层'])
    if fm:
        r['楼层位置'], r['总楼层'] = fm.group(1), fm.group(2)
    else:
        r['楼层位置'] = des['楼层']
        tf = re.search(r'(\d+)\s*层', des['楼层'])
        r['总楼层'] = tf.group(1) if tf else ''

    brand = item.select_one('p.content__list--item--brand span.brand')
    r['品牌'] = brand.get_text(strip=True) if brand else ''
    tm = item.select_one('p.content__list--item--brand span.content__list--item--time')
    maint = tm.get_text(strip=True) if tm else ''
    r['维护时间'] = maint
    dm = re.search(r'(\d+)\s*天前维护', maint)
    r['维护距今天数'] = dm.group(1) if dm else ('0' if '今天' in maint else '')

    rank = item.select_one('span.content__list--item--rankboard-desc')
    r['政府平台核验备注'] = rank.get_text(strip=True) if rank else ''
    r['是否政府平台核验'] = '是' if rank else '否'

    price = item.select_one('span.content__list--item-price em')
    p = price.get_text(strip=True) if price else ''
    r['月租金(元)'] = p
    try:
        r['单位面积月租(元/㎡)'] = round(float(p) / float(r['建筑面积(㎡)']), 2)
    except (ValueError, ZeroDivisionError):
        r['单位面积月租(元/㎡)'] = ''
    return r


def main():
    files = find_html(sys.argv[1:])
    groups = defaultdict(list)
    scanned = 0
    for f in files:
        html = open(f, encoding='utf-8', errors='ignore').read()
        soup = BeautifulSoup(html, 'html.parser')
        if not is_rent_page(soup):
            continue
        scanned += 1
        groups[os.path.dirname(f)].append((page_no(f), f, soup, html))

    if not groups:
        print('未找到出租列表页（需包含 div.content__list--item）')
        return

    cols = ['列表页', '列表位次', '房源编号', '房源链接', '标题', '租赁方式', '房源类型',
            '是否独栋公寓', '城市', '城区', '商圈', '小区', '建筑面积(㎡)', '朝向', '朝向数量',
            '户型', '室', '厅', '卫',
            '楼层位置', '总楼层', '月租金(元)', '单位面积月租(元/㎡)',
            '是否权属核验', '是否政府平台核验', '政府平台核验备注', '是否自营省心租',
            '是否省心租标识', '是否新上', '是否月租', '是否近地铁', '是否精装',
            '是否押一付一', '是否随时看房', '是否首次出租', '是否双卫生间', '标签汇总',
            '品牌', '维护时间', '维护距今天数', '是否有VR', '好房标签', '是否跃层',
            '标题小区', '标题户型', '标题朝向', '封面图']

    rows = []
    for folder, entries in sorted(groups.items()):
        for page, path, soup, html in sorted(entries, key=lambda x: (x[0], x[1])):
            city = extract_city(soup)
            host = extract_host(html)
            page_rows = [parse_item(item, page, city, host)
                         for item in soup.select('div.content__list--item')]
            # 独栋公寓广告卡没有城区/商圈链路，用同页普通房源的众数补齐
            districts = [x['城区'] for x in page_rows if x['城区']]
            areas = [x['商圈'] for x in page_rows if x['商圈']]
            d_mode = max(set(districts), key=districts.count) if districts else ''
            a_mode = max(set(areas), key=areas.count) if areas else ''
            for x in page_rows:
                if not x['城区']:
                    x['城区'] = d_mode
                if not x['商圈']:
                    x['商圈'] = a_mode
            rows.extend(page_rows)

    df = pd.DataFrame(rows)
    df = df[cols]
    # 分页边界常有同一房源重复出现
    df = df.drop_duplicates(subset='房源编号', keep='first').reset_index(drop=True)
    df.insert(0, '序号', range(1, len(df) + 1))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, OUT_NAME)
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'共扫描 {scanned} 个出租列表页 / {len(df)} 条房源 -> {out}')


if __name__ == '__main__':
    main()
