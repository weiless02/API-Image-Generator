import base64
import logging
import os
import time
from contextlib import ExitStack

import httpx
from openai import BadRequestError, OpenAI

# 脚本起点。放在 import 之后，所以不含 Python 启动和导入库的时间（那部分通常 1 秒内）
SCRIPT_T0 = time.perf_counter()

# 让 SDK 的静默重试变可见：它重发时会打印 "Retrying request to /images/edits in ..."
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ==================== 配置区：平时只改这里 ====================
BASE_DIR = r"D:\PyCharm 2025.3.2\P5\image"

# 参考图文件名，按顺序填 —— 顺序会影响模型对「图 1 / 图 2」的理解
INPUT_IMAGES = [
    "img_2.png",
]

OUTPUT_IMAGE = "zmd.png"   # 输出文件名，会存进 aipicture/年月日/ 里
OUTPUT_ROOT = "aipicture"   # 输出根目录名，建在 BASE_DIR 下

PROMPT = "把图片人物的白色外套去掉"

MODEL = "gpt-image-2.5"     # 编辑接口的模型名，按中转站文档写gpt-image-2.5-sunburst/gpt-image-2.5-flare
SIZE = "1024x1024"
# ==========================================================

IMAGE_PATHS = [os.path.join(BASE_DIR, n) for n in INPUT_IMAGES]

# 中转站后面挂着多个上游，对多图 multipart 的字段名要求不一致：
# 有的只认重复的 image（SDK 单图用的就是这个），有的只认 SDK 多图默认的 image[]。
# 认不出的会回 400 image_input_required，而 SDK 不重试 400（只重试 408/409/429/5xx），
# 所以这个 400 得我们自己接手，换另一种字段名再来一次。
FIELD_ORDER = ["image", "image[]"]


class FieldNameClient(httpx.Client):
    """把 SDK 多图请求里的 image[] 换成 field 指定的名字；field='image[]' 表示原样不改。"""

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
    """取 API key：优先同目录的 env.py，其次环境变量 A6_API_KEY。

    env.py 是本地文件，被根目录 .gitignore 排除，不会上传；模板见 env.example.py。
    【为什么不写默认值】key 写死在源码里，推到 GitHub 就等于公开。
    """
    try:
        from env import A6_API_KEY as key      # 同目录的 env.py
    except ImportError:
        key = None
    if not key:
        key = os.environ.get("A6API_KEY")
    if not key:
        raise RuntimeError(
            "没有找到 API key，无法调用接口。\n"
            "本地运行：把同目录的 env.example.py 复制成 env.py，填上你的 key；\n"
            "或者设环境变量 A6API_KEY。"
        )
    return key


def _make_client(field):
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
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0),
        ),
    )


_clients = {f: _make_client(f) for f in FIELD_ORDER}
_preferred = {"field": FIELD_ORDER[0]}  # 记住能用的是哪个，优先用它


def edit_image(prompt, **kwargs):
    """多图编辑。字段名两种都试，谁能用就用谁。"""
    order = [_preferred["field"]] + [f for f in FIELD_ORDER if f != _preferred["field"]]
    last_err = None
    # ExitStack 保证所有文件句柄最后都被关掉（普通 with 一次只能管一个文件）。
    # 文件对象在多次尝试间可以复用：httpx 每次编码都会把指针重置回开头（实测过）。
    with ExitStack() as stack:
        images = [stack.enter_context(open(p, "rb")) for p in IMAGE_PATHS]
        for field in order:
            try:
                resp = _clients[field].images.edit(image=images, prompt=prompt, **kwargs)
                _preferred["field"] = field
                return resp
            except BadRequestError as e:
                if "image_input_required" not in str(e):
                    raise  # 是别的问题（prompt / 额度 / 模型名），不关字段名的事
                print(f"[提示] 字段名 {field!r} 被中转站拒绝，换另一种重试…")
                last_err = e
    raise RuntimeError(f"两种字段名都被拒了，中转站可能又改接口了。原始报错：\n{last_err}")


t0 = time.perf_counter()
resp = edit_image(prompt=PROMPT, model=MODEL, size=SIZE, n=1)
elapsed = time.perf_counter() - t0

# 返回格式也随商家变：多数给 base64，有的只给图片链接，两种都兼容
print(resp.model_dump_json(indent=2)[:2000])
item = resp.data[0]
if item.b64_json:
    img_bytes = base64.b64decode(item.b64_json)
elif item.url:
    print("[提示] 这个商家只返回图片链接，正在下载…")
    img_bytes = httpx.get(item.url, timeout=300.0).content  # 同样自动走系统代理
else:
    raise RuntimeError(f"返回里既没有 b64_json 也没有 url：{resp}")

# 按年月日建子文件夹，例如 aipicture/2026-09-24/
# 用 BASE_DIR 拼而不是写 "./aipicture"：工作目录未必是脚本所在目录
#（PyCharm 默认是项目根目录，我们这次会话开头就踩过这个坑）
day_dir = os.path.join(BASE_DIR, OUTPUT_ROOT, time.strftime("%Y-%m-%d"))
os.makedirs(day_dir, exist_ok=True)

# 同名的图自动加序号，免得当天第二次运行把第一次的输出覆盖掉
stem, ext = os.path.splitext(OUTPUT_IMAGE)
out_path = os.path.join(day_dir, OUTPUT_IMAGE)
seq = 2
while os.path.exists(out_path):
    out_path = os.path.join(day_dir, f"{stem}_{seq}{ext}")
    seq += 1

with open(out_path, "wb") as f:
    f.write(img_bytes)

total = time.perf_counter() - SCRIPT_T0
print(f"搞定，图片存到 {out_path}")
print(f"[耗时] 脚本总计 {total:.1f} 秒")
print(f"[耗时] 其中接口调用 {elapsed:.1f} 秒，本地开销 {total - elapsed:.1f} 秒")
if elapsed > 300:
    # read 超时是 300 秒。低于 300 秒就返回，说明只发了一次请求；
    # 超过 300 秒必然是第一次卡到超时、SDK 又静默重发了一次。
    print("[提醒] 超过 300 秒，几乎可以肯定发生了重试，往上翻日志找 Retrying")
