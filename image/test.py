import base64
import os

from openai import OpenAI

# key 从环境变量读，源码里不写死（这个仓库要推到 GitHub）。
# 本地怎么设见 WEB/key.bat.example。
api_key = os.environ.get("A6API_KEY")
if not api_key:
    raise SystemExit(
        "没有读到环境变量 A6API_KEY。把 WEB/key.bat.example 复制成 WEB/key.bat，\n"
        "填上你的 key，然后用 WEB/启动.bat 启动。"
    )

client = OpenAI(
    api_key=api_key,
    base_url="https://api.a6api.com/v1",  # ← 结尾一定要带 /v1
)

resp = client.images.generate(
    model="gpt-image-2.5",  # ← 模型名要按中转站文档写
    prompt="生成一个沿海高速的图片，可以看见大海，写实风格",
    size="1024x1024",
    n=1,
)

# gpt-image-1 默认返回 base64 图片数据，需要自己解码存成文件
img_bytes = base64.b64decode(resp.data[0].b64_json)
with open(".png", "wb") as f:
    f.write(img_bytes)

print("搞定，图片存到 output.png")