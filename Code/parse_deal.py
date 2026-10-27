"""批量解析链家「成交记录」列表页 HTML，自动生成 CSV。

用法：
    python3 Code/parse_deal.py                    # 默认扫描 华西二手房/成交记录
    python3 Code/parse_deal.py 目录A 目录B          # 只扫描指定目录（可传多个）

规则：
    - 只处理含 ul.listContent 的成交列表页，其他 html 自动跳过
    - 所有板块、所有分页合并为一张表，按房源编号去重
    - CSV 统一输出到项目根目录的 output/成交房源明细.csv
"""
import os
import re
import sys
from collections import defaultdict

import pandas as pd
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_TARGET = os.path.join(DEFAULT_ROOT, '华西二手房', '成交记录')
OUTPUT_DIR = os.path.join(DEFAULT_ROOT, 'output')
OUT_NAME = '成交房源明细.csv'


def find_html(paths):
    """递归收集 html 文件；未指定路径时扫描默认成交记录目录"""
    targets = paths or [DEFAULT_TARGET]
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


def is_deal_page(soup):
    ul = soup.select_one('ul.listContent')
    return bool(ul and ul.find('li'))


def extract_city(soup):
    """城市：优先 JS 里的 cityName，其次页头「成都二手房」这类城市入口链接"""
    m = re.search(r"cityName:\s*'([^']+)'", str(soup))
    if m:
        return m.group(1)
    for a in soup.find_all('a', href=True):
        if re.match(r'^(//|https?://)[a-z]+\.lianjia\.com(/[a-z]*)?/?$', a['href']):
            m2 = re.match(r'^(.{2,6}?)(二手房|租房)$', a.get_text(strip=True))
            if m2:
                return m2.group(1)
    return ''


def extract_region(soup):
    """城区/商圈：取筛选栏选中的行政区与板块（如 武侯区 / 华西）"""
    texts = [a.get_text(strip=True) for a in soup.select('a.selected')]
    texts = [t for t in texts if t]
    district = next((t for t in texts if re.search(r'(区|县|市)$', t)), '')
    area = ''
    if district in texts:
        idx = texts.index(district)
        area = texts[idx + 1] if idx + 1 < len(texts) else ''
    return district, area


def extract_host(html, default='cd'):
    """城市站点域名前缀：cd / bj / sh ..."""
    hits = re.findall(r'(?:https?:)?//([a-z]+)\.lianjia\.com', html)
    hits = [h for h in hits if h not in ('s1', 'www', 'image1', 'image', 'ajax')]
    return max(set(hits), key=hits.count) if hits else default


def page_no(path):
    """分页序号：xxx第2页 / xxx-2.html / xxx_2.html，均无则为第1页"""
    name = os.path.basename(path)
    for pat in (r'第(\d+)页', r'-(\d+)\.html?$', r'_(\d+)\.html?$'):
        m = re.search(pat, name)
        if m:
            return int(m.group(1))
    return 1


