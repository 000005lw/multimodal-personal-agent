"""记忆云的「可编辑版」—— 浏览器里点一个节点就能改它。

    python neuron_edit.py            # 起服务并自动开浏览器（默认看主人的网）
    python neuron_edit.py --cid u1000000011
    python neuron_edit.py --port 8099

改完点「保存」，直接写进本地 data/neuron.db。
想让服务器上的她也生效，改完把 neuron.db 传上去（或告诉我，我来传）。

只用标准库，不装任何东西。
"""

from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from whale.memory import neuron

PORT = 8098
DEFAULT_CID = ""

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>记忆云 · 可编辑</title>
<style>
  html,body{margin:0;height:100%;background:#04060b;color:#dfe8ff;
    font-family:"Microsoft YaHei",system-ui,sans-serif;overflow:hidden}
  #graph{width:100%;height:100%}
  #hud{position:fixed;left:18px;top:14px;font-size:13px;line-height:1.9;
    color:#9fb6dd;pointer-events:none;text-shadow:0 0 8px #000}
  #hud b{color:#8ef1c9;font-weight:600}
  #hud .warn{color:#ffd479}
  #panel{position:fixed;right:16px;top:14px;width:320px;max-height:82vh;overflow:auto;
    background:rgba(9,14,25,.94);border:1px solid #22334f;border-radius:12px;
    padding:14px 16px;font-size:13px;line-height:1.7;display:none}
  #panel h3{margin:0 0 10px;font-size:13px;color:#8ef1c9;font-weight:600}
  #panel .kv{color:#8fa6c8;font-size:12px}
  #panel label{display:block;margin:10px 0 4px;color:#8fa6c8;font-size:12px}
  #panel input,#panel select,#panel textarea{
    width:100%;box-sizing:border-box;background:#0d1524;color:#e6efff;
    border:1px solid #24354f;border-radius:7px;padding:7px 9px;font-size:13px;
    font-family:inherit}
  #panel textarea{resize:vertical;min-height:64px}
  #panel .row{display:flex;gap:8px;margin-top:12px}
  #panel button{flex:1;cursor:pointer;border:0;border-radius:7px;padding:9px 0;
    font-size:13px;font-weight:600;font-family:inherit}
  #save{background:#1f6f52;color:#c9ffe8}
  #save:hover{background:#268a66}
  #del{background:#5a2230;color:#ffc9d6}
  #del:hover{background:#7a2c3f}
  #close{background:#1a2436;color:#9fb6dd}
  #hint{position:fixed;bottom:10px;width:100%;text-align:center;font-size:12px;color:#465872}
  #toast{position:fixed;left:50%;bottom:46px;transform:translateX(-50%);
    background:#12304a;color:#bfe6ff;padding:8px 16px;border-radius:8px;
    font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none}
  #toast.on{opacity:1}
</style>
</head>
<body>
<div id="graph"></div>
<div id="hud">
  节点 <b id="n1">0</b> · 突触 <b id="n2">0</b><br>
  <span class="warn" id="dirty"></span>
</div>
<div id="panel">
  <h3>编辑这条记忆</h3>
  <div class="kv" id="meta"></div>
  <label>内容（改完会重新算向量）</label>
  <textarea id="f_text"></textarea>
  <label>类型</label>
  <select id="f_kind">
    <option value="fact">事实</option>
    <option value="prefer">喜好</option>
    <option value="event">事件</option>
    <option value="mood">情绪</option>
    <option value="person">人</option>
  </select>
  <label>稳定性 S（天，越大越不容易忘）</label>
  <input id="f_stab" type="number" step="0.5" min="0.2" max="365">
  <div class="row">
    <button id="save">保存</button>
    <button id="del">删除</button>
  </div>
  <div class="row">
    <button id="close">关闭</button>
  </div>
</div>
<div id="hint">点节点编辑 · 滚轮缩放 · 左键拖动旋转</div>
<div id="toast"></div>

