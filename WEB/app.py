"""生图网页版 —— 本机自用。

    python app.py            # 真调用
    A6_MOCK=1 python app.py  # 本地假图，秒级，用来调前端

设计上的三条硬约束（改代码前先读这段）：

1. API key 存在后端（WEB/settings.json，已被 .gitignore 排除）。前端平时只拿到掩码。

   ⚠️【2026-10-14 起的有意放宽】用户明确要求能在设置面板点「显示」看到完整 key，
   所以多了 GET /api/settings/api-key 这个端点，它会返回明文。
   这不是 bug，别顺手"修"回去。

   它能成立，全靠下面第 3 条：只监听 127.0.0.1，且 Host / Origin 都校验过。
   【一旦把服务改成 0.0.0.0、或挂到反向代理后面对外，这个端点就是致命的】
   —— 那时必须先把 /api/settings/api-key 删掉，再谈对外的事。

2. 生图要 60~300 秒，不能同步等 —— 提交拿 task_id，前端轮询。
3. 只监听 127.0.0.1。绑 0.0.0.0 等于把无鉴权的生图接口暴露给整个局域网，
   同 WiFi 下任何人都能烧你的额度。

已知限制：任务表在内存里，重启后丢失（正在跑的那次随进程消失）。
已落盘的图不受影响，会出现在历史里。

用户设置（图片保存位置 / API key）存 WEB/settings.json，读写都在 settings_store.py。
用户不用碰代码，在网页左下角的「设置」里改。
"""

import asyncio
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import a6
import settings_store

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

# 能不能弹原生文件夹选择框。这里只代表「tkinter 能 import」，不代表当前会话
# 真能弹出来（无 GUI 会话、驱动异常都会在 Tk() 那一步才炸），所以端点里还要再兜一层。
try:
    import tkinter  # noqa: F401

    CAN_PICK_FOLDER = True
except Exception:
    CAN_PICK_FOLDER = False

# 每项设置实际是从哪来的，只为了在界面和启动横幅上说清楚，免得用户
# 「明明改了却不生效」却找不到原因。
RUNTIME = {"output_root_source": "default", "key_source": "none"}

# 【必须在任何人改它们之前记下来】这两个是「回到默认」时的目标值。
# a6.OUTPUT_ROOT 是可变的：_apply_settings 每次都可能重新赋值，等到那时再去
# 推导默认值，拿到的会是被上一次设置改过的旧值。
_DEFAULT_OUTPUT_ROOT = a6.OUTPUT_ROOT
_ENV_KEY_AT_START = os.environ.get("A6API_KEY") or None


def _apply_settings(s: dict) -> None:
    """把设置落到运行时：a6.OUTPUT_ROOT 和环境变量 A6API_KEY。

    【调用时机是契约】必须等新值【成功写进 settings.json 之后】再调这里，
    这样盘上和内存里永远一致，不会出现「文件没写成、目录却已经换了」。

    每项都是 settings > 环境变量 > 内置默认 的顺序。环境变量留给 PyCharm
    Run Configuration 和临时调试用，所以没删，但优先级让给设置面板。

    【每个分支都必须赋值】不能只在「设置里有值」时才写。用户把 key 或目录
    清空之后，如果不主动恢复，运行时还停在上一轮的值上 —— 界面上显示已经
    回到默认，实际却照旧用着旧的，这种 bug 用户根本无从发现。
    """
    # --- 图片保存位置 ---
    if s.get("output_dir"):
        a6.OUTPUT_ROOT = Path(s["output_dir"])
        RUNTIME["output_root_source"] = "settings"
    else:
        a6.OUTPUT_ROOT = _DEFAULT_OUTPUT_ROOT      # 里面已经含了 A6_OUTPUT_DIR 的覆盖
        RUNTIME["output_root_source"] = "env" if os.environ.get("A6_OUTPUT_DIR") else "default"

    # --- API key。注意变量名没有下划线：A6API_KEY ---
    if s.get("api_key"):
        os.environ["A6API_KEY"] = s["api_key"]
        RUNTIME["key_source"] = "settings"
    elif _ENV_KEY_AT_START:
        os.environ["A6API_KEY"] = _ENV_KEY_AT_START
        RUNTIME["key_source"] = "env"
    else:
        # 真的要删掉。上一轮可能就是我们自己写进去的，不删等于清不掉。
        os.environ.pop("A6API_KEY", None)
        RUNTIME["key_source"] = "none"

    # 换没换 key 都清一次。代价近乎为零，省得再去比对，逻辑最简单。
    # 不清的话，用户改完 key 还是用缓存里的旧客户端发请求（见 a6.reset_clients）。
    a6.reset_clients()


