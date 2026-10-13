"""给网页版（app.py）用。

=============================================================================
⚠ 本文件里的 FieldNameClient / _make_client / 字段名重试顺序，是从
   image2.5.py 第 39-66 行与 85-98 行【拷贝】过来的，不是 import。

   为什么拷贝而不是共用：image2.5.py 在模块最外层就直接发请求，import 它
   等于立刻生一张图，没法当库用。而你要求 image2.5.py 一个字符都不改，
   所以这两份代码从今天起独立演进。

   ⇒ 再改接口时，下面这几处要【同步改两个文件】：
        FieldNameClient         （本文件 与 image2.5.py:42-66）
        _make_client            （本文件 与 image2.5.py:85-98）
        字段名重试顺序 FIELD_ORDER（本文件 与 image2.5.py:39）
     日志配置、BASE_DIR、API key 也各有一份。
=============================================================================
"""

import base64
import io
import logging
import mimetypes
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

# 注意：这里【故意不】调 logging.basicConfig。
# image2.5.py 里那句会劫持 root logger，把 uvicorn 的日志配置弄乱。
log = logging.getLogger(__name__)

# ==================== 配置区 ====================

# 用 __file__ 推导而不是写死路径：PyCharm 的工作目录未必是脚本所在目录，
# 网页版更是完全不吃 cwd。这顺带修掉了 image2.5.py 注释里提到的那个坑。
BASE_DIR = Path(__file__).resolve().parent

# 输出根目录。图仍然存进 image2.5.py 那棵树，两边共用，历史才是同一份。
#
# 【为什么不能写成 BASE_DIR / "aipicture"】网页版被从 P5\image 搬到了 P5\WEB，
# 而 __file__ 会跟着搬 —— 那样输出目录就变成 WEB\aipicture（空的），
# 界面上看不到任何历史，还会悄悄往新目录里写图。
# 所以这里显式指回 image\aipicture，并留一个环境变量可以覆盖。
#
# 想让网页版自己用一个目录：设 A6_OUTPUT_DIR 就行。
OUTPUT_ROOT = Path(
    os.environ.get("A6_OUTPUT_DIR") or (BASE_DIR.parent / "image" / "aipicture")
)
DEFAULT_NAME = "image"                   # 前端没填文件名时用这个

MOCK = os.environ.get("A6_MOCK", "").strip().lower() not in ("", "0", "false", "no")

# 模型线路。服务端后面挂着多个上游，-sunburst / -flare 互为备份：
# 主线路 429/5xx/超时时自动换下一条，但内容策略类错误换线路也没用。
MODEL_LINES = [
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-2.5",
]
DEFAULT_MODEL = MODEL_LINES[0]

# 尺寸下拉框的选项，按总像素从大到小排。
#
# 分两类，代码本身不会告诉你是哪类，这里标一下：
#
#   【SDK 声明过的】256x256 / 512x512 / 1024x1024 / 1536x1024 / 1024x1536 / auto
#     openai SDK 里 images.edit 的 size 类型是 Literal 列出来的。
#     其中只有 1024x1024 真正对着服务端跑通过，其余没实测。
#
#   【SDK 没声明的】2048x2048 / 2048x1152 / 3840x2160 / 2160x3840
#     这 4 个不在 Literal 里。但 size 的类型是 Union[str, Literal[...]]，
#     带上 str 之后【任何字符串都能通过检查并发出去】，SDK 不拦也不校验。
#     服务端认不认是未知的 —— 不认的话会回 400，报错会显示在任务卡片上。
#     注意这两个 4K 尺寸（3840x2160 / 2160x3840）如果服务端接受，
#     耗时和额度很可能明显高于 1024 档，别一次点一堆。
SIZES = [
    "256x256",
    "512x512",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x1152",
    "2048x2048",
    "3840x2160",
    "2160x3840",
    "auto",
]
DEFAULT_SIZE = "1024x1024"

