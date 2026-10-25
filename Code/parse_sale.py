"""批量解析链家「二手房」列表页 HTML，自动生成 CSV。

用法：
    python3 Code/parse_sale.py                    # 扫描项目根目录下所有 html
    python3 Code/parse_sale.py 目录A 目录B          # 只扫描指定目录（可传多个）

规则：
    - 只处理含 ul.sellListContent 的二手房列表页，其他 html 自动跳过
    - 所有地区、所有分页合并为一张表，按房源编号去重
    - CSV 统一输出到项目根目录的 output/二手房源明细.csv
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
OUT_NAME = '二手房源明细.csv'


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


def is_sale_page(soup):
    ul = soup.select_one('ul.sellListContent')
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


def extract_district(soup, city):
    """城区：优先筛选栏选中的行政区，其次从页面标题（城市之后）提取 XX区/县/市"""
    for a in soup.select('div.position a.selected') + soup.select('.position a.selected'):
        text = a.get_text(strip=True)
        if text and text not in ('区域', '不限') and re.search(r'(区|县|市)$', text):
            return text
    title = soup.title.get_text() if soup.title else ''
    tail = title.split(city, 1)[1] if city and city in title else title
    m = re.match(r'^([\u4e00-\u9fa5]{2,5}?(?:区|县|市))', tail)
    return m.group(1) if m else ''


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


def parse_item(li, page, idx, city, district, host):
    r = {'列表页': page, '列表位次': idx, '城市': city, '城区': district}

    a = li.select_one('.title a')
    title = a.get_text(strip=True) if a else ''
    r['标题'] = title
    href = a['href'] if a else ''
    r['房源链接'] = href if href.startswith('http') else f'https://{host}.lianjia.com{href}'
    r['房源编号'] = a.get('data-housecode', '') if a else ''
    tm = re.match(r'^(.+?)\s+(\d+室\d*厅)\s+(.+)$', title)
    r['标题小区'], r['标题户型'], r['标题朝向'] = tm.groups() if tm else ('', '', '')

    span = li.select_one('.title span')
    r['好房标签'] = span.get_text(strip=True) if span else ''

    pos = li.select('.positionInfo a')
    r['小区'] = pos[0].get_text(strip=True) if len(pos) > 0 else ''
    r['商圈'] = pos[1].get_text(strip=True) if len(pos) > 1 else ''

    info = li.select_one('.houseInfo')
    info_text = info.get_text() if info else ''
    parts = [x.strip() for x in info_text.split('|')] if info else []
    r['户型'] = parts[0] if parts else ''
    lm = re.search(r'(\d+)室(\d*)厅', r['户型'])
    r['室'], r['厅'] = (lm.group(1), lm.group(2) or '0') if lm else ('', '')
    am = re.search(r'([\d.]+)平米', info_text)
    r['建筑面积(㎡)'] = am.group(1) if am else ''

    r['朝向'] = r['装修'] = r['楼层位置'] = r['总楼层'] = r['建筑年代'] = r['建筑类型'] = ''
    for p in parts[1:]:
        if re.search(r'[东南西北]{1,2}$', p) and '室' not in p and '年' not in p:
            r['朝向'] = p
        elif p in ('精装', '简装', '毛坯', '其他', '精装修'):
            r['装修'] = p
        elif '楼层' in p or re.match(r'^\d+层$', p):
            fm = re.search(r'(低楼层|中楼层|高楼层|地下室|顶层).*?共(\d+)层', p)
            if fm:
                r['楼层位置'], r['总楼层'] = fm.group(1), fm.group(2)
            elif re.match(r'^\d+层$', p):
                r['总楼层'] = p.replace('层', '')
        elif re.fullmatch(r'\d{4}年', p):
            r['建筑年代'] = p.replace('年', '')
        elif p:
            r['建筑类型'] = p

    fi = li.select_one('.followInfo')
    ftxt = fi.get_text(strip=True) if fi else ''
    gm = re.search(r'(\d+)人关注', ftxt)
    r['关注人数'] = gm.group(1) if gm else ''
    pm = re.search(r'/\s*(.+?)发布', ftxt)
    r['发布时长'] = pm.group(1) if pm else ''
    est = ''
    if pm:
        t = pm.group(1)
        if '天' in t:
            est = re.search(r'(\d+)', t).group(1)
        elif '个月' in t:
            est = str(int(re.search(r'(\d+)', t).group(1)) * 30)
        elif '一年' in t:
            est = '365'
    r['发布时长(天,估算)'] = est

    tag_spans = li.select('.tag span')
    tag_texts = [s.get_text(strip=True) for s in tag_spans]
    tag_cls = [s.get('class')[0] for s in tag_spans if s.get('class')]
    r['标签汇总'] = ' / '.join(tag_texts)
    r['是否近地铁'] = '是' if 'subway' in tag_cls else '否'
    r['是否VR房源'] = '是' if 'vr' in tag_cls else '否'
    r['是否VR看装修'] = '是' if 'isVrFutureHome' in tag_cls else '否'
    r['是否房本满五年'] = '是' if 'taxfree' in tag_cls else '否'
    r['是否房本满两年'] = '是' if 'five' in tag_cls else '否'
    r['是否随时看房'] = '是' if 'haskey' in tag_cls else '否'

    tp = li.select_one('.totalPrice')
    r['总价(万元)'] = re.sub(r'[^\d.]', '', tp.get_text()) if tp else ''
    up = li.select_one('.unitPrice')
    unit = up.get('data-price', '') if up else ''
    if not unit and r['总价(万元)'] and r['建筑面积(㎡)']:
        unit = round(float(r['总价(万元)']) * 10000 / float(r['建筑面积(㎡)']))
    r['单价(元/㎡)'] = unit

    img = li.select_one('a.noresultRecommend img.lj-lazy')
    r['封面图'] = (img.get('data-original') or img.get('src')) if img else ''
    return r


def main():
    files = find_html(sys.argv[1:])
    groups = defaultdict(list)
    scanned = 0
    for f in files:
        html = open(f, encoding='utf-8', errors='ignore').read()
        soup = BeautifulSoup(html, 'html.parser')
        if not is_sale_page(soup):
            continue
        scanned += 1
        groups[os.path.dirname(f)].append((page_no(f), f, soup, html))

    if not groups:
        print('未找到二手房列表页（需包含 ul.sellListContent）')
        return

    cols = ['列表页', '列表位次', '房源编号', '房源链接', '标题',
            '标题小区', '标题户型', '标题朝向', '城市', '城区', '商圈', '小区',
            '户型', '室', '厅', '建筑面积(㎡)', '朝向', '装修', '楼层位置', '总楼层',
            '建筑年代', '建筑类型', '总价(万元)', '单价(元/㎡)', '关注人数', '发布时长',
            '发布时长(天,估算)', '是否近地铁', '是否VR房源', '是否VR看装修',
            '是否房本满五年', '是否房本满两年', '是否随时看房', '好房标签', '标签汇总', '封面图']

    rows = []
    for folder, entries in sorted(groups.items()):
        for page, path, soup, html in sorted(entries, key=lambda x: (x[0], x[1])):
            city = extract_city(soup)
            district = extract_district(soup, city)
            host = extract_host(html)
            ul = soup.select_one('ul.sellListContent')
            for idx, li in enumerate(ul.find_all('li', recursive=False)):
                rows.append(parse_item(li, page, idx, city, district, host))

    df = pd.DataFrame(rows)
    df = df[cols]
    # 分页边界常有同一房源重复出现
    df = df.drop_duplicates(subset='房源编号', keep='first').reset_index(drop=True)
    df.insert(0, '序号', range(1, len(df) + 1))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, OUT_NAME)
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f'共扫描 {scanned} 个二手房列表页 / {len(df)} 条房源 -> {out}')


if __name__ == '__main__':
    main()
