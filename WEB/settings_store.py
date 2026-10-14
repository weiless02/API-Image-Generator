"""本地设置（图片保存位置 / API key）的唯一持久化点。

存 WEB/settings.json —— 里面【有 API key】，所以被根目录 .gitignore 排除，
进仓库的是模板 settings.json.example。

两条硬要求，都是被用户的真实操作逼出来的：

1. 【读坏了必须能起来】用户会用记事本手改这个文件，改出语法错误是迟早的事。
   load() 全程吞异常，回退默认值，并把坏文件另存一份，绝不因此让服务打不开。
2. 【写不能写坏】临时文件 + os.replace 原子替换。断电、强杀都不会留下半个
   JSON —— 那是下次启动直接起不来的那种坏法。

本模块刻意不 import a6、不 import fastapi：只管读写和校验，方便单测。
"""

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"

# 内置默认输出目录，和 a6.py 里那句是同一个推导，只是这里不去 import a6
# （本模块要保持能单独测、单独跑）。界面上「恢复默认目录」就是把设置清空成
# None，然后由 a6 那边的默认值接手。
DEFAULT_OUTPUT_DIR = BASE_DIR.parent / "image" / "aipicture"

VERSION = 1

# 字段语义是【三态】，不是两态，这是刻意的：
#   键缺失 / null  → 这一项从来没设过，用默认值
#   "字符串"        → 设过，用这个值
#   空字符串        → 由调用方翻译成「显式清空、回到默认」，落盘前会归一成 null
# 少了这一层，「只改目录、不动 key」和「清空 key」就分不开了。
DEFAULTS = {"version": VERSION, "output_dir": None, "api_key": None}

# 校验用的写探针文件名。挑一个不会被当成图片扫进历史的点开头的名字。
_PROBE = ".a6-write-test"

_lock = threading.Lock()


class InvalidSetting(ValueError):
    """校验没过。str(e) 是可以直接显示给用户的中文消息。"""


# ==================== 读 ====================


def load() -> dict:
    """读设置。永远不抛异常 —— 坏掉就回默认值。"""
    with _lock:
        return _load_locked()


def _load_locked() -> dict:
    data = dict(DEFAULTS)
    if not SETTINGS_PATH.exists():
        return data

    try:
        # 必须显式 utf-8：Windows 上不带 encoding 会用 cp936，路径里有中文就写坏/读坏
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("顶层不是 JSON 对象")
    except Exception as e:
        _quarantine(e)
        return data

    # 逐字段校验类型。取值再奇怪也只当「没设过」，绝不往上抛。
    for key in ("output_dir", "api_key"):
        v = raw.get(key)
        data[key] = v.strip() if isinstance(v, str) and v.strip() else None
    return data


def _quarantine(err: Exception) -> None:
    """把读不动的文件改名留证，只留一份，避免每次启动堆一个新文件。"""
    bad = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".corrupt")
    try:
        os.replace(SETTINGS_PATH, bad)
        log.warning("settings.json 读不了（%s），已改名成 %s，本次按默认值启动。", err, bad.name)
    except OSError:
        log.warning("settings.json 读不了（%s），本次按默认值启动。", err)


# ==================== 写 ====================


def save(**patch) -> dict:
    """合并后原子写回，只认 DEFAULTS 里有的键。

    写失败会【抛异常】。这是有意的：调用方据此知道盘上没写成功，可以选择
    不动内存里的状态，避免出现「文件没写成、但目录已经换了」这种割裂。
    """
    with _lock:
        data = _load_locked()
        for k in DEFAULTS:
            if k in patch:
                data[k] = patch[k]
        data["version"] = VERSION

        tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, SETTINGS_PATH)          # Windows 上也是原子的
        return data


# ==================== 校验 ====================


def normalize_output_dir(raw: str, *, create: bool = True) -> Path:
    """把用户填的一坨东西校验成可用的绝对目录，不行就抛 InvalidSetting。

    顺序有讲究：**先全部校验通过，才允许调用方去改生效目录**。用户填错一个
    盘符就把好端端的历史弄没，是这次改动最不能出的事故。
    """
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        raise InvalidSetting("路径是空的")

    s = os.path.expandvars(s)                # 容忍 %USERPROFILE% 这类写法
    p = Path(s)

    if not p.is_absolute():
        raise InvalidSetting(f"请给一个完整的绝对路径（带盘符），比如 D:\\图片\\aipicture")

    # 盘符根 / 共享根：历史是【扫目录】的，指到根会把整块盘翻一遍找图片。
    # 注意这个判断要在任何 rstrip 之前做，否则 "D:\" 会变成 "D:" 而不再是绝对路径。
    if p.parent == p:
        raise InvalidSetting("不要选盘符或共享的根目录，请在它下面新建一个文件夹")

    try:
        if p.exists() and not p.is_dir():
            raise InvalidSetting(f"{s} 是一个文件，不是文件夹")
    except OSError as e:                     # 网络路径可能探测失败
        raise InvalidSetting(f"访问不了这个路径（{e.strerror or e}）：{s}") from e

    if create and not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise InvalidSetting(f"建不出这个目录（{e.strerror or e}）：{s}") from e

    if not p.is_dir():
        raise InvalidSetting(f"{s} 不存在")

    # 写探针。这是唯一能真正证明「可写」的办法 —— 权限、只读盘、盘满，
    # 都只有真去写一下才会暴露，isdir() 是看不出来的。
    probe = p / _PROBE
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        os.close(fd)
    except OSError as e:
        raise InvalidSetting(f"这个目录写不进去（{e.strerror or e}）：{s}") from e
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass

    return p


def looks_like_unc(p: Path) -> bool:
    """网络路径（\\\\server\\share）—— 能用，但很可能又慢又会掉线，值得提醒一句。"""
    s = str(p)
    return s.startswith("\\\\") or s.startswith("//")
