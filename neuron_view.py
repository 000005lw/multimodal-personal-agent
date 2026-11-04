"""把记忆网画出来。

    python neuron_view.py            # 生成 data/memory-graph.html 并打印统计
    python neuron_view.py --open     # 生成后直接打开浏览器

配色按截图来：深空黑底、节点发光、按种类分色、边是淡蓝细线。
点一颗节点看原文；滚轮缩放、左键拖动转视角（3D 力导向图）。
"""

from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path

import neuron

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>小鲸鱼的记忆网</title>
<style>
  html,body{margin:0;height:100%;background:#05070d;color:#dfe8ff;
    font-family:"Microsoft YaHei",system-ui,sans-serif;overflow:hidden}
  #graph{width:100%;height:100%}
  #hud{position:fixed;left:18px;top:14px;font-size:13px;line-height:1.9;
    color:#9fb6dd;pointer-events:none;text-shadow:0 0 8px #000}
  #hud b{color:#8ef1c9;font-weight:600}
  #detail{position:fixed;right:18px;top:14px;width:300px;max-height:70vh;overflow:auto;
    background:rgba(10,16,28,.86);border:1px solid #1d2b45;border-radius:10px;
    padding:12px 14px;font-size:13px;line-height:1.8;display:none}
  #detail h3{margin:0 0 8px;font-size:13px;color:#8ef1c9;font-weight:600}
  #detail .kv{color:#8fa6c8}
  #hint{position:fixed;bottom:12px;width:100%;text-align:center;font-size:12px;color:#4d5f7d}
</style>
</head>
<body>
<div id="graph"></div>
<div id="hud">
  记忆节点 <b id="n1">0</b> · 突触 <b id="n2">0</b><br>
  生成于 __BUILT__
</div>
<div id="detail"></div>
<div id="hint">滚轮缩放 · 左键拖动旋转 · 点一颗看原文</div>

<script src="https://unpkg.com/three@0.136.0/build/three.min.js"></script>
<script src="https://unpkg.com/3d-force-graph@1.73.4/dist/3d-force-graph.min.js"></script>
<script>
const DATA = __DATA__;

const KIND_COLOR = {
  fact:   '#7fd3ff',
  prefer: '#8ef1c9',
  event:  '#c9b6ff',
  mood:   '#ff9ec7',
  person: '#ffe08a'
};

document.getElementById('n1').textContent = DATA.nodes.length;
document.getElementById('n2').textContent = DATA.links.length;

// 每个节点都显示名字（之前只显示 24 个，用户要求全显示）
const topIds = new Set(DATA.nodes.map(n => n.id));

function makeTextSprite(text) {
  const pad = 5, fs = 26;
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  ctx.font = fs + 'px "Microsoft YaHei", sans-serif';
  const w = Math.min(360, ctx.measureText(text).width + pad * 2);
  c.width = w; c.height = fs + pad * 2;
  const g = c.getContext('2d');
  g.font = fs + 'px "Microsoft YaHei", sans-serif';
  // 先描一圈深色底再写字，不然压在亮边上糊成一片
  g.textBaseline = 'middle';
  g.lineWidth = 5;
  g.strokeStyle = 'rgba(3,6,12,0.92)';
  g.strokeText(text, pad, c.height / 2);
  g.fillStyle = 'rgba(214,232,255,0.96)';
  g.fillText(text, pad, c.height / 2);
  const tex = new THREE.CanvasTexture(c);
  tex.minFilter = THREE.LinearFilter;
  const mat = new THREE.SpriteMaterial({map: tex, transparent: true, depthWrite: false});
  const sp = new THREE.Sprite(mat);
  const scale = 0.15;
  sp.scale.set(c.width * scale, c.height * scale, 1);
  return sp;
}

// 圆形光点纹理（不这样搞的话 Sprite 是方块）
const GLOW_TEX = (() => {
  const size = 128;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(size/2, size/2, 0, size/2, size/2, size/2);
  grd.addColorStop(0, 'rgba(255,255,255,1)');
  grd.addColorStop(0.35, 'rgba(255,255,255,0.55)');
  grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, size, size);
  return new THREE.CanvasTexture(c);
})();

