"""生图网页版 —— 本机自用。

    python app.py            # 真调用
    A6_MOCK=1 python app.py  # 本地假图，秒级，用来调前端

设计上的三条硬约束（改代码前先读这段）：

1. API key 只在后端。前端拿不到，也永远不该拿到。
2. 生图要 60~300 秒，不能同步等 —— 提交拿 task_id，前端轮询。
3. 只监听 127.0.0.1。绑 0.0.0.0 等于把无鉴权的生图接口暴露给整个局域网，
   同 WiFi 下任何人都能烧你的额度。

已知限制：任务表在内存里，重启后丢失（正在跑的那次随进程消失）。
已落盘的图不受影响，会出现在历史里。
"""

import asyncio
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import a6

# Windows 控制台默认 GBK，日志里有中文就会崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = a6.BASE_DIR
STATIC_DIR = BASE_DIR / "static"

HOST = os.environ.get("A6_HOST", "127.0.0.1")
PORT = int(os.environ.get("A6_PORT", "8765"))

# 并发上限。不用 asyncio.to_thread：它写死走事件循环的默认线程池
# （min(32, cpu+4)，这台机器上十几个），一口气点十次就是十条 300 秒的连接
# 同时打向服务端，几乎必然撞限流，还会被上游重复计费。
MAX_WORKERS = int(os.environ.get("A6_MAX_CONCURRENCY", "2"))
MAX_ACTIVE = MAX_WORKERS + int(os.environ.get("A6_MAX_QUEUE", "4"))   # 排队深度上限

MAX_IMAGES = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024

EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="gen")

# 只允许本机来源。任意网页里的 JS 都能向 127.0.0.1:8765 发跨站请求，
# 而这里每次调用都要花钱，所以 Host / Origin 都得校验。不开 CORS。
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@dataclass
class Task:
    id: str
    kind: str                                    # txt2img | img2img
    prompt: str
    model: str
    size: str
    input_fidelity: str | None = None
    name: str = a6.DEFAULT_NAME
    ref_names: list[str] = field(default_factory=list)

    status: str = "queued"                       # queued|running|done|error|cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    images: list[dict] = field(default_factory=list)
    error: str | None = None
    model_used: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    # 上传的参考图（NamedBytesIO），只给 worker 用，不出现在 API 响应里
    images_payload: list = field(default_factory=list, repr=False)

    def public(self) -> dict:
        now = time.time()
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "prompt": self.prompt,
            "model": self.model,
            "model_used": self.model_used,
            "size": self.size,
            "name": self.name,
            "ref_names": self.ref_names,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            # 前端自己算秒表，别用后端累计值 —— 轮询有间隔，累计值会一跳一跳的
            "queued_for": (self.started or now) - self.created,
            "elapsed": ((self.finished or now) - self.started) if self.started else None,
            "images": self.images,
            "error": self.error,
            "cancel_requested": self.cancel.is_set(),
        }


# 只在事件循环线程里读写（worker 线程不碰它）
TASKS: dict[str, Task] = {}


def _active_count() -> int:
    return sum(1 for t in TASKS.values() if t.status in ("queued", "running"))


def _prune() -> None:
    """TASKS 会一直涨，把老的已结束任务清掉，只留最近 40 个。"""
    done = [t for t in TASKS.values() if t.status in ("done", "error", "cancelled")]
    if len(done) <= 40:
        return
    done.sort(key=lambda t: t.finished or t.created)
    for t in done[:-40]:
        TASKS.pop(t.id, None)


# ==================== 后台执行 ====================


