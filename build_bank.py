# -*- coding: utf-8 -*-
"""从 Word 题库解析出 {题干: 答案字母}，输出 bank.json"""
import docx, json, re, sys

SRC = r'D:/文档/xwechat_files/wxid_f4zwr5ghrpli32_a9be/temp/RWTemp/2026-10/00e4b785cfe673eee5359159f9c2eb02/南京理工大学预防艾滋病答题题库(2024).docx'

NUM_HEAD = re.compile(r'^\s*(\d{1,3})\s*[.、．]?\s*(.+)$', re.S)
TAIL_ANS = re.compile(r'^(.*?)([A-Da-d])\s*$', re.S)

def split_options(text):
    """把一段里粘连的选项拆开，返回 [(字母, 内容)]"""
    text = text.replace('\u3000', ' ')
    parts = re.split(r'(?:(?<=^)|(?<=[\n]))\s*([A-DＡ-Ｄ])\s*[、.．:：]\s*', text)
    out = []
    if len(parts) > 1:
        for i in range(1, len(parts) - 1, 2):
            code = parts[i].upper().replace('Ａ','A').replace('Ｂ','B').replace('Ｃ','C').replace('Ｄ','D')
            body = parts[i + 1].strip().strip('\n').strip()
            out.append((code, body))
    return out

def parse():
    d = docx.Document(SRC)
    ps = [p.text.strip() for p in d.paragraphs]
    items, i = [], 0
    while i < len(ps):
        line = ps[i]
        if not line:
            i += 1; continue
        m = NUM_HEAD.match(line)
        if m and len(m.group(2)) > 6:
            stem_raw = m.group(2)
            ans = None
            tm = TAIL_ANS.match(stem_raw)
            if tm and len(tm.group(1)) > 4:
                stem, ans = tm.group(1), tm.group(2).upper()
            else:
                stem = stem_raw
            # 收集紧随的选项段（可能多段）
            opts, j = [], i + 1
            while j < len(ps) and j <= i + 5:
                nxt = ps[j]
                if not nxt:
                    if opts: break
                    j += 1; continue
                if NUM_HEAD.match(nxt) and len(NUM_HEAD.match(nxt).group(2)) > 6:
                    break
                if nxt in ('窗体顶端', '窗体底端') or nxt.endswith('单选'):
                    break
                got = split_options(nxt)
                if got:
                    opts.extend(got)
                    j += 1
                    if len(opts) >= 4: break
                elif opts and not nxt.rstrip().endswith('。'):
                    # 无字母前缀的续行（如“淋病”）
                    opts.append((chr(65 + len(opts)), nxt))
                    j += 1
                else:
                    break
            if ans and len(opts) >= 2:
                items.append({'num': int(m.group(1)), 'stem': stem.strip(), 'answer': ans, 'options': opts})
            i = j
        else:
            i += 1
    return items

def norm(s):
    s = re.sub(r'[\s，。、,．.？?！!；;：:“”"\'’‘（）()《》\-—_]', '', s)
    return s

if __name__ == '__main__':
    items = parse()
    seen, uniq = set(), []
    for it in items:
        k = norm(it['stem'])
        if len(k) < 8 or k in seen: continue
        seen.add(k); uniq.append(it)
        it['key'] = k
    json.dump(uniq, open('bank.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('解析题目', len(items), '去重后', len(uniq))
    from collections import Counter
    print(Counter(it['answer'] for it in uniq))
    for it in uniq[:3]:
        print(it['num'], it['stem'][:40], '->', it['answer'], it['options'][:2])
