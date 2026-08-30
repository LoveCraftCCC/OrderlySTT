"""独立润色服务 (127.0.0.1:47640, 标准库实现, 无额外依赖).

后端可插拔, 由环境变量决定:
- POLISH_BACKEND=openai   (默认) 转发到任意 OpenAI 兼容端点
    POLISH_BASE_URL  e.g. https://api.xxx.com/v1  (公司内网大模型服务填内网地址)
    POLISH_MODEL     e.g. qwen2.5-1.5b-instruct / 内网模型名
    POLISH_API_KEY   可选
- POLISH_BACKEND=llamacpp  本机 CPU 推理 (pip install llama-cpp-python)
    POLISH_GGUF      模型路径, e.g. models/qwen2.5-0.5b-instruct-q4_k_m.gguf

启动: python polish/server.py   (随 Vernest sidecar 一起拉起或独立开机自启)
"""

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACKEND = os.environ.get("POLISH_BACKEND", "openai").lower()
BASE_URL = os.environ.get("POLISH_BASE_URL", "").rstrip("/")
MODEL = os.environ.get("POLISH_MODEL", "qwen2.5-1.5b-instruct")
API_KEY = os.environ.get("POLISH_API_KEY", "")
GGUF = os.environ.get("POLISH_GGUF", "")
PORT = int(os.environ.get("POLISH_PORT", "47640"))

SYSTEM_PROMPT = (
    "你是语音输入的润色助手。只做: 标点整理、口语词清理(嗯/呃/那个)、"
    "明显错别字纠正、专有名词按用户词表纠正。禁止: 增删内容、改写句式、"
    "翻译、回答或评论。只输出润色后的句子本身, 不加任何解释。"
)

_llm = None
_llm_lock = threading.Lock()


def _get_llamacpp():
    global _llm
    if _llm is None:
        with _llm_lock:
            if _llm is None:  # 双重检查, 防止并发首个请求把模型加载两遍
                from llama_cpp import Llama
                _llm = Llama(model_path=GGUF, n_ctx=1024,
                             n_threads=os.cpu_count() or 4, verbose=False)
    return _llm


def _polish_openai(text: str, budget_ms: int) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": text}],
        "temperature": 0.1, "max_tokens": max(64, len(text)),
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(f"{BASE_URL}/chat/completions",
                                 data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=max(1.0, budget_ms / 1000.0)) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def _polish_llamacpp(text: str, budget_ms: int) -> str:
    llm = _get_llamacpp()
    out = llm.create_chat_completion(
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": text}],
        temperature=0.1, max_tokens=max(64, len(text)))
    return out["choices"][0]["message"]["content"].strip()


def polish(text: str, budget_ms: int = 800) -> str:
    text = (text or "").strip()
    if not text:
        return text
    t0 = time.monotonic()
    try:
        if BACKEND == "llamacpp":
            out = _polish_llamacpp(text, budget_ms)
        else:
            out = _polish_openai(text, budget_ms)
        # 超预算丢弃, 客户端会用原文
        if time.monotonic() - t0 > budget_ms / 1000.0:
            return text
        return out or text
    except Exception:
        return text  # 静默降级


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/polish":
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self.send_response(400); self.end_headers(); return
        text = (req.get("text") or "").strip()
        budget = int(req.get("budget_ms") or 800)
        t0 = time.monotonic()
        out = polish(text, budget)
        body = json.dumps({"polished": out, "backend": BACKEND,
                           "latency_ms": int((time.monotonic() - t0) * 1000)},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 安静
        pass


if __name__ == "__main__":
    print(f"polish server :{PORT} backend={BACKEND} model={MODEL or GGUF}")
    if BACKEND == "llamacpp":
        # 预热: 模型加载耗时数十秒, 不能让首个请求扛 (预算 800ms 必超)
        print("预热: 加载 GGUF 模型...")
        try:
            _get_llamacpp()
            print("预热完成")
        except Exception as e:
            print(f"预热失败 (llamacpp): {e}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
