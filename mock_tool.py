"""Mock API tool with embedded admin UI.

Run:
    pip install flask
    python mock_tool.py
Then open http://127.0.0.1:5001/ to manage endpoints.
Mocked endpoints are served under http://127.0.0.1:5001/mock/<your-path>.

使用方法:
1. 浏览器打开 http://127.0.0.1:5001/
2. 在左侧表单填写 方法 / 路径 / 状态码 / 响应体, 点"保存"
3. 也可以从"模板库"下拉中选择内置模板 (test.py 中的接口已全部内置), 自动填充表单后点"保存"
4. 调用方真实请求时, 把原始 host 改成 http://127.0.0.1:5001/mock 即可, 例如:
       POST http://127.0.0.1:5001/mock/task-center-service/task/page
5. 响应体支持两种模式:
   - template (默认): {{path.id}} / {{query.name}} / {{body.field}} / {{header.xxx}} /
                       {{random.int|uuid|float}} / {{now}} / {{= python 表达式 }}
   - script: 多行 Python 代码, 把最终响应赋给变量 result (dict/list/str 均可)
             可用对象: path, query, body, headers, random, uuid, math, datetime, date, timedelta, json
"""

from __future__ import annotations

import hmac
import json
import math
import os
import random
import re
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path
from threading import Lock

from flask import Flask, Response, jsonify, request

STORE_FILE = Path(__file__).with_name("mocks.json")
MOCK_PREFIX = "/mock"

app = Flask(__name__)
_store_lock = Lock()


# ---------- Basic Auth (protects Admin UI + Admin API) ----------

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")  # empty => auth disabled


def _auth_ok(auth) -> bool:
    if not ADMIN_PASS:
        return True
    if not auth or auth.type != "basic":
        return False
    return (
        hmac.compare_digest(auth.username or "", ADMIN_USER)
        and hmac.compare_digest(auth.password or "", ADMIN_PASS)
    )


def _auth_challenge():
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Mock Tool Admin"'},
    )


@app.before_request
def _require_auth():
    if not ADMIN_PASS:
        return None
    p = request.path or ""
    # Mocked endpoints stay public; only admin surface is protected.
    if p.startswith(MOCK_PREFIX + "/") or p == MOCK_PREFIX:
        return None
    if not _auth_ok(request.authorization):
        return _auth_challenge()
    return None