# 张数固定 1。原来 image2.5.py / test.py 就是一张张跑的，不需要批量，
# 接口参数里也不再暴露 n —— 免得误传 n=4 一次扣四张的额度。
# 同理不暴露 quality（画质档位）：原本的脚本从来没传过它，
# 服务端认不认也没验证过，留着只是多一个可能踩坑的旋钮。
N_IMAGES = 1

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 两次调用之间的最长等待，给前端和任务表用
READ_TIMEOUT = 300.0

# 服务端后面挂着多个上游，对多图 multipart 的字段名要求不一致：
# 有的只认重复的 image（SDK 单图用的就是这个），有的只认 SDK 多图默认的 image[]。
# 认不出的会回 400 image_input_required，而 SDK 不重试 400（只重试 408/409/429/5xx），
# 所以这个 400 得我们自己接手，换另一种字段名再来一次。
FIELD_ORDER = ["image", "image[]"]

# =============================================


class NamedBytesIO(io.BytesIO):
    """带 filename 的内存文件，专给 httpx 的 multipart 用。

    必须给名字，原因在 httpx/_multipart.py:145：
        filename = Path(str(getattr(value, "name", "upload"))).name
    取不到 .name 时发的不是「没有 filename」，而是字面量 filename="upload"，
    随后 _guess_content_type("upload") 推出 application/octet-stream。
    上游可能靠扩展名判断图片格式，所以名字必须带正确后缀。
    """

    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


class FieldNameClient(httpx.Client):
    """把 SDK 多图请求里的 image[] 换成 field 指定的名字；field='image[]' 表示原样不改。

    —— 以下到 send() 结束，与 image2.5.py:42-66 逐字相同，改这里要同步改那边。
    """

    def __init__(self, field, **kwargs):
        super().__init__(**kwargs)
        self._field = field

    def send(self, request, **kwargs):
        if self._field == "image[]" or not request.headers.get("content-type", "").startswith("multipart/form-data"):
            return super().send(request, **kwargs)

        body = request.read()
        if b'name="image[]"' in body:  # 只有多图才会出现这个名字
            body = body.replace(b'name="image[]"', b'name="%s"' % self._field.encode())
        # Content-Length 要剔除：改了字段名长度会变，交给 httpx 重算
        headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
        return super().send(
            httpx.Request(
                request.method, request.url,
                headers=headers,
                content=body,
                extensions=request.extensions,  # 保住 SDK 设的单个请求超时
            ),
            **kwargs,
        )


def _require_api_key():
    """从环境变量取 key；取不到就抛错，并说清楚本地怎么设。

    【为什么没有默认值】key 写死在源码里，推到 GitHub 就等于公开。
    本地设置方法见同目录的 key.bat.example。
    """
    key = os.environ.get("A6API_KEY")
    if not key:
        raise RuntimeError(
            "没有读到环境变量 A6API_KEY，无法调用接口。\n"
            "本地运行：把 WEB/key.bat.example 复制成 WEB/key.bat，填上你的 key，\n"
            "然后用 启动.bat 启动（它会自动加载 key.bat）。"
        )
    return key


def _make_client(field):
    """—— 与 image2.5.py:85-98 相同，唯一差别是 max_retries=0。"""
    return OpenAI(
        # key 只从环境变量读，见上面的 _require_api_key
        api_key=_require_api_key(),
        base_url="https://api.a6api.com/v1",  # ← 结尾一定要带 /v1
        # 不传 proxy：httpx 会自动读 Windows 系统代理（经 urllib 查注册表），
        # 代理软件开着就走代理，不需要在这里配。想强制直连就加 trust_env=False。
        http_client=FieldNameClient(
            field,
            # read 是「两次收到数据之间的最大间隔」，不是总时长。生成期间服务端会
            # 静默几十秒，所以必须给足；connect 反而要短，避免连不上时干等。
            timeout=httpx.Timeout(connect=10.0, read=READ_TIMEOUT, write=300.0, pool=10.0),
        ),
        # 【关键】SDK 默认重试 2 次。配上 300 秒 read 超时，一次点击最长可能
        # 900 秒才返回，而且上游可能已经生成并【计费了两次】。
        # image2.5.py:162 那句「超过 300 秒几乎可以肯定发生了重试」说的就是这个。
        # 网页版必须让一次点击对应一次调用，重试决策收到自己手里。
        max_retries=0,
    )