<script src="https://unpkg.com/three@0.136.0/build/three.min.js"></script>
<script src="https://unpkg.com/3d-force-graph@1.73.4/dist/3d-force-graph.min.js"></script>
<script>
var CID = new URLSearchParams(location.search).get('cid') || '';
var DATA = {nodes: [], links: []};
var G = null, dirty = 0, current = null;

var KIND_COLOR = {fact:'#7fd3ff', prefer:'#8ef1c9', event:'#c9b6ff', mood:'#ff9ec7', person:'#ffe08a'};
var KIND_NAME  = {fact:'事实', prefer:'喜好', event:'事件', mood:'情绪', person:'人'};

function toast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('on');
  setTimeout(function(){ t.classList.remove('on'); }, 1800);
}

var GLOW_TEX = (function(){
  var size = 128, c = document.createElement('canvas');
  c.width = c.height = size;
  var g = c.getContext('2d');
  var grd = g.createRadialGradient(size/2,size/2,0,size/2,size/2,size/2);
  grd.addColorStop(0,'rgba(255,255,255,1)');
  grd.addColorStop(0.35,'rgba(255,255,255,0.55)');
  grd.addColorStop(1,'rgba(255,255,255,0)');
  g.fillStyle = grd; g.fillRect(0,0,size,size);
  return new THREE.CanvasTexture(c);
})();

function makeTextSprite(text) {
  var pad = 5, fs = 26;
  var c = document.createElement('canvas');
  var ctx = c.getContext('2d');
  ctx.font = fs + 'px "Microsoft YaHei", sans-serif';
  var w = Math.min(420, ctx.measureText(text).width + pad * 2);
  c.width = w; c.height = fs + pad * 2;
  var g = c.getContext('2d');
  g.font = fs + 'px "Microsoft YaHei", sans-serif';
  g.textBaseline = 'middle';
  g.lineWidth = 5;
  g.strokeStyle = 'rgba(3,6,12,0.94)';
  g.strokeText(text, pad, c.height/2);
  g.fillStyle = 'rgba(216,234,255,0.97)';
  g.fillText(text, pad, c.height/2);
  var tex = new THREE.CanvasTexture(c);
  tex.minFilter = THREE.LinearFilter;
  var mat = new THREE.SpriteMaterial({map: tex, transparent: true, depthWrite: false});
  var sp = new THREE.Sprite(mat);
  sp.scale.set(c.width * 0.15, c.height * 0.15, 1);
  return sp;
}

// 取景：用 90% 节点的分布半径，避免个别离群点把镜头拉太远
function fitCamera() {
  var ns = (G.graphData().nodes || []).filter(function(n){ return n.x !== undefined; });
  if (!ns.length) return;
  function med(arr){ arr = arr.slice().sort(function(a,b){ return a-b; });
    return arr[Math.floor(arr.length / 2)]; }
  var cx = med(ns.map(function(n){ return n.x; }));
  var cy = med(ns.map(function(n){ return n.y; }));
  var cz = med(ns.map(function(n){ return n.z; }));
  var ds = ns.map(function(n){
    var dx = n.x - cx, dy = n.y - cy, dz = n.z - cz;
    return Math.sqrt(dx*dx + dy*dy + dz*dz);
  }).sort(function(a,b){ return a-b; });
  var r = ds[Math.floor(ds.length * 0.85)] || 80;
  var d = Math.max(220, r * 2.9);
  G.cameraPosition({x: cx, y: cy, z: cz + d}, {x: cx, y: cy, z: cz}, 0);
}

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

