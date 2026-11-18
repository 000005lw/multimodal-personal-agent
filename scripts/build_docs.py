"""把 README.md 渲染成自带样式的单文件 HTML。

用途：DSH 客户端预览 markdown 时，代码块/行内代码会出现白底白字（主题 token
没吃进深色值），导致文档不可读。这个脚本生成一个不依赖任何外部样式的 HTML，
直接用浏览器打开就能看。

用法：
    python build_docs.py            # README.md -> README.html
    python build_docs.py 别的.md
"""

import sys
from pathlib import Path

import markdown

BASE_DIR = Path(__file__).resolve().parents[1]

# 深色样式：代码块深底浅字，与「白底白字」彻底无关
STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 48px 24px 96px;
  background: #0f1115; color: #e6e8eb;
  font: 15px/1.75 -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
}
main { max-width: 860px; margin: 0 auto; }
h1, h2, h3, h4 { line-height: 1.35; margin: 1.8em 0 .7em; }
h1 { font-size: 30px; margin-top: 0; padding-bottom: .4em; border-bottom: 1px solid #262d36; }
h2 { font-size: 22px; padding-bottom: .3em; border-bottom: 1px solid #1c222a; }
h3 { font-size: 17px; }
a { color: #6cb6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: none; border-top: 1px solid #262d36; margin: 2em 0; }
p { margin: .9em 0; }
strong { color: #fff; font-weight: 600; }

/* 代码块：深底 + 浅字，这是我这边唯一要保证的事 */
pre {
  background: #161b22; border: 1px solid #262d36; border-radius: 8px;
  padding: 16px 18px; overflow-x: auto; margin: 1.2em 0;
  font: 13px/1.65 "Cascadia Code", Consolas, "Courier New", monospace;
  color: #d6deeb;
}
pre code { background: none; border: none; padding: 0; color: inherit; font-size: inherit; }

/* 行内代码：浅一点的中灰底，文字保持浅色 */
code {
  background: #232b35; border: 1px solid #2e3843; border-radius: 4px;
  padding: 1px 6px; color: #ffd8a8;
  font: 13px/1.5 "Cascadia Code", Consolas, "Courier New", monospace;
}

/* 表格 */
table { border-collapse: collapse; width: 100%; margin: 1.2em 0; font-size: 14px; }
th, td { border: 1px solid #262d36; padding: 8px 12px; text-align: left; vertical-align: top; }
th { background: #171d25; font-weight: 600; color: #fff; }
tr:nth-child(even) td { background: #131820; }

blockquote {
  margin: 1.2em 0; padding: .2em 0 .2em 16px;
  border-left: 3px solid #3a4a5c; color: #a9b4c0;
}
ul, ol { padding-left: 24px; }
li { margin: .35em 0; }

.tip {
  margin: 32px 0 0; padding: 12px 16px; border-radius: 8px;
  background: #14202b; border: 1px solid #24486b; color: #a8c8e8; font-size: 13px;
}
"""

TIP = (
    '<div class="tip">这份 HTML 由 build_docs.py 从 README.md 生成。'
    "内容与 README.md 一致，只是不依赖客户端主题，所以代码块一定是深底浅字。"
    "改完 README.md 后重新跑一次 python build_docs.py 即可更新。</div>"
)


def convert(src: Path, dst: Path) -> int:
    text = src.read_text(encoding="utf-8")
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format="html",
    )
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{src.stem}</title><style>{STYLE}</style></head>\n"
        f"<body><main>{body}{TIP}</main></body></html>\n"
    )
    dst.write_text(html, encoding="utf-8")
    return len(html)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "README.md"
    src = BASE_DIR / name
    if not src.exists():
        print(f"找不到 {src}")
        return 1
    dst = src.with_suffix(".html")
    size = convert(src, dst)
    print(f"已生成 {dst.name}（{size} 字节）")
    print(f"用浏览器打开：{dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