# 懒加载：原脚本在模块级建两个连接池，网页版 import 时不该白建，
# 而且 base_url 配错的话 import 阶段就炸，报错难定位。
_clients: dict[str, OpenAI] = {}
_clients_lock = threading.Lock()

_preferred = {"field": FIELD_ORDER[0]}          # 记住能用的字段名
_preferred_model = {"line": None}               # 记住能用的线路


def get_client(field: str) -> OpenAI:
    with _clients_lock:
        if field not in _clients:
            _clients[field] = _make_client(field)
        return _clients[field]


def _field_order():
    return [_preferred["field"]] + [f for f in FIELD_ORDER if f != _preferred["field"]]


def _model_order(model: str):
    order = [model]
    if _preferred_model["line"] and _preferred_model["line"] not in order:
        order.append(_preferred_model["line"])
    order += [m for m in MODEL_LINES if m not in order]
    return order


# ==================== 调用 ====================


def generate_image(*, prompt, model, size, **extra):
    """文生图（对应 test.py 的 images.generate）。JSON 请求，不走 FieldNameClient。"""
    if MOCK:
        return _mock_response(prompt, size, []), "mock"

    def call(line):
        return get_client("image[]").images.generate(
            prompt=prompt, model=line, size=size, n=N_IMAGES, **extra
        )

    return _call_with_fallback(model, call)


def edit_image(images, *, prompt, model, size, input_fidelity=None, **extra):
    """图生图 / 多图编辑。

    images 必须是 list[file-like]，元素要有 .name（见 NamedBytesIO）。
    """
    # 必须显式断言是 list：openai 的 is_list 实现是 isinstance(obj, list)，
    # 传 tuple 不进数组分支，会被当成【单个】file content，
    # 于是变成 ("image[]", (图1, 图2)) —— httpx 把二元组当 (filename, fileobj)，
    # 结果静默只传了一张图，且不报任何错。
    assert isinstance(images, list), (
        f"images 必须是 list，收到 {type(images).__name__}。"
        "tuple 会被 openai 当成单个文件，静默只传第一张。"
    )
    if not images:
        raise ValueError("图生图至少需要一张参考图")

    if MOCK:
        return _mock_response(prompt, size, images), "mock"

    kwargs = {}
    if input_fidelity:
        kwargs["input_fidelity"] = input_fidelity
    kwargs.update(extra)

    def call(line):
        return _edit_once(images, prompt=prompt, model=line, size=size, **kwargs)

    return _call_with_fallback(model, call)


def _edit_once(images, *, prompt, model, size, **kwargs):
    """字段名两种都试，谁能用就用谁。"""
    last_err = None
    for field in _field_order():
        # 每次尝试前显式回卷。httpx 的 render_data 自己会 seek(0)，
        # 但不依赖这个内部行为，显式做一次更稳（换 openai/httpx 版本也不怕）。
        for img in images:
            if hasattr(img, "seek"):
                img.seek(0)
        try:
            resp = get_client(field).images.edit(
                image=images, prompt=prompt, model=model, size=size, n=N_IMAGES, **kwargs
            )
            _preferred["field"] = field
            return resp
        except BadRequestError as e:
            if "image_input_required" not in str(e):
                raise  # 是别的问题（prompt / 额度 / 模型名），不关字段名的事
            log.warning("字段名 %r 被服务端拒绝，换另一种重试", field)
            last_err = e
    raise RuntimeError(f"两种字段名都被拒了，服务端可能又改接口了。原始报错：\n{last_err}")