function buildGraph() {
  if (G) { G.graphData({nodes: DATA.nodes.map(function(n){return Object.assign({},n);}),
                        links: DATA.links.map(function(l){return Object.assign({},l);})}); return; }
  G = ForceGraph3D()(document.getElementById('graph'))
    .backgroundColor('#04060b')
    .nodeLabel(function(n){ return n.text; })
    .nodeRelSize(3.0)
    .linkColor(function(){ return 'rgba(168,205,255,0.62)'; })
    .linkWidth(function(l){ return 0.55 + l.w * 2.2; })
    .linkOpacity(0.92)
    .onNodeClick(function(n){ openPanel(n); })
    .nodeThreeObject(function(n){
      var color = KIND_COLOR[n.kind] || '#7fd3ff';
      var group = new THREE.Group();
      // 2026-10-29 用户反馈"光团太亮"，把核收小、外圈压淡
      var core = new THREE.Sprite(new THREE.SpriteMaterial({
        map: GLOW_TEX, color: color, transparent: true,
        opacity: 0.58 + n.strength * 0.18,
        blending: THREE.AdditiveBlending, depthWrite: false}));
      var s1 = 3.2 + n.strength * 7.5;
      core.scale.set(s1, s1, 1); group.add(core);
      var halo = new THREE.Sprite(new THREE.SpriteMaterial({
        map: GLOW_TEX, color: color, transparent: true,
        opacity: 0.05 + n.strength * 0.09,
        blending: THREE.AdditiveBlending, depthWrite: false}));
      var s2 = 10 + n.strength * 18;
      halo.scale.set(s2, s2, 1); group.add(halo);
      if (n.label) {
        var label = makeTextSprite(n.label);
        // 按 id 奇偶上下交错，密集处不至于糊成一条
        var up = (n.id % 2 === 0) ? 1 : -1;
        label.position.set(0, up * (6 + n.strength * 5), 0);
        group.add(label);
      }
      return group;
    });

  var N = Math.max(1, DATA.nodes.length);
  // 2026-10-29 用户反馈"几个团之间隔太远、看着累"，把斥力收小、连线拉近、向心力加大
  G.d3Force('charge').strength(N > 120 ? -70 : -130);
  G.d3Force('link').distance(function(l){ return 40 - Math.min(15, l.w * 15); });
  G.d3Force('center').strength(0.38);
  // 拖动灵敏度：默认转得又快又没惯性，手感很飘
  try {
    var ctl = G.controls();
    ctl.enableDamping = true;
    ctl.dampingFactor = 0.09;
    ctl.rotateSpeed = 0.35;
    ctl.zoomSpeed = 0.55;
    ctl.panSpeed = 0.4;
  } catch (e) {}
  // 布局归一化之后离群点不会再撑爆取景，直接让库算精确范围
  G.onEngineStop(function(){ tidyLayout(); G.zoomToFit(0, 60); });
  setTimeout(function(){ tidyLayout(); G.zoomToFit(700, 60); }, 4200);
  var ns = DATA.nodes.map(function(n){return Object.assign({},n);});
  var ls = DATA.links.map(function(l){return Object.assign({},l);});
  var pinned = pinIsolated(ns, ls);
  if (pinned) console.log('孤立记忆 ' + pinned + ' 条，已钉在中心附近');
  G.graphData({nodes: ns, links: ls});
  window.G = G;
}

function refresh() {
  return fetch('/api/graph' + (CID ? '?cid=' + encodeURIComponent(CID) : ''))
    .then(function(r){ return r.json(); })
    .then(function(d){
      DATA = d;
      document.getElementById('n1').textContent = d.nodes.length;
      document.getElementById('n2').textContent = d.links.length;
      buildGraph();
    });
}

function openPanel(n) {
  current = n;
  document.getElementById('panel').style.display = 'block';
  document.getElementById('meta').textContent =
    'id ' + n.id + ' · 强度 ' + n.strength + ' · 被想起 ' + n.hits + ' 次 · 存在 ' + n.age_days + ' 天';
  document.getElementById('f_text').value = n.text;
  document.getElementById('f_kind').value = n.kind || 'fact';
  document.getElementById('f_stab').value = n.stability || 3;
}

