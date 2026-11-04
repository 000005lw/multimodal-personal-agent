"""批量做歌库：把 songs.txt 里的歌一首首做好，塞进服务器曲库。

跑一次之后，就算家里电脑不开，她也能唱这些歌。

用法（双击 做歌库.bat，或者命令行）：
    python build_library.py            做 songs.txt 里所有还没做的
    python build_library.py 起风了 晴天   只做指定的几首
    python build_library.py --list      看服务器曲库里有什么

需要先开着 SSH 隧道（做歌库.bat 会自动开）。
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import home_agent  # noqa: E402
import sing  # noqa: E402

BASE = home_agent.BASE
TOKEN = home_agent.TOKEN
SONG_LIST = Path(__file__).with_name("songs.txt")

# 和服务器 songlib.norm 保持一致，用来判断"这首做过了没有"
_STRIP = re.compile(r"[《》〈〉「」『』\"'“”‘’()（）\[\]【】{}\s\-_·、,，.。!！?？:：;；/\\]")
_TAIL = re.compile(r"(翻唱|cover|原唱|完整版|片段|无损|高音质)$")


def norm(text):
    return _TAIL.sub("", _STRIP.sub("", (text or "").strip().lower()))


def _request(path, payload=None, method=None, timeout=60):
    url = f"{BASE}{path}"
    body = None
    headers = {"X-Home-Token": TOKEN}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=body, headers=headers, method=method or ("POST" if body else "GET")
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def library_info() -> tuple[set, int]:
    """服务器曲库里已有的歌名（归一化后），以及真实首歌数。"""
    try:
        data = _request("/songs/index", timeout=20)
    except Exception as exc:
        print(f"  [!] 拿曲库清单失败：{exc}")
        return set(), 0
    names = set()
    for item in data.get("songs") or []:
        for value in (item.get("query"), item.get("title")):
            key = norm(value or "")
            if key:
                names.add(key)
    return names, int(data.get("count") or len(data.get("songs") or []))


def library_names() -> set:
    return library_info()[0]


def upload(query, title, audio_b64, ext="mp3", segment=0, total=1, timeout=180):
    return _request(
        "/songs/upload",
        {
            "query": query,
            "title": title,
            "audio": audio_b64,
            "ext": ext,
            "segment": segment,
            "total": total,
        },
        timeout=timeout,
    )


def read_song_list():
    if not SONG_LIST.exists():
        print(f"[!] 找不到歌单 {SONG_LIST}")
        return []
    songs = []
    for line in SONG_LIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            songs.append(line)
    return songs


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    have, total_before = library_info()
    print(f"曲库现有 {total_before} 首")

    if "--list" in flags:
        try:
            data = _request("/songs/index", timeout=20)
            for item in data.get("songs") or []:
                print(f"    {item.get('query') or item.get('title')}")
        except Exception as exc:
            print(f"  [!] 拿清单失败：{exc}")
        return

    songs = args or read_song_list()
    if not songs:
        print("没有要做的歌")
        return

    ffmpeg = home_agent.find_ffmpeg()
    if not ffmpeg:
        print("[!] 找不到 ffmpeg，做不了")
        return

    redo = "--redo" in sys.argv
    if redo:
        print("[!] --redo：库里已有的歌也重做（补全后面的段）\n")
        todo = list(songs)
    else:
        todo = [s for s in songs if norm(s) not in have]
    print(f"计划：{len(songs)} 首，其中 {len(todo)} 首还没做\n")

    done = failed = 0
    start = time.time()
    for index, name in enumerate(todo, 1):
        print(f"[{index}/{len(todo)}] {name}")
        # 先整首准备好（下载 + 分离），之后每段只做 RVC
        try:
            prepared = sing.prepare_song(name, ffmpeg)
        except Exception as exc:
            print(f"  [!] 出错：{type(exc).__name__} {exc}")
            failed += 1
            continue

        total = int(prepared.get("total") or 0)
        if total <= 0:
            print(f"  [!] 失败：{prepared.get('error')}")
            failed += 1
            continue

        print(f"  共 {total} 段，开始逐段做")
        saved = 0
        for seg in range(total):
            try:
                result = sing.make_segment(name, ffmpeg, index=seg)
            except Exception as exc:
                print(f"    [!] 第 {seg + 1} 段出错：{exc}")
                break
            if not result.get("audio"):
                print(f"    [!] 第 {seg + 1} 段失败：{result.get('error')}")
                break
            try:
                upload(
                    name,
                    result.get("title") or name,
                    result["audio"],
                    result.get("ext") or "mp3",
                    segment=seg,
                    total=total,
                )
                saved += 1
                print(f"    ✓ 第 {seg + 1}/{total} 段已入库")
            except Exception as exc:
                print(f"    [!] 第 {seg + 1} 段上传失败：{exc}")
                break
        if saved:
            done += 1
            print(f"  ✓ 《{name}》{saved}/{total} 段（共 {library_info()[1]} 首）")
        else:
            failed += 1
        time.sleep(1)

    cost = time.time() - start
    _, total = library_info()
    print("\n" + "=" * 46)
    print(f"  完成 {done} 首，失败 {failed} 首，用时 {cost/60:.1f} 分钟")
    print(f"  曲库现在共 {total} 首")
    print("=" * 46)


if __name__ == "__main__":
    main()
