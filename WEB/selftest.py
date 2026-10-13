"""金丝雀自检 —— 升级 openai / httpx 之后，先跑这个再信网页版。

锁的是两件「坏了也不会报错、只会静默出错」的事：

1. openai 给多图 multipart 用的字段名。a6.FieldNameClient 靠【字节替换】
   b'name="image[]"' 来适配服务端的两种要求。如果 openai 哪天把默认的
   array_format 从 "brackets" 改成 "repeat"（裸 image），替换就再也匹配不上，
   代码不报错，只是发错字段名 → 服务端回 400 image_input_required。
2. 替换之后确实变成了我们要的形状（两个 name="image"）。

这个文件可以随便 import openai 的私有 API —— 金丝雀的职责就是盯住它们。
它不发任何网络请求，也不消耗额度。

    python selftest.py
"""

import io
import sys
import traceback

import httpx

# Windows 控制台默认是 GBK，直接 print 中文/符号会 UnicodeEncodeError 崩掉。
# 强制按 UTF-8 输出（启动.bat 里也会 chcp 65001 配合）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import a6  # noqa: E402

FAILED = []


def check(label):
    def deco(fn):
        try:
            fn()
        except Exception as e:
            FAILED.append(label)
            print(f"  ✗ {label}")
            traceback.print_exc()
        else:
            print(f"  ✓ {label}")
        return fn
    return deco


def _fake_image(data=b"\x89PNG-fake", name="a.png"):
    b = io.BytesIO(data)
    b.name = name
    return b


print("a6 自检")
print(f"  MOCK = {a6.MOCK}")

# ---------------------------------------------------------------- 1. 字段名约定

# 复刻 openai/resources/images.py 里对多图 edit 的调用方式。
# paths 没传 array_format，所以走默认的 "brackets"。
EDIT_PATHS = [["image"], ["image", "<array>"], ["mask"]]


@check("openai 多图字段名仍是 image[]（array_format 默认值没变）")
def _():
    from openai._utils._utils import extract_files

    files = [(_fake_image(name="a.png")), _fake_image(name="b.png")]
    pairs = extract_files({"image": files}, paths=EDIT_PATHS)
    keys = [k for k, _ in pairs]
    assert keys == ["image[]", "image[]"], (
        f"字段名约定变了，收到 {keys!r}。\n"
        "a6.FieldNameClient 的字节替换是为 b'name=\"image[]\"' 写的，"
        "现在匹配不上了 —— 需要同步改 a6.py 和 image2.5.py 的 FieldNameClient。"
    )
    assert len(pairs) == 2, f"两张图应当产出两个 part，实际 {len(pairs)} 个"


@check("多图必须放在 list 里（tuple 会静默只传一张）")
def _():
    from openai._utils._utils import extract_files

    # list → 两个 part，正确
    pairs = extract_files({"image": [_fake_image(), _fake_image()]}, paths=EDIT_PATHS)
    assert [k for k, _ in pairs] == ["image[]", "image[]"], pairs

    # tuple → 不进数组分支，退化成【一个】file content。
    # 这正是 a6.edit_image 里那句 isinstance(images, list) 断言要挡的情况。
    pairs = extract_files({"image": (_fake_image(), _fake_image())}, paths=EDIT_PATHS)
    assert len(pairs) == 1, f"预期 tuple 退化成 1 个 part（这就是要挡的坑），实际 {len(pairs)}"


# ------------------------------------------------------- 2. 字节替换真的命中

@check("FieldNameClient 把 image[] 换成 image，且两个 part 都换到")
def _():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        seen["ctype"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"data": []})

    client = a6.FieldNameClient("image", transport=httpx.MockTransport(handler))
    req = httpx.Request(
        "POST", "https://example.invalid/v1/images/edits",
        files=[("image[]", _fake_image(data=b"AAA", name="a.png")),
               ("image[]", _fake_image(data=b"BBB", name="b.png"))],
    )
    client.send(req)

    assert seen["ctype"].startswith("multipart/form-data"), seen["ctype"]
    body = seen["body"]
    assert body.count(b'name="image[]"') == 0, "还有没换掉的 image[]"
    assert body.count(b'name="image"') == 2, f"预期 2 个 name=\"image\"，实际 {body.count(b'name=\"image\"')}"
    assert b"AAA" in body and b"BBB" in body, "两张图的内容应当都在 body 里"


@check("field='image[]' 时原样不改")
def _():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"data": []})

    client = a6.FieldNameClient("image[]", transport=httpx.MockTransport(handler))
    client.send(httpx.Request(
        "POST", "https://example.invalid/v1/images/edits",
        files=[("image[]", _fake_image()), ("image[]", _fake_image())],
    ))
    assert seen["body"].count(b'name="image[]"') == 2


