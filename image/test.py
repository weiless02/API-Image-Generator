import base64
import os

from openai import OpenAI

# key 从同目录的 env.py 读；那个文件不进仓库（见根目录 .gitignore）。
# 第一次用：把 env.example.py 复制成 env.py，填上你的 key。
try:
    from env import A6_API_KEY as api_key
except ImportError:
    api_key = None
api_key = api_key or os.environ.get("A6API_KEY")
if not api_key:
    raise SystemExit(
        "没有找到 API key。把 image/env.example.py 复制成 image/env.py，填上你的 key。"
    )

client = OpenAI(
    api_key=api_key,
    base_url="https://api.a6api.com/v1",  # ← 结尾一定要带 /v1
)

resp = client.images.generate(
    model="gpt-image-2.5",  # ← 模型名要按服务端文档写
    prompt="生成一个沿海高速的图片，可以看见大海，写实风格",
    size="1024x1024",
    n=1,
)

# gpt-image-1 默认返回 base64 图片数据，需要自己解码存成文件
img_bytes = base64.b64decode(resp.data[0].b64_json)
with open(".png", "wb") as f:
    f.write(img_bytes)

print("搞定")