_settings = settings_store.load()

# 一次性迁移：设置里没有 key，但环境变量里有 —— 多半是老的 启动.bat 刚从
# key.bat 里设进来的。写进设置之后就没人再需要 key.bat 了。
if not _settings.get("api_key") and (os.environ.get("A6API_KEY") or "").strip():
    try:
        _settings = settings_store.save(api_key=os.environ["A6API_KEY"].strip())
        print(
            f"\n  [迁移] 已把环境变量里的 API key 存进 {settings_store.SETTINGS_PATH.name}，"
            f"\n         以后换 key 用网页左下角的「设置」，WEB/key.bat 可以删掉了。"
        )
    except Exception as e:
        # 目录只读之类。不致命：这次照样能用环境变量里的 key 跑。
        print(f"\n  [迁移] key 没能写进设置（{e}），本次先用环境变量里的。")

_apply_settings(_settings)


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
        "output_root_source": RUNTIME["output_root_source"],
        # 只给布尔，不给 key 本身 —— 首页据此提示「还没配置 API key」
        "has_api_key": bool(os.environ.get("A6API_KEY")),
        "key_source": RUNTIME["key_source"],
        "can_pick_folder": CAN_PICK_FOLDER,
        "settings_path": str(settings_store.SETTINGS_PATH),
    }


# ==================== 设置 ====================


def _mask(key: str) -> str:
    """sk-3ku3gJ1h...MQ57 —— 够用户认出是哪一把，又不足以拿去用。"""
    if not key:
        return ""
    return f"{key[:8]}…{key[-4:]}" if len(key) >= 14 else "••••••"


def _settings_view() -> dict:
    s = settings_store.load()
    key = s.get("api_key") or os.environ.get("A6API_KEY") or ""
    return {
        "settings_path": str(settings_store.SETTINGS_PATH),
        "output_dir": s.get("output_dir"),          # 盘上存的（可能是 null = 用默认）
        "output_dir_effective": str(a6.OUTPUT_ROOT),  # 眼下真正生效的
        "output_dir_source": RUNTIME["output_root_source"],
        "default_dir": str(settings_store.DEFAULT_OUTPUT_DIR),
        "api_key_masked": _mask(key),
        "has_api_key": bool(key),
        "key_source": RUNTIME["key_source"],
    }


@app.get("/api/settings")
def api_get_settings():
    """设置面板打开时读。**永远不返回完整 key**，只给掩码。"""
    return _settings_view()


@app.get("/api/settings/api-key")
def api_get_settings_key(request: Request):
    """给「显示」按钮用的，唯一会吐明文 key 的端点。

    比同目录其它端点多一道闸：只认本页自己发起的同源请求。
    因为 guard 中间件靠 Origin 判断，而 <img>/<script> 这类跨站引用【不带 Origin】，
    光靠它拦不住。Sec-Fetch-Site 是浏览器自己打的、页面改不了，正好补上这个洞。

    放宽这条约束的理由和被删掉的条件，写在文件开头的硬约束第 1 条。
    """
    site = request.headers.get("sec-fetch-site", "")
    if site and site not in ("same-origin", "none"):
        return JSONResponse({"error": "拒绝非本页发起的请求"}, status_code=403)

    s = settings_store.load()
    key = s.get("api_key") or os.environ.get("A6API_KEY") or ""
    if not key:
        return JSONResponse({"error": "还没有设置 API key"}, status_code=404)
    return {"api_key": key}


class SettingsPatch(BaseModel):
    # 三态：字段缺失或 null = 这一项不动；空字符串 = 清空、回到默认。
    # 少了这一层，「只改目录、不动 key」和「把 key 删掉」就分不开了。
    output_dir: str | None = None
    api_key: str | None = None