def _worker(task: Task) -> dict:
    """在子线程里跑阻塞的 SDK 调用。

    只【读】task 的字段、只 【return】结果，不改任何共享状态 ——
    状态更新统一由事件循环里的 done 回调来做，省掉一大堆加锁。
    """
    if task.cancel.is_set():
        return {"status": "cancelled"}

    # 下面这两行是「worker 只 return、不改共享状态」这条规矩的【唯一例外】。
    # 必须在这里写，因为只有线程自己知道它真的开始跑了（之前可能一直在排队），
    # 而前端要据此区分「排队中」和「生成中」的秒表。
    # 安全性：普通对象单字段赋值在 GIL 下是原子的；且先写 started 再写 status，
    # 所以事件循环绝不会读到「status=running 但 started 还是 None」。
    task.started = time.time()
    task.status = "running"

    try:
        common = dict(prompt=task.prompt, model=task.model, size=task.size)

        if task.kind == "img2img":
            if task.input_fidelity:
                common["input_fidelity"] = task.input_fidelity
            resp, model_used = a6.edit_image(task.images_payload, **common)
        else:
            resp, model_used = a6.generate_image(**common)
    except Exception as e:
        a6.log.exception("任务 %s 调用失败", task.id)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # 尽力取消的第二道闸：线程杀不掉，只能在这里选择不落盘。
    # 注意这【挡不住额度消耗】—— 请求早就发出去了。
    if task.cancel.is_set():
        return {"status": "cancelled"}

    try:
        blobs = a6.extract_images(resp)
        saved = [a6.save_image(b, task.name) for b in blobs]
    except Exception as e:
        a6.log.exception("任务 %s 落盘失败", task.id)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    return {
        "status": "done",
        "model_used": model_used,
        "images": [
            {
                "name": p.name,
                "rel": f"{p.parent.name}/{p.name}",
                "url": "/images/" + a6.quote(f"{p.parent.name}/{p.name}", safe="/"),
                "size": p.stat().st_size,
            }
            for p in saved
        ],
    }


def _submit(task: Task) -> None:
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(EXECUTOR, _worker, task)
    # 必须取一次 exception，否则 concurrent.futures 会打印
    # "exception was never retrieved"
    fut.add_done_callback(lambda f: f.exception())

    def on_done(f):
        # 这个回调由 loop.call_soon 调度，跑在【事件循环线程】里，
        # 所以可以安全地改 TASKS。
        t = TASKS.get(task.id)
        if t is None:
            return
        if f.cancelled():
            t.status, t.error = "error", "任务被取消（未执行）"
        elif f.exception() is not None:
            t.status, t.error = "error", f"{type(f.exception()).__name__}: {f.exception()}"
        else:
            t.__dict__.update(f.result())
        t.finished = time.time()
        t.images_payload = []          # 释放参考图内存
        _prune()

    fut.add_done_callback(on_done)


# ==================== 应用 ====================

app = FastAPI(title="生图", docs_url=None, redoc_url=None)