class EmptyImageData(RuntimeError):
    """响应里没有图片数据。

    服务端会在 HTTP 200 的 body 里包错误：上游挂掉时返回
    {"data": null, "error": {"code": "upstream_error", ...}}，状态码却是 200。
    SDK 只看状态码，所以既不重试也不抛错，直接把 data=None 交给调用方，
    最后在 resp.data[0] 处炸出一句毫无信息量的
    TypeError: 'NoneType' object is not subscriptable。
    （ImagesResponse.data 的类型是 Optional[List[Image]]，缺 data 也不触发 pydantic 校验。）

    这里把它升级成「可换线路重试的失败」，因为绝大多数情况就是这条线路的上游挂了。
    """


def _check_has_data(resp, line):
    if not getattr(resp, "data", None):
        err = getattr(resp, "error", None)
        raise EmptyImageData(
            f"线路 {line} 的响应里没有图片数据（上游可能挂了，错误被包在 200 里）。error={err}"
        )


def _call_with_fallback(model, call):
    """依次试各条线路，返回 (resp, 实际用到的线路)。"""
    last_err = None
    for line in _model_order(model):
        try:
            resp = call(line)
            _check_has_data(resp, line)
            _preferred_model["line"] = line
            return resp, line
        except BadRequestError:
            # prompt 被拦、额度不够、模型名非法 —— 换线路解决不了，直接抛
            raise
        except (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError) as e:
            log.warning("线路 %s 失败（%s），换下一条", line, type(e).__name__)
            last_err = e
        except EmptyImageData as e:
            log.warning("线路 %s 返回空数据，换下一条", line)
            last_err = e
    raise RuntimeError(f"所有线路都失败了。最后一次报错：{last_err}")


def extract_images(resp) -> list[bytes]:
    """从响应里取出图片字节。返回格式随商家变：多数给 base64，有的只给链接。"""
    _check_has_data(resp, _preferred_model["line"] or "?")
    out = []
    for item in resp.data:
        if getattr(item, "b64_json", None):
            out.append(base64.b64decode(item.b64_json))
        elif getattr(item, "url", None):
            log.info("这个商家只返回图片链接，正在下载…")
            out.append(httpx.get(item.url, timeout=READ_TIMEOUT).content)  # 同样自动走系统代理
    if not out:
        raise RuntimeError(f"返回里既没有 b64_json 也没有 url：{resp}")
    return out


# ==================== 落盘 ====================