def load_store() -> list[dict]:
    if not STORE_FILE.exists():
        return []
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_store(mocks: list[dict]) -> None:
    STORE_FILE.write_text(
        json.dumps(mocks, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def path_to_regex(path: str) -> tuple[re.Pattern, list[str]]:
    """Convert '/users/{id}/books/{bid}' to regex + param names."""
    names: list[str] = []

    def repl(m: re.Match) -> str:
        names.append(m.group(1))
        return r"(?P<" + m.group(1) + r">[^/]+)"

    pattern = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, path.rstrip("/"))
    return re.compile("^" + pattern + "/?$"), names


SAFE_BUILTINS = {
    "abs": abs, "min": min, "max": max, "round": round,
    "len": len, "sum": sum, "int": int, "float": float, "str": str,
    "bool": bool, "list": list, "dict": dict, "tuple": tuple,
    "range": range, "enumerate": enumerate, "sorted": sorted,
    "map": map, "filter": filter, "zip": zip, "any": any, "all": all,
    "True": True, "False": False, "None": None,
    "isinstance": isinstance, "print": print,
}


def _script_env(ctx: dict) -> dict:
    return {
        "__builtins__": SAFE_BUILTINS,
        "random": random,
        "uuid": uuid,
        "math": math,
        "datetime": datetime,
        "date": date,
        "timedelta": timedelta,
        "json": json,
        "path": ctx["path"],
        "query": ctx["query"],
        "body": ctx["body"],
        "headers": ctx["headers"],
        "header": ctx["headers"],
    }


def eval_expr(expr: str, ctx: dict):
    try:
        return eval(expr, _script_env(ctx), {})
    except Exception as e:
        return f"<expr-error: {e}>"


def run_script(code: str, ctx: dict):
    """Run a multi-line python script. Read final value from `result`."""
    env = _script_env(ctx)
    local: dict = {}
    try:
        exec(code, env, local)
    except Exception as e:
        return {"error": f"script-error: {e}"}
    if "result" in local:
        return local["result"]
    return {"error": "script must assign final value to `result`"}


def render_template(text: str, ctx: dict) -> str:
    """Replace {{namespace.key}} / {{= expr }} / {{token}} placeholders."""

    def _get(ns: str, key: str | None) -> str:
        if ns == "path":
            return str(ctx["path"].get(key, ""))
        if ns == "query":
            return str(ctx["query"].get(key, ""))
        if ns == "body":
            v = ctx["body"]
            if isinstance(v, dict) and key is not None:
                return str(v.get(key, ""))
            return str(v)
        if ns == "header":
            return str(ctx["headers"].get(key, "")) if key else ""
        if ns == "random":
            if key == "int":
                return str(random.randint(1, 1_000_000))
            if key == "uuid":
                return str(uuid.uuid4())
            if key == "float":
                return f"{random.random():.4f}"
        if ns == "now":
            return datetime.now().isoformat(timespec="seconds")
        return ""

    def repl(m: re.Match) -> str:
        token = m.group(1).strip()
        if token.startswith("="):
            val = eval_expr(token[1:].strip(), ctx)
            if isinstance(val, str):
                return json.dumps(val)[1:-1]
            return json.dumps(val, ensure_ascii=False, default=str)
        if "." in token:
            ns, key = token.split(".", 1)
            return _get(ns, key)
        return _get(token, None)

    return re.sub(r"\{\{\s*(.+?)\s*\}\}", repl, text, flags=re.S)


def match_mock(method: str, sub_path: str) -> tuple[dict, dict] | None:
    sub_path = "/" + sub_path.lstrip("/")
    for m in load_store():
        if m["method"].upper() != method.upper():
            continue
        pattern, _names = path_to_regex(m["path"])
        mo = pattern.match(sub_path)
        if mo:
            return m, mo.groupdict()
    return None


# ---------- Mock dispatcher ----------


@app.route(
    MOCK_PREFIX + "/<path:sub_path>",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
@app.route(
    MOCK_PREFIX + "/",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    defaults={"sub_path": ""},
)
def dispatch_mock(sub_path: str):
    hit = match_mock(request.method, sub_path)
    if not hit:
        return jsonify({"error": "mock not found", "path": "/" + sub_path}), 404

    mock, path_params = hit
    try:
        body_json = request.get_json(silent=True)
    except Exception:
        body_json = None
    ctx = {
        "path": path_params,
        "query": request.args.to_dict(),
        "body": body_json if body_json is not None else request.form.to_dict(),
        "headers": {k: v for k, v in request.headers.items()},
    }

    status = int(mock.get("status", 200))
    content_type = mock.get("content_type") or "application/json"
    mode = mock.get("mode", "template")

    if mode == "script":
        value = run_script(mock.get("response", ""), ctx)
        rendered = json.dumps(value, ensure_ascii=False, default=str) \
            if not isinstance(value, str) else value
    else:
        rendered = render_template(mock.get("response", ""), ctx)
    return Response(rendered, status=status, content_type=content_type)


# ---------- Admin API ----------


@app.get("/api/mocks")
def api_list():
    return jsonify(load_store())


@app.post("/api/mocks")
def api_create():
    payload = request.get_json(force=True)
    required = {"path", "method", "response"}
    if not required.issubset(payload):
        return jsonify({"error": f"missing {required - set(payload)}"}), 400
    with _store_lock:
        mocks = load_store()
        payload["id"] = uuid.uuid4().hex[:8]
        payload.setdefault("status", 200)
        payload.setdefault("content_type", "application/json")
        payload.setdefault("mode", "template")
        if not payload["path"].startswith("/"):
            payload["path"] = "/" + payload["path"]
        mocks.append(payload)
        save_store(mocks)
    return jsonify(payload), 201


@app.put("/api/mocks/<mid>")
def api_update(mid: str):
    payload = request.get_json(force=True)
    with _store_lock:
        mocks = load_store()
        for i, m in enumerate(mocks):
            if m["id"] == mid:
                payload["id"] = mid
                if not payload.get("path", "/").startswith("/"):
                    payload["path"] = "/" + payload["path"]
                mocks[i] = {**m, **payload}
                save_store(mocks)
                return jsonify(mocks[i])
    return jsonify({"error": "not found"}), 404


@app.delete("/api/mocks/<mid>")
def api_delete(mid: str):
    with _store_lock:
        mocks = load_store()
        new = [m for m in mocks if m["id"] != mid]
        if len(new) == len(mocks):
            return jsonify({"error": "not found"}), 404
        save_store(new)
    return jsonify({"ok": True})


# ---------- Admin UI ----------


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>Mock 工具</title>
<style>
  body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;color:#222}
  h1{font-size:20px;margin:0 0 16px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}
  .card{border:1px solid #e5e7eb;border-radius:8px;padding:16px;background:#fff}
  label{display:block;font-size:12px;color:#555;margin:8px 0 4px}
  input,select,textarea{width:100%;padding:6px 8px;border:1px solid #d1d5db;border-radius:6px;font:inherit;box-sizing:border-box}
  textarea{min-height:240px;font-family:ui-monospace,Menlo,Consolas,monospace}
  button{background:#2563eb;color:#fff;border:0;padding:8px 14px;border-radius:6px;cursor:pointer}
  button.secondary{background:#6b7280}
  button.danger{background:#dc2626}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{border-bottom:1px solid #eee;padding:8px 6px;text-align:left;vertical-align:top}
  code{background:#f3f4f6;padding:1px 4px;border-radius:4px}
  .tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;color:#fff;background:#10b981}
  .tag.POST{background:#f59e0b}.tag.PUT{background:#6366f1}.tag.DELETE{background:#dc2626}.tag.PATCH{background:#ec4899}
  .hint{font-size:12px;color:#666;margin-top:6px;line-height:1.6}
  .row{display:flex;gap:8px}
  .row>*{flex:1}
</style>
</head>
<body>
<h1>Mock 工具 <span style="font-size:12px;color:#666">/ 接口录入与参数化返回</span></h1>
<div class="grid">
  <div class="card">
    <h3 id="formTitle" style="margin-top:0">新增 Mock 接口</h3>
    <input type="hidden" id="mid"/>

    <label>模板库（可选，选择后自动填充表单）</label>
    <select id="tpl" onchange="applyTpl()">
      <option value="">— 不使用模板 —</option>
    </select>

    <div class="row">
      <div>
        <label>请求方法</label>
        <select id="method">
          <option>GET</option><option>POST</option><option>PUT</option>
          <option>DELETE</option><option>PATCH</option>
        </select>
      </div>
      <div style="flex:3">
        <label>路径 (支持 <code>{id}</code> 占位)</label>
        <input id="path" placeholder="/users/{id}"/>
      </div>
    </div>
    <div class="row">
      <div>
        <label>状态码</label>
        <input id="status" value="200"/>
      </div>
      <div>
        <label>Content-Type</label>
        <input id="content_type" value="application/json"/>
      </div>
      <div>
        <label>响应模式</label>
        <select id="mode">
          <option value="template">template (占位符)</option>
          <option value="script">script (Python 脚本)</option>
        </select>
      </div>
    </div>
    <label>备注</label>
    <input id="note" placeholder="可选"/>
    <label>响应体</label>
    <textarea id="response">{
  "id": "{{path.id}}",
  "name": "{{query.name}}",
  "token": "{{random.uuid}}",
  "time": "{{now}}"
}</textarea>
    <div class="hint">
      <b>template 模式占位符：</b>
      <code>{{path.xxx}}</code> 路径参数，
      <code>{{query.xxx}}</code> 查询参数，
      <code>{{body.xxx}}</code> JSON/表单字段，
      <code>{{header.xxx}}</code> 请求头，
      <code>{{random.int|uuid|float}}</code>，
      <code>{{now}}</code>，
      <code>{{= 表达式 }}</code> 单行 Python 表达式。<br>
      <b>script 模式：</b> 写多行 Python, 把最终响应赋给 <code>result</code> (dict/list/str)。
      可用对象: <code>path / query / body / headers / random / uuid / math / datetime / json</code>。
    </div>
    <div style="margin-top:12px;display:flex;gap:8px">
      <button onclick="save()">保存</button>
      <button class="secondary" onclick="resetForm()">重置</button>
    </div>
  </div>

  <div class="card">
    <h3 style="margin-top:0">已注册接口</h3>
    <table>
      <thead><tr><th>方法</th><th>路径</th><th>模式</th><th>状态</th><th>备注</th><th></th></tr></thead>
      <tbody id="list"></tbody>
    </table>
    <p class="hint">调用示例：<code id="sample">/mock/users/123?name=tom</code></p>
    <p class="hint">
      使用步骤：① 选/填模板 → ② 保存 → ③ 用客户端访问 <code>http://127.0.0.1:5001/mock&lt;路径&gt;</code><br>
      mocks.json 与本文件同目录，删除该文件可清空所有接口。
    </p>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);

// ====== 内置模板（来自 test.py 的接口）======
const TEMPLATES = [
  {
    key: "platform_quotation",
    label: "平台报价 - POST /platform-quotation-service/base-price/search",
    method: "POST",
    path: "/platform-quotation-service/base-price/search",
    status: 200,
    content_type: "application/json",
    mode: "script",
    note: "按 skuIds × levelIds 组合返回价格",
    response:
`product_id = body.get("productId") if isinstance(body, dict) else None
level_ids  = body.get("levelIds", []) if isinstance(body, dict) else []
sku_ids    = body.get("skuIds", [])  if isinstance(body, dict) else []
rows = []
for sku in sku_ids:
    for lv in level_ids:
        rows.append({
            "productId": product_id,
            "skuId": sku,
            "levelId": lv,
            "biPriceBatchNo": uuid.uuid4().hex,
            "manualBasePrice": 156000,
            "standardPrice": round(500 + random.random() * 2000, 2),
            "biPriceType": 1,
            "manualBasePriceValid": False,
            "biPriceValid": False,
            "basePriceValidV2": False,
        })
result = {"code": 200, "resultMessage": "", "data": rows}`
  },
  {
    key: "price_with_strategy",
    label: "渠道策略价格 - POST /distribution-service/.../get-price-with-strategy",
    method: "POST",
    path: "/distribution-service/open-api/channel/default/get-price-with-strategy",
    status: 200,
    content_type: "application/json",
    mode: "template",
    note: "随机价格策略",
    response:
`{
  "code": 200,
  "message": "success",
  "data": {
    "priceBeforeStrategy": {{= round(3000 + random.random()*1180, 2) }},
    "priceAfterStrategy":  {{= round(3000 + random.random()*1180, 2) }}
  },
  "resultMessage": "成功"
}`
  },
  {
    key: "p1price_batch",
    label: "P1 价格批量查询 - POST /b2b/api/p1price/p1/batch/v2",
    method: "POST",
    path: "/b2b/api/p1price/p1/batch/v2",
    status: 200,
    content_type: "application/json",
    mode: "script",
    note: "body 为商品编码数组",
    response:
`codes = body if isinstance(body, list) else []
if not codes:
    result = {"code": 400, "message": "请求体不能为空", "data": [], "resultMessage": "失败"}
else:
    result = {
        "code": 0,
        "message": "success",
        "data": [
            {"goodsCode": c, "p1Price": round(500 + random.random() * 2000, 2)}
            for c in codes
        ],
        "resultMessage": "成功",
    }`
  },
  {
    key: "task_page",
    label: "任务中心分页 - POST /task-center-service/task/page",
    method: "POST",
    path: "/task-center-service/task/page",
    status: 200,
    content_type: "application/json",
    mode: "script",
    note: "回显请求中的 taskNo",
    response:
`b = body if isinstance(body, dict) else {}
task_no = b.get("taskNo") or "RW202605061641301623"
result = {
    "code": 200,
    "resultMessage": "",
    "data": [{
        "id": 8245,
        "taskNo": task_no,
        "taskTypeName": "导入",
        "taskSubTypeName": "拍照单海外批量发货",
        "sysSource": "银河",
        "status": 80,
        "statusName": "已通过",
        "approveStatus": 80,
        "approveStatusName": "审批通过",
        "createDt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "uploadFileName": "拍照单海外批量发货_" + uuid.uuid4().hex + ".xlsx",
        "uploadFileUrl": "",
        "executeModel": "",
        "executeModelName": "",
    }],
    "totalCount": 1,
}`
  },
  {
    key: "task_create_import",
    label: "创建导入任务 - POST /task-center-service/task/create-import",
    method: "POST",
    path: "/task-center-service/task/create-import",
    status: 200,
    content_type: "application/json",
    mode: "script",
    note: "返回新生成的任务编号",
    response:
`now = datetime.now()
task_no = "RW" + now.strftime("%Y%m%d%H%M%S") + str(now.microsecond)[:3]
result = {"code": 200, "resultMessage": "", "data": task_no}`
  },
  {
    key: "product_no_check",
    label: "商品编号校验 - GET /api/{productNo}",
    method: "GET",
    path: "/api/{productNo}",
    status: 200,
    content_type: "application/json",
    mode: "script",
    note: "productNo == 20260423135956361432 才返回成功",
    response:
`target = "20260423135956361432"
pno = path.get("productNo")
if pno == target:
    result = {
        "code": 200,
        "message": "success",
        "data": {
            "priceBeforeStrategy": round(3000 + random.random()*1180, 2),
            "priceAfterStrategy":  round(3000 + random.random()*1180, 2),
        },
        "resultMessage": "成功",
    }
else:
    result = {
        "code": 400,
        "message": f"productNo {pno} 验证失败",
        "data": {},
        "resultMessage": f"产品编号 {pno} 不存在",
    }`
  },
];

function initTplOptions(){
  const sel = $('#tpl');
  TEMPLATES.forEach(t=>{
    const opt = document.createElement('option');
    opt.value = t.key; opt.textContent = t.label;
    sel.appendChild(opt);
  });
}

function applyTpl(){
  const key = $('#tpl').value;
  if(!key) return;
  const t = TEMPLATES.find(x=>x.key===key);
  if(!t) return;
  $('#method').value = t.method;
  $('#path').value   = t.path;
  $('#status').value = t.status;
  $('#content_type').value = t.content_type;
  $('#mode').value   = t.mode;
  $('#note').value   = t.note || '';
  $('#response').value = t.response;
}

async function load(){
  const r = await fetch('/api/mocks'); const data = await r.json();
  const tbody = $('#list'); tbody.innerHTML='';
  data.forEach(m=>{
    const tr=document.createElement('tr');
    tr.innerHTML = `
      <td><span class="tag ${m.method}">${m.method}</span></td>
      <td><code>/mock${m.path}</code></td>
      <td>${m.mode||'template'}</td>
      <td>${m.status||200}</td>
      <td>${m.note||''}</td>
      <td>
        <button class="secondary" onclick='edit(${JSON.stringify(JSON.stringify(m))})'>编辑</button>
        <button class="danger" onclick="del('${m.id}')">删除</button>
      </td>`;
    tbody.appendChild(tr);
  });
  if(data[0]) $('#sample').textContent = '/mock' + data[0].path;
}

function edit(jsonStr){
  const m = JSON.parse(jsonStr);
  $('#mid').value = m.id;
  $('#method').value = m.method;
  $('#path').value = m.path;
  $('#status').value = m.status || 200;
  $('#content_type').value = m.content_type || 'application/json';
  $('#mode').value = m.mode || 'template';
  $('#note').value = m.note || '';
  $('#response').value = m.response || '';
  $('#tpl').value = '';
  $('#formTitle').textContent = '编辑 Mock 接口 (' + m.id + ')';
}

function resetForm(){
  $('#mid').value=''; $('#method').value='GET';
  $('#path').value=''; $('#status').value='200';
  $('#content_type').value='application/json';
  $('#mode').value='template';
  $('#note').value=''; $('#response').value='';
  $('#tpl').value='';
  $('#formTitle').textContent='新增 Mock 接口';
}

async function save(){
  const payload = {
    method: $('#method').value,
    path:   $('#path').value.trim(),
    status: Number($('#status').value)||200,
    content_type: $('#content_type').value.trim(),
    mode:   $('#mode').value,
    note:   $('#note').value,
    response: $('#response').value,
  };
  if(!payload.path){ alert('请输入路径'); return; }
  const id = $('#mid').value;
  const url = id ? '/api/mocks/'+id : '/api/mocks';
  const method = id ? 'PUT' : 'POST';
  const r = await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(!r.ok){ alert('保存失败: '+await r.text()); return; }
  resetForm(); load();
}

async function del(id){
  if(!confirm('确认删除？')) return;
  await fetch('/api/mocks/'+id,{method:'DELETE'});
  load();
}

initTplOptions();
load();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(INDEX_HTML, content_type="text/html; charset=utf-8")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