const G = ForceGraph3D()(document.getElementById('graph'))
  .backgroundColor('#05070d')
  .graphData((function(){
    const ns = DATA.nodes.map(n => ({...n}));
    const ls = DATA.links.map(l => ({...l}));
    const pinned = pinIsolated(ns, ls);
    if (pinned) console.log('孤立记忆 ' + pinned + ' 条，已钉在中心附近');
    return {nodes: ns, links: ls};
  })())
  .nodeLabel(n => n.text)
  .nodeRelSize(3.0)
  .linkColor(() => 'rgba(168,205,255,0.62)')
  .linkWidth(l => 0.55 + l.w * 2.2)
  .linkOpacity(0.92)
  .onNodeClick(n => showDetail(n))
  .nodeThreeObject(n => {
    const color = KIND_COLOR[n.kind] || '#7fd3ff';
    const group = new THREE.Group();

    // 2026-10-29 用户反馈"光团太亮"，核收小、外圈压淡
    const core = new THREE.Sprite(new THREE.SpriteMaterial({
      map: GLOW_TEX, color, transparent: true,
      opacity: 0.58 + n.strength * 0.18,
      blending: THREE.AdditiveBlending, depthWrite: false
    }));
    const s1 = 3.2 + n.strength * 7.5;
    core.scale.set(s1, s1, 1);
    group.add(core);

    const halo = new THREE.Sprite(new THREE.SpriteMaterial({
      map: GLOW_TEX, color, transparent: true, opacity: 0.05 + n.strength * 0.09,
      blending: THREE.AdditiveBlending, depthWrite: false
    }));
    const s2 = 10 + n.strength * 18;
    halo.scale.set(s2, s2, 1);
    group.add(halo);

    if (n.label) {
      const label = makeTextSprite(n.label);
      const up = (n.id % 2 === 0) ? 1 : -1;
      label.position.set(0, up * (6 + n.strength * 5), 0);
      group.add(label);
    }
    return group;
  });