@app.post("/api/settings")
def api_save_settings(patch: SettingsPatch):
    """保存设置。**必须先验证、再落盘、最后才改内存。**

    任何一步失败都保证当前生效的目录和 key 一个字都没动 —— 用户填错一个盘符
    就把好端端的历史弄没，是这次改动最不能出的事故。
    """
    row = {}

    # ---- 阶段一：校验。只算，不碰任何全局状态 ----
    if patch.output_dir is not None:
        raw = patch.output_dir.strip()
        if raw == "":
            row["output_dir"] = None            # 显式清空 → 回默认目录
        else:
            try:
                row["output_dir"] = str(settings_store.normalize_output_dir(raw))
            except settings_store.InvalidSetting as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            except OSError as e:
                return JSONResponse({"error": f"路径不可用：{e}"}, status_code=400)

    if patch.api_key is not None:
        row["api_key"] = patch.api_key.strip() or None

    # ---- 阶段二：落盘。失败就原样返回，内存里一点没动 ----
    try:
        settings_store.save(**row)
    except Exception as e:
        a6.log.exception("写 %s 失败", settings_store.SETTINGS_PATH)
        return JSONResponse(
            {"error": f"设置没保存成功（{type(e).__name__}: {e}），当前设置未改动"},
            status_code=500,
        )

    # ---- 阶段三：生效。这里只是赋值，不会失败 ----
    before = str(a6.OUTPUT_ROOT)
    _apply_settings(settings_store.load())
    view = _settings_view()

    return {
        "ok": True,
        "changed": {
            # 用「生效值是否真的变了」判断，而不是「有没有提交这个字段」——
            # 用户把路径改成一个等价写法时不该让前端白清一遍历史。
            "output_dir": before != str(a6.OUTPUT_ROOT),
            "api_key": "api_key" in row,
        },
        "warning": (
            "这是网络路径，读写可能很慢或中途掉线。"
            if row.get("output_dir") and settings_store.looks_like_unc(Path(row["output_dir"]))
            else None
        ),
        **view,
    }


# 弹窗期间占用的那把锁。同一个进程内只允许开一个对话框。
_picker_lock = threading.Lock()


@app.post("/api/pick-folder")
def api_pick_folder(initial: str = ""):
    """弹 Windows 原生文件夹选择框。

    【必须是同步 def】FastAPI 会把同步端点丢进线程池执行，事件循环不受影响，
    弹窗期间任务轮询、提交都照常。写成 async def 的话整个服务会冻住 ——
    用户在对话框上犹豫多久，网页就卡多久。
    """
    if not CAN_PICK_FOLDER:
        return JSONResponse(
            {
                "error": "这个 Python 没装 tkinter，弹不出文件夹选择框。请直接在输入框里粘贴路径。",
                "code": "tk_unavailable",
            },
            status_code=501,
        )

    # 非阻塞抢锁：第二个请求立刻被拒，不会叠出两个原生对话框。
    # 服务只有一个进程（明确禁了 reload 和 >1 workers），进程内的锁就够了。
    if not _picker_lock.acquire(blocking=False):
        return JSONResponse({"error": "已经有一个文件夹选择框开着了，先去关掉它"}, status_code=409)

    try:
        chosen = _ask_directory(initial)
    except Exception as e:
        # TclError：没有 GUI 会话、显示驱动异常等。手输路径那条路仍然可用。
        a6.log.exception("弹文件夹对话框失败")
        return JSONResponse(
            {
                "error": f"弹不出文件夹选择框（{type(e).__name__}: {e}）。请手动输入路径。",
                "code": "tk_failed",
            },
            status_code=501,
        )
    finally:
        _picker_lock.release()

    if not chosen:
        return {"ok": False, "cancelled": True}     # 取消不是错误，回 200
    return {"ok": True, "path": chosen}