document.getElementById('save').onclick = function(){
  if (!current) return;
  var body = {
    id: current.id,
    text: document.getElementById('f_text').value,
    kind: document.getElementById('f_kind').value,
    stability: parseFloat(document.getElementById('f_stab').value)
  };
  fetch('/api/node', {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify(body)})
    .then(function(r){ return r.json(); })
    .then(function(res){
      if (res.ok) {
        dirty++;
        document.getElementById('dirty').textContent = '本次已改 ' + dirty + ' 条';
        toast('已保存（写进 neuron.db 了）');
        document.getElementById('panel').style.display = 'none';
        refresh();
      } else {
        toast('没保存成功：' + (res.error || '未知原因'));
      }
    });
};

document.getElementById('del').onclick = function(){
  if (!current) return;
  if (!confirm('确定删掉「' + current.text + '」？删了就没了')) return;
  fetch('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({id: current.id})})
    .then(function(r){ return r.json(); })
    .then(function(res){
      if (res.ok) {
        dirty++;
        document.getElementById('dirty').textContent = '本次已改 ' + dirty + ' 条';
        toast('已删除');
        document.getElementById('panel').style.display = 'none';
        refresh();
      } else {
        toast('删除失败：' + (res.error || '未知原因'));
      }
    });
};

document.getElementById('close').onclick = function(){
  document.getElementById('panel').style.display = 'none';
};

refresh();
</script>
</body>
</html>
"""


PATCH_LOG = neuron.DATA / "neuron_patch.jsonl"


def _log_patch(op: str, payload: dict) -> None:
    """记一条补丁。上传时只把这行带到服务器，避免整库覆盖丢记忆。"""
    try:
        import time as _t

        payload = dict(payload)
        payload["op"] = op
        payload["ts"] = _t.strftime("%Y-%m-%d %H:%M:%S")
        with PATCH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静点
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/graph":
            qs = parse_qs(parsed.query)
            cid = (qs.get("cid") or [DEFAULT_CID])[0]
            try:
                conn = neuron.connect()
                neuron.decay(conn)
                self._json(neuron.export_graph(conn, cid=cid))
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._json({"ok": False, "error": f"读不懂请求：{exc}"}, 400)
            return

        try:
            conn = neuron.connect()
            if parsed.path == "/api/node":
                ok = neuron.update_node(
                    conn,
                    int(body.get("id") or 0),
                    text=body.get("text"),
                    kind=body.get("kind"),
                    stability=body.get("stability"),
                )
                if ok:
                    _log_patch("update", {
                        "id": int(body.get("id") or 0),
                        "text": body.get("text"),
                        "kind": body.get("kind"),
                        "stability": body.get("stability"),
                    })
                self._json({"ok": bool(ok), "error": "" if ok else "没找到这条记忆"})
                return
            if parsed.path == "/api/delete":
                ok = neuron.delete_node(conn, int(body.get("id") or 0))
                if ok:
                    _log_patch("delete", {"id": int(body.get("id") or 0)})
                self._json({"ok": bool(ok), "error": "" if ok else "没找到这条记忆"})
                return
            if parsed.path == "/api/add":
                text = str(body.get("text") or "").strip()
                if not text:
                    self._json({"ok": False, "error": "内容不能为空"}, 400)
                    return
                new_id = neuron.add_manual(
                    conn, text, kind=str(body.get("kind") or "fact"), cid=str(body.get("cid") or "")
                )
                _log_patch("add", {
                    "text": text,
                    "kind": str(body.get("kind") or "fact"),
                    "cid": str(body.get("cid") or ""),
                })
                self._json({"ok": True, "id": new_id})
                return
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)
            return

        self._send(404, b"not found", "text/plain")


def main() -> None:
    global PORT, DEFAULT_CID
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            PORT = int(args[i + 1])
        if a == "--cid" and i + 1 < len(args):
            DEFAULT_CID = args[i + 1]
    if not DEFAULT_CID:
        from scripts import neuron_view

        DEFAULT_CID = neuron_view._owner_cid()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    url = f"http://127.0.0.1:{PORT}/"
    print(f"记忆云编辑页：{url}")
    print(f"看的是：{DEFAULT_CID or '全部'}")
    print("改完点保存就直接写进 data/neuron.db。Ctrl+C 关掉。")
    print()
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