@check("同一组 BytesIO 反复编码，两张图内容都还在（不存在第二张传空）")
def _():
    imgs = [a6.NamedBytesIO(b"AAA", "a.png"), a6.NamedBytesIO(b"BBB", "b.png")]

    # 注意：每次都生成新的随机 boundary，所以两次的原始字节本来就【不该】相等。
    # 要验的是「图片内容还在、总长度一致」，不是逐字节相同。
    def encode():
        return httpx.Request("POST", "https://example.invalid/x",
                             files=[("image[]", i) for i in imgs]).read()

    def intact(body, tag):
        assert b"AAA" in body, f"{tag}：第一张图丢了"
        assert b"BBB" in body, f"{tag}：第二张图丢了"
        return len(body)

    n1 = intact(encode(), "首次编码")

    # 走一遍 FieldNameClient 那条路径：request.read() 会消费掉流并替换 stream，
    # 之后重新编码必须还能拿到两张图
    a6.FieldNameClient("image", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": []})
    )).send(httpx.Request("POST", "https://example.invalid/x",
                          files=[("image[]", i) for i in imgs]))

    n2 = intact(encode(), "替换路径之后再编码")
    assert n1 == n2, f"两次编码长度不同：{n1} vs {n2}（boundary 随机但长度固定，应当一致）"


@check("NamedBytesIO 的 .name 会被 httpx 用作 multipart filename")
def _():
    imgs = [a6.NamedBytesIO(b"AAA", "img_1.png")]
    req = httpx.Request("POST", "https://example.invalid/x", files=[("image[]", i) for i in imgs])
    body = req.read()
    assert b'filename="img_1.png"' in body, body[:400]
    assert b'filename="upload"' not in body, "落到 httpx 的默认名了，说明 .name 没生效"


# ------------------------------------------- 3. 200 响应里包着错误（本服务端老毛病）


class _FakeItem:
    def __init__(self, b64):
        self.b64_json = b64
        self.url = None


class _FakeResp:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


@check("data 为空（含 data=null）会被识别出来")
def _():
    empty = _FakeResp(None, error={"code": "upstream_error", "message": "Upstream error, please retry."})
    try:
        a6._check_has_data(empty, "gpt-image-2.5")
    except a6.EmptyImageData as e:
        assert "upstream_error" in str(e), str(e)
    else:
        raise AssertionError("data=None 却没报错")

    try:
        a6._check_has_data(_FakeResp([]), "x")
    except a6.EmptyImageData:
        pass
    else:
        raise AssertionError("data=[] 却没报错")


@check("某条线路返回空数据时自动换下一条，而不是直接炸 TypeError")
def _():
    tried = []

    def call(line):
        tried.append(line)
        if len(tried) == 1:
            return _FakeResp(None, error={"code": "upstream_error"})
        return _FakeResp([_FakeItem("aGk=")])

    resp, used = a6._call_with_fallback("gpt-image-2.5-sunburst", call)
    assert len(tried) == 2, f"应当只试两条线路，实际试了 {tried}"
    assert used == tried[1], f"真正生效的应当是 {tried[1]}，返回了 {used}"
    assert a6.extract_images(resp) == [b"hi"]


@check("所有线路都空时报错可读，而不是 'NoneType' is not iterable")
def _():
    def call(line):
        return _FakeResp(None, error={"code": "upstream_error", "message": "Upstream error"})

    try:
        a6._call_with_fallback("gpt-image-2.5", call)
    except RuntimeError as e:
        msg = str(e)
        assert "所有线路都失败了" in msg, msg
        assert "upstream_error" in msg, msg
        assert "NoneType" not in msg, msg
    else:
        raise AssertionError("全线空却没报错")


# ------------------------------------------------------------ 4. 本地副作用

@check("safe_name 挡得住路径穿越")
def _():
    for bad in [r"..\..\evil.png", "../../evil.png", r"C:\Windows\evil.png", "a/b/c.png"]:
        got = a6.safe_name(bad)
        assert "/" not in got and "\\" not in got and ".." not in got, f"{bad!r} -> {got!r}"
    assert a6.safe_name("") == "image.png"
    assert a6.safe_name("zmd.png") == "zmd.png"


@check("save_image 同名会加序号，且不覆盖已有文件")
def _():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        real_root = a6.OUTPUT_ROOT
        a6.OUTPUT_ROOT = Path(td)
        try:
            p1 = a6.save_image(b"one", "canary.png")
            p2 = a6.save_image(b"two", "canary.png")
            assert p1 != p2, "第二次应当换名，不能覆盖"
            assert p1.read_bytes() == b"one" and p2.read_bytes() == b"two"
            assert p2.name == "canary_2.png", p2.name
        finally:
            a6.OUTPUT_ROOT = real_root


@check("list_outputs 能在空目录/不存在目录上安全工作")
def _():
    import tempfile
    from pathlib import Path

    real_root = a6.OUTPUT_ROOT
    a6.OUTPUT_ROOT = Path(tempfile.gettempdir()) / "a6-does-not-exist-xyz"
    try:
        out = a6.list_outputs()
        assert out["total"] == 0 and out["items"] == [], out
    finally:
        a6.OUTPUT_ROOT = real_root


# ---------------------------------------------------------------- 5. MOCK 通路

@check("MOCK 模式能产出一张合法 PNG（且不发网络请求）")
def _():
    if not a6.MOCK:
        print("        (跳过：未设 A6_MOCK=1)")
        return
    resp, line = a6.generate_image(prompt="测试", model=a6.DEFAULT_MODEL, size="256x256", n=1)
    data = a6.extract_images(resp)[0]
    assert data[:8] == b"\x89PNG\r\n\x1a\n", data[:8]


print()
if FAILED:
    print(f"✗ {len(FAILED)} 项未通过：")
    for f in FAILED:
        print(f"    - {f}")
    sys.exit(1)
print("✓ 全部通过。")