def parse_item(li, page, idx, city, district, area, host):
    r = {'列表页': page, '列表位次': idx, '城市': city, '城区': district, '商圈': area}

    a = li.select_one('.title a')
    title = a.get_text(strip=True) if a else ''
    r['标题'] = title
    href = a['href'] if a else ''
    if not href:
        img_a = li.select_one('a.img')
        href = img_a['href'] if img_a and img_a.get('href') else ''
    r['房源链接'] = href if href.startswith('http') else f'https://{host}.lianjia.com{href}'
    cm = re.search(r'/chengjiao/(\d+)\.html', href)
    r['房源编号'] = cm.group(1) if cm else ''

    # 标题形如「小区 2室1厅 88.5平米」，少数为「小区 2房间1卫 62.7平米」
    tm = re.match(r'^(.+?)\s+((?:\d+室\d*厅|\d+房间?\d*[卫厅]?|开间))\s+([\d.]+)平米$', title)
    if tm:
        r['小区'], r['户型'], r['建筑面积(㎡)'] = tm.groups()
    else:
        r['小区'] = r['户型'] = r['建筑面积(㎡)'] = ''
        am = re.search(r'([\d.]+)\s*平米', title)
        r['建筑面积(㎡)'] = am.group(1) if am else ''
    lm = re.search(r'(\d+)室(\d*)厅', r['户型'])
    if lm:
        r['室'], r['厅'] = lm.group(1), lm.group(2) or '0'
    else:
        fm2 = re.search(r'(\d+)房间?', r['户型'])
        r['室'], r['厅'] = (fm2.group(1), '0') if fm2 else ('', '')

    info = li.select_one('.houseInfo')
    parts = [x.strip() for x in info.get_text().split('|')] if info else []
    parts = [p for p in parts if p]
    r['朝向'] = parts[0] if len(parts) > 0 else ''
    r['装修'] = parts[1] if len(parts) > 1 else ''

    pos = li.select_one('.positionInfo')
    ptxt = pos.get_text(strip=True) if pos else ''
    fm = re.search(r'(低楼层|中楼层|高楼层|地下室|顶层)?\s*[（(]?共(\d+)层[)）]?\s*(.*)', ptxt)
    if fm:
        r['楼层位置'], r['总楼层'], r['建筑类型'] = fm.group(1) or '', fm.group(2), fm.group(3)
    else:
        r['楼层位置'] = r['总楼层'] = r['建筑类型'] = ''

    dd = li.select_one('.dealDate')
    r['成交日期'] = dd.get_text(strip=True) if dd else ''

    tp = li.select_one('.totalPrice .number')
    r['成交总价(万元)'] = tp.get_text(strip=True) if tp else ''
    up = li.select_one('.unitPrice .number')
    r['成交单价(元/㎡)'] = up.get_text(strip=True) if up else ''

    tag_texts = [s.get_text(strip=True) for s in li.select('.dealHouseTxt span')]
    r['标签汇总'] = ' / '.join(tag_texts)
    r['是否满两年'] = '是' if '房屋满两年' in tag_texts else '否'
    r['是否满五年'] = '是' if '房屋满五年' in tag_texts else '否'
    r['是否近地铁'] = '是' if '近地铁' in tag_texts else '否'

    cycle_texts = [s.get_text(strip=True) for s in li.select('.dealCycleTxt span')]
    list_price = next((re.search(r'([\d.]+)', t).group(1)
                       for t in cycle_texts if '挂牌' in t and re.search(r'[\d.]+', t)), '')
    cycle = next((re.search(r'(\d+)', t).group(1)
                  for t in cycle_texts if '成交周期' in t and re.search(r'\d', t)), '')
    r['挂牌价(万元)'] = list_price
    r['成交周期(天)'] = cycle
    try:
        r['议价空间(万元)'] = round(float(list_price) - float(r['成交总价(万元)']), 1)
        r['议价幅度(%)'] = round((float(list_price) - float(r['成交总价(万元)']))
                             / float(list_price) * 100, 2)
    except (ValueError, ZeroDivisionError):
        r['议价空间(万元)'] = r['议价幅度(%)'] = ''

    agent = li.select_one('.agent_name')
    r['经纪人'] = agent.get_text(strip=True) if agent else ''

    img = li.select_one('a.img img')
    r['封面图'] = (img.get('data-original') or img.get('src')) if img else ''
    return r


def main():
    files = find_html(sys.argv[1:])
    groups = defaultdict(list)
    scanned = 0
    for f in files:
        html = open(f, encoding='utf-8', errors='ignore').read()
        soup = BeautifulSoup(html, 'html.parser')
        if not is_deal_page(soup):
            continue
        scanned += 1
        groups[os.path.dirname(f)].append((page_no(f), f, soup, html))

    if not groups:
        print('未找到成交列表页（需包含 ul.listContent）')
        return

    cols = ['列表页', '列表位次', '房源编号', '房源链接', '标题', '城市', '城区', '商圈',
            '小区', '户型', '室', '厅', '建筑面积(㎡)', '朝向', '装修', '楼层位置', '总楼层',
            '建筑类型', '成交日期', '成交总价(万元)', '成交单价(元/㎡)', '挂牌价(万元)',
            '议价空间(万元)', '议价幅度(%)', '成交周期(天)',
            '是否满两年', '是否满五年', '是否近地铁', '标签汇总', '经纪人', '封面图']

    rows = []
    for folder, entries in sorted(groups.items()):
        for page, path, soup, html in sorted(entries, key=lambda x: (x[0], x[1])):
            city = extract_city(soup)
            district, area = extract_region(soup)
            host = extract_host(html)
            ul = soup.select_one('ul.listContent')
            for idx, li in enumerate(ul.find_all('li', recursive=False)):
                if not li.select_one('.title a'):
                    continue
                rows.append(parse_item(li, page, idx, city, district, area, host))

    df = pd.DataFrame(rows, columns=cols)
    # 分页边界常有同一房源重复出现
    df = df.drop_duplicates(subset='房源编号', keep='first').reset_index(drop=True)
    df.insert(0, '序号', range(1, len(df) + 1))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, OUT_NAME)
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'共扫描 {scanned} 个成交列表页 / {len(df)} 条房源 -> {out}')


if __name__ == '__main__':
    main()