@app.middleware("http")
async def guard(request, call_next):
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    if host and host not in ALLOWED_HOSTS:
        return JSONResponse({"error": f"只允许本机访问，收到 Host: {host}"}, status_code=403)
    origin = request.headers.get("origin")
    if origin:
        try:
            oh = urlparse(origin).hostname
        except Exception:
            oh = None
        if oh not in ALLOWED_HOSTS:
            return JSONResponse({"error": f"拒绝跨站请求，Origin: {origin}"}, status_code=403)

    resp = await call_next(request)
    # 前端是本机开发用的，改完希望刷新就生效。但 Starlette 的静态文件不带
    # Cache-Control，浏览器会按 Last-Modified 猜一个新鲜期（约为文件年龄的 10%），
    # 于是「改完代码 → 重开页面」拿到的还是旧 JS，白白以为改错了地方。
    # 所以前端一律 no-store。/images 除外：历史图又多又大，重复下载不划算。
    if not request.url.path.startswith("/images"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/config")
def api_config():
    return {
        "models": a6.MODEL_LINES,
        "default_model": a6.DEFAULT_MODEL,
        "sizes": a6.SIZES,
        "default_size": a6.DEFAULT_SIZE,
        "max_images": MAX_IMAGES,
        "max_workers": MAX_WORKERS,
        "max_active": MAX_ACTIVE,
        "mock": a6.MOCK,
        "read_timeout": a6.READ_TIMEOUT,
        "output_root": str(a6.OUTPUT_ROOT),
    }


@app.post("/api/generate")
async def api_generate(
    prompt: str = Form(...),
    model: str = Form(""),
    size: str = Form(""),
    input_fidelity: str = Form(""),
    name: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    prompt = (prompt or "").strip()
    if not prompt:
        return JSONResponse({"error": "提示词不能为空"}, status_code=400)

    if _active_count() >= MAX_ACTIVE:
        return JSONResponse(
            {"error": f"已经有 {_active_count()} 个任务在跑或排队，先等一等再说"},
            status_code=429,
        )

    if len(files) > MAX_IMAGES:
        return JSONResponse({"error": f"最多 {MAX_IMAGES} 张参考图"}, status_code=400)

    # 必须在这里把上传内容读完：UploadFile 超过约 1MB 会落到
    # SpooledTemporaryFile，请求一结束就被回收，之后再读必然失败。
    payload, ref_names, total = [], [], 0
    for i, uf in enumerate(files, 1):
        data = await uf.read()
        if not data:
            continue
        if len(data) > MAX_IMAGE_BYTES:
            return JSONResponse({"error": f"{uf.filename} 超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB"}, status_code=400)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            return JSONResponse({"error": "参考图总大小超限"}, status_code=400)
        fname = a6.upload_name(uf.filename, uf.content_type, i)
        # 带 .name 才能让 httpx 发出正确的 filename + Content-Type
        payload.append(a6.NamedBytesIO(data, fname))
        ref_names.append(uf.filename or fname)

    task = Task(
        id=uuid.uuid4().hex[:12],
        kind="img2img" if payload else "txt2img",
        prompt=prompt,
        model=model.strip() or a6.DEFAULT_MODEL,
        size=size.strip() or a6.DEFAULT_SIZE,
        input_fidelity=input_fidelity.strip() or None,
        name=name.strip() or a6.DEFAULT_NAME,
        ref_names=ref_names,
        images_payload=payload,
    )
    TASKS[task.id] = task
    _submit(task)
    return {"task_id": task.id, "task": task.public()}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    t = TASKS.get(task_id)
    if t is None:
        return JSONResponse({"error": "没有这个任务（服务可能重启过，任务表在内存里）"}, status_code=404)
    return t.public()


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel(task_id: str):
    t = TASKS.get(task_id)
    if t is None:
        return JSONResponse({"error": "没有这个任务"}, status_code=404)
    if t.status in ("done", "error", "cancelled"):
        return {"ok": False, "reason": "已经结束了", "task": t.public()}
    t.cancel.set()
    # 如实告知：线程杀不掉，请求早就发出去了，额度照样消耗
    return {
        "ok": True,
        "note": "已标记作废。如果图还没生成完，这次结果会被丢弃；但请求已经发出去了，"
                "额度和上游的生成都会照常发生 —— 取消省不下这一次调用。",
        "task": t.public(),
    }


@app.get("/api/tasks")
def api_tasks():
    items = sorted(TASKS.values(), key=lambda t: t.created, reverse=True)[:30]
    return {"items": [t.public() for t in items]}


@app.get("/api/history")
def api_history(limit: int = 60, offset: int = 0):
    return a6.list_outputs(limit=max(1, min(limit, 500)), offset=max(0, offset))


# 挂载顺序有讲究：Starlette 按注册顺序匹配，"/" 是贪婪的，
# /api 和 /images 必须先注册，否则全被根静态目录吃掉。
a6.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=a6.OUTPUT_ROOT), name="images")
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


if __name__ == "__main__":
    import webbrowser

    import uvicorn

    url = f"http://{HOST}:{PORT}"
    print(f"\n  生图网页版  →  {url}")
    print(f"  输出目录    →  {a6.OUTPUT_ROOT}")
    print(f"  并发上限    →  {MAX_WORKERS}（排队上限 {MAX_ACTIVE}）")
    if a6.MOCK:
        print("  ⚠ MOCK 模式：不会真的调接口，出的是本地画的假图")
    print("  关闭窗口即停止。Ctrl+C 如果卡住（有任务在跑会等最多 300 秒），再按一次。\n")

    # 传 app 对象而不是 "app:app" 字符串：自动禁掉 reload。
    # reload 会起子进程，任务表会分裂成两份。workers 也绝不能 > 1，同理。
    try:
        if not os.environ.get("A6_NO_BROWSER"):
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    except KeyboardInterrupt:
        # 线程池的线程是非守护的，concurrent.futures 注册了 atexit 会 join 它们，
        # 所以正常退出最长要等当前调用返回（可能 300 秒）窗口才关。
        # 要立刻退就硬退，代价是极小概率留下一个半写文件。
        print("\n强行退出。")
        os._exit(0)
