#!/usr/bin/env python3
"""将中文 Markdown 交接资料汇成可离线查看/打印的 HTML，不修改原始日志。"""
from pathlib import Path
import html
import re

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'docs/zh'
NAMES=['交接报告','用例总表','编译指南','使用指南','设计指南','七天压测与日志交接','交接验证记录']


def inline(text):
    value=html.escape(text)
    value=re.sub(r'`([^`]+)`',r'<code>\1</code>',value)
    value=re.sub(r'\*\*([^*]+)\*\*',r'<strong>\1</strong>',value)
    value=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',lambda m:f'<a href="#{NAMES.index(Path(m[2]).stem)}">{m[1]}</a>' if Path(m[2]).stem in NAMES else m[1],value)
    return value


def render(text):
    result=[];code=[];fenced=False;table=False;listing=False;header=False
    def close():
        nonlocal table,listing
        if table:result.append('</tbody></table></div>');table=False
        if listing:result.append('</ul>');listing=False
    for line in text.splitlines():
        if line.startswith('```'):
            if fenced:result.append('<pre><code>'+html.escape('\n'.join(code))+'</code></pre>');code=[];fenced=False
            else:close();fenced=True
            continue
        if fenced:code.append(line);continue
        if line.startswith('|'):
            cells=[c.strip() for c in line.strip().strip('|').split('|')]
            if all(re.fullmatch(r':?-+:?',cell.replace(' ','')) for cell in cells):continue
            if not table:
                if listing:result.append('</ul>');listing=False
                result.append('<div class="table"><table><thead><tr>'+''.join('<th>'+inline(c)+'</th>' for c in cells)+'</tr></thead><tbody>');table=True
            else:result.append('<tr>'+''.join('<td>'+inline(c)+'</td>' for c in cells)+'</tr>')
            continue
        if table:result.append('</tbody></table></div>');table=False
        if not line.strip():
            if listing:result.append('</ul>');listing=False
            continue
        heading=re.match(r'^(#{1,6})\s+(.+)',line)
        if heading:
            if listing:result.append('</ul>');listing=False
            level=min(len(heading[1])+1,6);result.append(f'<h{level}>{inline(heading[2])}</h{level}>');continue
        item=re.match(r'^(?:[-*]|\d+\.)\s+(.+)',line)
        if item:
            if not listing:result.append('<ul>');listing=True
            result.append('<li>'+inline(item[1])+'</li>')
        else:
            if listing:result.append('</ul>');listing=False
            result.append('<p>'+inline(line)+'</p>')
    close();return '\n'.join(result)


def main():
    contents=[]
    for index,name in enumerate(NAMES):
        file=SOURCE/(name+'.md')
        contents.append(f'<section id="{index}">'+render(file.read_text(encoding='utf-8-sig'))+'</section>')
    toc=''.join(f'<li><a href="#{i}">{html.escape(name)}</a></li>' for i,name in enumerate(NAMES))
    document='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CPU GPU NPU 中文交接报告</title><style>
body{font:16px/1.8 "Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif;color:#17212b;max-width:1240px;margin:36px auto;padding:0 24px}h1,h2,h3,h4{color:#142a43;line-height:1.4}h1{font-size:30px}h2{font-size:26px;margin-top:36px}h3{font-size:21px;margin-top:30px}nav{border-bottom:1px solid #cbd5e1;padding-bottom:20px}nav ul{display:flex;gap:10px 28px;flex-wrap:wrap;padding-left:22px}a{color:#235c91}code{background:#eef2f6;padding:2px 5px;border-radius:3px;word-break:break-word}pre{padding:18px;background:#f3f5f8;border:1px solid #d9e0e7;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}pre code{padding:0;background:none}.table{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;margin:16px 0}th,td{border:1px solid #d5dde5;padding:10px 12px;vertical-align:top}th{background:#203b5b;color:white;text-align:left}tr:nth-child(even){background:#f3f6f9}section{margin-bottom:48px}p{margin:12px 0}@media print{body{max-width:none;font-size:10pt;margin:0;padding:0}nav{display:none}h2{break-before:page}h3{break-after:avoid}pre{font-size:8pt}.table{overflow:visible}table{font-size:8pt}thead{display:table-header-group}tr{break-inside:avoid}a{color:inherit;text-decoration:none}@page{size:A4;margin:16mm}}
</style></head><body><h1>CPU GPU NPU 中文交接报告</h1><p>用例原理、统一编译、使用与设计、七天执行与日志交接。实际板端记录尚待运行产生；不包含调压/Vmin结论。</p><nav><ul>'''+toc+'</ul></nav>'+''.join(contents)+'</body></html>'
    output=SOURCE/'交接附件/中文交接报告.html';output.parent.mkdir(exist_ok=True);output.write_text(document,encoding='utf-8')
    print(output)


if __name__=='__main__':main()
