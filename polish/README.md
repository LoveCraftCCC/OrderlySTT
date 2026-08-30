# polish - Vernest 本地润色层

STT 不动，识别结果在粘贴前经过润色流水线；用户修正被静默采集，反哺个人词表。

## 架构

```
SenseVoice raw_text
   │
   ├─① 词表快路径 (本地 JSON, <1ms, 即学即用)
   │
   ├─② 小模型润色 (HTTP 127.0.0.1:47640, 预算超时→静默降级返回原文)
   │
   ├─③ 驻留浮窗 (跟随光标 1.5s, 可点击编辑, 无操作自动上屏)
   │
   └─④ 三元组落盘 (raw / polished / final, 字符级 diff, 全本地)
```

铁律：**润色层任何失败都不得阻塞输入**，全部静默降级为直接粘贴原文。

## 文件

- `client.py` —— 供 `voice_core/runtime.py` 调用的唯一入口 `polish_with_timeout()` / `dwell_overlay()`
- `server.py` —— 独立润色服务 (标准库实现, 无额外依赖)。后端可插拔:
  - `openai` 模式: 转发到任意 OpenAI 兼容端点 (公司内网大模型服务 / 外网 API 均可)
  - `llamacpp` 模式: 本地 CPU 推理 (llama-cpp-python + Qwen2.5-0.5B/1.5B int4, 无 GPU)
- `wordlist.py` —— 个人词表 (即时生效)
- `triplet.py` —— 三元组存储与字符级 diff 学习
- `overlay.py` —— 驻留浮窗 (Tk, WS_EX_NOACTIVATE 不抢焦点)

## 配置 (%APPDATA%\Vernest\config.json 的 "polish" 键)

```json
"polish": {
  "enabled": true,
  "endpoint": "http://127.0.0.1:47640",
  "budget_ms": 800,
  "dwell_ms": 1500,
  "dwell_enabled": true,
  "learn_enabled": true
}
```

## 公司内网部署

1. 只需拷贝整个仓库; `polish/server.py` 把 `backend` 指向内网 OpenAI 兼容端点 (改环境变量 `POLISH_BACKEND=openai POLISH_BASE_URL=... POLISH_MODEL=...`), 无外网依赖
2. 或 `POLISH_BACKEND=llamacpp` 纯本机 CPU (0.5B int4 约 200MB, 普通办公本 1~2s/句; 建议内网找更快的推理服务)
3. 三元组/词表数据全部落在本机 `%APPDATA%\Vernest\polish\`, 不出内网
4. 不使用键盘钩子/注入, 仅剪贴板+模拟Ctrl+V (与原版言栖相同的权限面)