def _ask_directory(initial: str) -> str:
    """真正弹窗的那段。跑在 FastAPI 的线程池线程里，创建/使用/销毁都在同一个线程内完成。

    Tk 在 Windows 上允许非主线程使用（macOS 才要求主线程，与本项目无关），
    但必须保证一个 Tk 解释器只被创建它的那个线程碰 —— 这里每次新建一个、
    用完立刻 destroy，不复用也不跨线程。
    """
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()                          # 藏掉那个空的根窗口
        root.attributes("-topmost", True)        # 否则对话框常被浏览器盖住
        root.update()

        start = initial.strip() if initial and os.path.isdir(initial.strip()) else str(a6.OUTPUT_ROOT)
        chosen = filedialog.askdirectory(
            parent=root,
            title="选择图片保存位置",
            initialdir=start,
            mustexist=True,
        )
    finally:
        root.destroy()                           # 必须销毁，否则 Tcl 解释器泄漏、线程不退出

    return os.path.normpath(chosen) if chosen else ""


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


# 启动时尽力建一次目录。建不出来【不让服务起不来】—— 用户完全可能把输出目录
# 指到一个暂时离线的网络盘或没插的 U 盘上，那时应该能启动、能进设置改回来，
# 而不是看到一个起不来的服务干瞪眼。
try:
    a6.OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
except OSError as e:
    a6.log.warning("输出目录 %s 现在建不出来：%s（服务照常启动，去设置里换一个）", a6.OUTPUT_ROOT, e)


@app.get("/images/{path:path}")
def serve_image(path: str):
    """按【请求时】的 a6.OUTPUT_ROOT 发图。

    这里不能用 StaticFiles 挂载：那样目录在启动那一刻就写死了，设置面板
    运行时换目录的整个功能就无从谈起。这就是它存在的全部理由。

    另外注意 path 已经是被 uvicorn 解过一次码的（ASGI 的 scope["path"]），
    【不要再 unquote 一次】—— 多解一层会把文件名里字面量的 % 弄坏。
    """
    root = os.path.normpath(str(a6.OUTPUT_ROOT))

    # 只接受相对路径：挡掉盘符（C:）、UNC 开头、前导分隔符、NUL
    if not path or path.startswith(("/", "\\")) or ":" in path or "\x00" in path:
        return JSONResponse({"error": "路径不合法"}, status_code=404)

    # 反斜杠一律当分隔符，normpath 折叠掉 ".."，再按大小写无关的前缀比较确认
    # 没跑出 root 之外。这套判断照抄 starlette.staticfiles 的 lookup_path：
    # 【纯字符串运算，不碰文件系统】，所以即使 root 是离线的网络盘也不会卡住
    # （Path.resolve() 在那种情况下会干等几十秒）。
    full = os.path.normpath(os.path.join(root, path.replace("\\", "/")))
    root_c, full_c = os.path.normcase(root), os.path.normcase(full)
    if full_c != root_c and not full_c.startswith(root_c + os.sep):
        return JSONResponse({"error": "路径越界"}, status_code=404)

    if not os.path.isfile(full):
        return JSONResponse({"error": "没有这张图"}, status_code=404)

    # 中间件给非 /images 的响应加了 no-store；图片反过来，给一个短缓存，
    # 否则每次重排历史都要把所有缩略图重新下一遍。
    return FileResponse(full, headers={"Cache-Control": "public, max-age=300"})


# 挂载顺序有讲究：Starlette 按注册顺序匹配，"/" 是贪婪的，
# /api 和 /images 必须先注册，否则全被根静态目录吃掉。
# /images 现在是普通路由了，但这条规矩一样适用，它必须留在这一行之前。
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


if __name__ == "__main__":
    import webbrowser

    import uvicorn

    url = f"http://{HOST}:{PORT}"
    print(f"\n  生图网页版  →  {url}")
    # 把「这个值是哪来的」一并打出来。用户在界面上改了没生效时，
    # 看这一行就知道是被环境变量接管了，还是设置压根没存进去。
    _dir_src = {"settings": "设置", "env": "环境变量", "default": "默认"}[RUNTIME["output_root_source"]]
    print(f"  输出目录    →  {a6.OUTPUT_ROOT}   （来源：{_dir_src}）")
    _key_src = {"settings": "设置", "env": "环境变量", "none": "未设置"}[RUNTIME["key_source"]]
    _key_tail = "（打开网页左下角「设置」填写）" if RUNTIME["key_source"] == "none" else ""
    print(f"  API key     →  {_key_src}{_key_tail}")
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