function showDetail(n) {
  const box = document.getElementById('detail');
  box.style.display = 'block';
  box.classList.add('on');
  box.innerHTML =
    '<h3>' + n.kind + '</h3>' +
    '<div style="margin-bottom:8px;color:#eaf2ff">' + escapeHtml(n.text) + '</div>' +
    '<div class="kv">强度 ' + n.strength + ' · 被想起 ' + n.hits + ' 次</div>' +
    '<div class="kv">情绪 ' + n.valence + ' · 存在 ' + n.age_days + ' 天</div>' +
    '<div class="kv" style="margin-top:6px;color:#5f7395">再点空白处关掉</div>';
  const d = 90;
  const p = {x: n.x || 1, y: n.y || 1, z: n.z || 1};
  const ratio = 1 + d / Math.hypot(p.x, p.y, p.z);
  G.cameraPosition({x: p.x * ratio, y: p.y * ratio, z: p.z * ratio}, n, 1200);
}
function escapeHtml(s){return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

// 力度按节点数自适应：人少散开，人多收紧
// 2026-10-29 用户反馈"团之间隔太远"，斥力收小、连线拉近、向心力加大
// 没有任何连线的记忆会被斥力推到很远，把整个布局拉长、镜头被迫拉远。
// 这里把它们钉在中心附近的一小圈上，视觉上仍然看得到，但不影响主结构。
function pinIsolated(nodes, links) {
  var deg = {};
  nodes.forEach(function(n){ deg[n.id] = 0; });
  (links || []).forEach(function(l){
    var a = (l.source && l.source.id !== undefined) ? l.source.id : l.source;
    var b = (l.target && l.target.id !== undefined) ? l.target.id : l.target;
    if (deg[a] !== undefined) deg[a]++;
    if (deg[b] !== undefined) deg[b]++;
  });
  var k = 0;
  nodes.forEach(function(n){
    if (deg[n.id]) return;
    var a = k * 2.399963;   // 黄金角，均摊不重叠
    var r = 42 + (k % 5) * 7;
    n.fx = Math.cos(a) * r;
    n.fy = Math.sin(a) * r * 0.55;
    n.fz = Math.sin(a * 0.7) * r * 0.5;
    n.__iso = true;
    k++;
  });
  return k;
}

// 布局收敛后收拾一下：把整团按"85% 节点的半径"缩放到目标半径内并钉住。
// 否则个别跑远的节点会把画面撑得又空又小（实测相机距离被拉到 1500+）。
var TIDY_RADIUS = 260, tidied = false;
function tidyLayout() {
  var ns = (G.graphData().nodes || []).filter(function(n){ return n.x !== undefined; });
  if (!ns.length) return;
  function med(a){ a = a.slice().sort(function(x,y){ return x-y; }); return a[Math.floor(a.length/2)]; }
  var cx = med(ns.map(function(n){ return n.x; }));
  var cy = med(ns.map(function(n){ return n.y; }));
  var cz = med(ns.map(function(n){ return n.z; }));
  var ds = ns.map(function(n){
    return Math.sqrt((n.x-cx)*(n.x-cx) + (n.y-cy)*(n.y-cy) + (n.z-cz)*(n.z-cz));
  }).sort(function(a,b){ return a-b; });
  var r = ds[Math.floor(ds.length * 0.85)] || 1;
  if (r <= TIDY_RADIUS * 1.15) return false;
  var k = TIDY_RADIUS / r;
  ns.forEach(function(n){
    n.x = cx + (n.x - cx) * k;
    n.y = cy + (n.y - cy) * k;
    // 深度压扁到 45%：3D 的纵深会让同一团在屏幕上散开，看着像好几团
    n.z = cz + (n.z - cz) * k * 0.45;
    n.fx = n.x; n.fy = n.y; n.fz = n.z;   // 钉住，别再被斥力弹开
  });
  G.nodePositionUpdate && G.nodePositionUpdate();
  console.log('布局已归一化：原半径 ' + r.toFixed(0) + ' → ' + TIDY_RADIUS);
  return true;
}

const N = DATA.nodes.length;
G.d3Force('charge').strength(N > 120 ? -70 : -130);
G.d3Force('link').distance(l => 40 - Math.min(15, l.w * 15));
G.d3Force('center').strength(0.38);
try {
  const ctl = G.controls();
  ctl.enableDamping = true;
  ctl.dampingFactor = 0.09;
  ctl.rotateSpeed = 0.35;
  ctl.zoomSpeed = 0.55;
  ctl.panSpeed = 0.4;
} catch (e) {}

// 引擎稳定后自动取景：按 90% 节点的分布半径算，比 zoomToFit 稳
// （后者会被一两个离群点撑到很远，看着就一小撮）
function fitCamera() {
  const ns = (G.graphData().nodes || []).filter(n => n.x !== undefined);
  if (!ns.length) return;
  const med = arr => { const a = arr.slice().sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; };
  const cx = med(ns.map(n => n.x));
  const cy = med(ns.map(n => n.y));
  const cz = med(ns.map(n => n.z));
  const ds = ns.map(n => Math.hypot(n.x - cx, n.y - cy, n.z - cz)).sort((a, b) => a - b);
  const r = ds[Math.floor(ds.length * 0.85)] || 80;
  const d = Math.max(220, r * 2.9);
  G.cameraPosition({x: cx, y: cy, z: cz + d}, {x: cx, y: cy, z: cz}, 0);
}
G.onEngineStop(() => { tidyLayout(); G.zoomToFit(0, 60); });
setTimeout(() => { tidyLayout(); G.zoomToFit(700, 60); }, 4200);

setTimeout(() => {
  
}, 1200);
</script>
</body>
</html>
"""


def _owner_cid() -> str:
    """没指定 cid 时，默认看主人自己的那张网（从 .env 的 OWNER_QQ 读）。"""
    try:
        for line in (BASE / ".env" if "BASE" in dir() else Path(__file__).parent / ".env").read_text(
            encoding="utf-8"
        ).splitlines():
            line = line.strip()
            if line.startswith("OWNER_QQ=") or line.startswith("OWNER_QQS="):
                val = line.split("=", 1)[1].strip()
                for part in val.replace(",", " ").split():
                    if part.isdigit():
                        return "u" + part
    except Exception:
        pass
    return ""


def build(open_browser: bool = False, cid: str = "") -> Path:
    if not cid:
        cid = _owner_cid()
    conn = neuron.connect()
    neuron.decay(conn)
    graph = neuron.export_graph(conn, cid=cid)
    out = neuron.DATA / f"memory-graph{('-' + cid) if cid else ''}.html"
    html = HTML.replace("__DATA__", json.dumps(graph, ensure_ascii=False))
    html = html.replace("__BUILT__", graph["built"])
    out.write_text(html, encoding="utf-8")

    st = neuron.stats(conn, cid=cid)
    print(f"节点 {st['nodes']} · 突触 {st['synapses']}" + (f"（只看 {cid}）" if cid else ""))
    for kind, info in sorted(st["by_kind"].items()):
        print(f"  {kind:8s} {info['count']:4d} 条，平均强度 {info['avg_strength']}")
    for text, s in st["strongest"]:
        print(f"  最强: {text}  ({s})")
    print(f"\n已生成 {out}")
    if open_browser:
        webbrowser.open(out.as_uri())
    return out


if __name__ == "__main__":
    cid = ""
    for i, a in enumerate(sys.argv):
        if a == "--cid" and i + 1 < len(sys.argv):
            cid = sys.argv[i + 1]
    build("--open" in sys.argv, cid=cid)