# Windows 保留设备名，直接当文件名会出各种怪错
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """把前端传来的文件名洗成安全的裸文件名。

    必须去掉目录部分：不然前端传 "..\\..\\x.png" 就写到 aipicture 外面去了。
    """
    name = Path(name or "").name          # 只取最后一段，路径分隔符在这里被吃掉
    name = _BAD_CHARS.sub("_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    if stem.upper() in _RESERVED:
        stem = f"_{stem}"
    return (stem[:80] + ext.lower()) or f"{DEFAULT_NAME}.png"


def save_image(data: bytes, name_hint: str = DEFAULT_NAME) -> Path:
    """存进 aipicture/年月日/，同名自动加序号。

    用 O_CREAT|O_EXCL 而不是先 exists() 再写：网页版有 2 个 worker 线程，
    原来那种「先查再写」会有 TOCTOU 竞态，两个线程同时判定同名不存在，
    然后互相覆盖。O_EXCL 让内核保证原子性。
    """
    day_dir = OUTPUT_ROOT / time.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    stem, ext = os.path.splitext(safe_name(name_hint))
    if ext not in IMAGE_EXTS:
        ext = ".png"

    seq = 1
    while True:
        name = f"{stem}{ext}" if seq == 1 else f"{stem}_{seq}{ext}"
        path = day_dir / name
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            seq += 1
            if seq > 9999:
                raise RuntimeError(f"{day_dir} 下同名文件太多了")
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path


# ==================== 历史（纯扫目录，不写任何元数据文件）====================


def list_outputs(limit: int = 60, offset: int = 0) -> dict:
    """扫 aipicture 目录当历史记录。

    不维护集中索引是故意的：image2.5.py 也往同一棵树写，只要还有第二个写入口，
    索引就必然漂移，且漂移方向是「图在、索引没有」—— 脚本生成的图会从历史里消失。
    直接扫目录则天然正确处理手删、手放、脚本生成三种情况。
    """
    items = []
    if OUTPUT_ROOT.exists():
        for day in OUTPUT_ROOT.iterdir():
            if not day.is_dir() or day.name.startswith("."):   # 跳过 .thumbs 之类
                continue
            for p in day.iterdir():
                if p.name.startswith(".") or p.suffix.lower() not in IMAGE_EXTS:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue           # 扫描期间被删掉/占用，跳过就好
                rel = f"{day.name}/{p.name}"
                items.append({
                    "rel": rel,
                    # 中文文件名必须 quote，否则浏览器可能取不到图。
                    # 由后端直接给完整 url，前端别自己拼字符串。
                    "url": "/images/" + quote(rel, safe="/"),
                    "name": p.name,
                    "day": day.name,
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"total": len(items), "items": items[offset:offset + limit], "root": str(OUTPUT_ROOT)}


def upload_name(filename: str | None, content_type: str | None, idx: int) -> str:
    """给上传的图起个带正确扩展名的名字（httpx 靠它推 Content-Type）。"""
    name = Path(filename or "").name
    if name and os.path.splitext(name)[1]:
        return safe_name(name)
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    return f"image_{idx}{ext}"


# ==================== MOCK ====================
# 真调用一次要 60~300 秒，调前端时会疯掉。A6_MOCK=1 用 Pillow 本地画一张，
# 整条链路（提交 → 轮询 → 落盘 → 历史）秒级跑通。


class _MockItem:
    def __init__(self, b64):
        self.b64_json = b64
        self.url = None


class _MockResp:
    def __init__(self, b64):
        self.data = [_MockItem(b64)]


def _mock_response(prompt, size, images) -> _MockResp:
    from PIL import Image, ImageDraw

    # 想在 MOCK 下验证「排队中 / 生成中」的界面表现，就把 A6_MOCK_DELAY 设成
    # 比如 20，再看前端秒表走不走、并发第二个任务会不会进队列。
    delay = float(os.environ.get("A6_MOCK_DELAY", "0") or 0)
    if delay > 0:
        time.sleep(delay)

    # A6_MOCK_FAIL=1 让任务必定失败，用来验证错误信息能不能正确显示到界面上。
    # 这条路径在正常使用中几乎碰不到，也最容易悄无声息地坏掉。
    if os.environ.get("A6_MOCK_FAIL", "").strip().lower() not in ("", "0", "false", "no"):
        raise RuntimeError(
            "模拟失败：这是 A6_MOCK_FAIL 造出来的错误，用来检查失败时的界面显示"
        )

    try:
        w, h = (int(x) for x in str(size).split("x"))
    except ValueError:
        w, h = 1024, 1024
    # 上限放到 4096：新加了 3840x2160 / 2160x3840 两个 4K 尺寸，
    # 卡在 2048 的话假图会被缩掉，看着和选的尺寸对不上。
    w, h = max(64, min(w, 4096)), max(64, min(h, 4096))

    img = Image.new("RGB", (w, h), (28, 32, 44))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=(120, 180, 255), width=5)
    lines = [
        "MOCK",
        f"{w}x{h}",
        f"ref={len(images)}",
        time.strftime("%H:%M:%S"),
    ]
    y = h // 6
    for s in lines:
        d.text((w // 8, y), s, fill=(230, 240, 255))
        y += max(18, h // 14)
    d.text((w // 8, y + 10), f"prompt len={len(prompt or '')}", fill=(150, 200, 160))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return _MockResp(base64.b64encode(buf.getvalue()).decode())
