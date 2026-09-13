# 语音输入文本润色 · 小模型选型报告

- **日期**：2026-08-30
- **场景**：Windows 桌面语音输入工具。SenseVoice 识别中文 → 小模型轻量润色（标点、口语词清理、错别字、专有名词纠正）
- **约束**：纯 CPU（办公笔记本），延迟预算 < 1 秒/句（≤30 字短句），可走 OpenAI 兼容端点（llama.cpp `llama-server`）或 GGUF 本地跑

## 0. 一个重要的前置事实

**SenseVoice 本身就输出标点和 ITN**（数字规整），`use_itn=True` 时自带标点预测与逆文本正则化（FunASR 论文与 sherpa-onnx 实现均确认）。因此润色模型的核心工作其实是：

1. **口语词清理**（"那个"、"就是"、"嗯"、"然后"开头语气词等）
2. **同音/近音错字纠正**（ASR 后处理纠错，本质是 CSC 任务）
3. **专有名词纠正**（人名、术语，需要上下文或词表）

标点可直接依赖 SenseVoice 输出，润色模型只做"保标点不改写"。这大幅降低了对模型"灵活性"的需求。

## 1. 通用小 LLM 候选（Qwen 系）

### 公开 CPU benchmark 数据

| 模型 | 量化 | CPU 吞吐（来源） | 30 字短句预估延迟 |
|---|---|---|---|
| Qwen2.5-0.5B-Instruct | Q8_0 | ~100 tok/s（llama.cpp discussion #19813，AVX 机器） | 30 字 ≈ 25–30 输出 token → **~0.3s** |
| Qwen2.5-0.5B-Instruct | Q4_K_M | iPhone CPU ~26 tok/s（CVPR edge 论文）；桌面 x86 更快，估 80–120 tok/s | **~0.3–0.4s** ✅ |
| Qwen2.5-1.5B-Instruct | Q4_K_M / Q8_0 | ~11–14 tok/s（手机 CPU）；桌面 x86 估 35–70 tok/s | 25 token → **0.4–0.7s** ⚠️ 压线 |
| Qwen2.5-3B-Instruct | Q4_K_M | 桌面 x86 估 15–30 tok/s | 25 token → **~1–1.7s** ❌ 超预算 |
| Qwen3-0.6B | Q8_0 | 手机 CPU 16 tok/s（掘金实测，Termux）；桌面 x86 估 60–100 tok/s | ~0.3–0.5s ✅ |
| Qwen3-1.7B | Q8_0 | 手机 CPU 8 tok/s；桌面 x86 估 25–45 tok/s | 25 token → **0.6–1s** ⚠️ |

注：桌面笔记本（现代 x86，AVX2/AVX512）通常比手机 ARM 快 3–5 倍；以上为保守估区间。短句 prompt（<500 token）TTFT 可忽略不计（<0.1s）。

### Qwen3 小尺寸确认

Qwen3（2025-04 发布，Apache 2.0）Dense 系列含 **0.6B 和 1.7B**，官方直接提供 GGUF（`Qwen/Qwen3-0.6B-GGUF`、`Qwen/Qwen3-1.7B-GGUF`）。注意 Qwen3 默认带 thinking 模式，**必须关掉**（`/no_think` 或 non-thinking 配置），否则会先输出思考过程，延迟爆炸。这一点对延迟敏感场景是坑，Qwen2.5 反而省心。

## 2. 中文纠错/润色专用模型

| 模型 | 参数量 | 体积 | CPU 延迟（30 字） | 许可证 | 地址 |
|---|---|---|---|---|---|
| **shibing624/macbert4csc-base-chinese** | 102M (BERT-base) | fp32 ~400MB / ONNX int8 ~110MB | **<100ms**（单次 forward，pycorrector 实测 QPS 224） | Apache 2.0 | huggingface.co/shibing624/macbert4csc-base-chinese |
| Macropodus/macbert4csc_v2 | 102M | ~400MB | <100ms | Apache 2.0 | huggingface.co/Macropodus/macbert4csc_v2 |
| shibing624/mengzi-t5-base-chinese-correction | ~220M | ~800MB | ~200–400ms（生成式 T5） | Apache 2.0 | huggingface.co/shibing624/mengzi-t5-base-chinese-correction |
| Soft-Masked BERT（原论文复现） | 102M | ~400MB | <100ms | 学术/开源复现 | 主要在 pycorrector 生态 |
| 百度 MiduCTC（输入法纠错，BERT-base+CTC解码） | 110M | ~400MB | <100ms | 开源（PaddleNLP 生态） | github.com/awesome-cli/MiduCTC（Paddle 复现） |
| **shibing624/chinese-text-correction-1.5b** ⭐ | 1.5B（Qwen2.5-1.5B-Instruct 纠错微调，支持错字+多字/少字+词序+语法） | Q4_K_M ~0.9GB / Q8_0 ~1.7GB | 桌面 CPU 约 0.5–0.8s（同 Qwen2.5-1.5B 量级） | Apache 2.0 | huggingface.co/shibing624/chinese-text-correction-1.5b；GGUF: huggingface.co/QuantFactory/chinese-text-correction-1.5b-GGUF |
| shibing624/chinese-kenlm-klm | 统计语言模型 | 小 | 极快但效果差（SIGHAN F1 仅 0.31） | Apache 2.0 | pycorrector 生态 |

**关键发现**：`chinese-text-correction-1.5b` 是 pycorrector 作者基于 Qwen2.5-1.5B 在纠错数据上微调的专用模型，评测中 MCSC（医学拼写）F1 达 0.95、SIGHAN-2015 句级表现远超 BERT 类模型，且因为任务收敛（输出≈输入长度），不会幻觉改写。它就是"通用小 LLM"和"专用纠错模型"之间的最佳平衡点，且有现成 GGUF。

## 3. 权衡分析

| 维度 | 通用小 LLM（Qwen2.5-0.5B/3B） | 专用纠错模型（MacBERT4CSC 等） | 纠错微调 LLM（chinese-text-correction-1.5b） |
|---|---|---|---|
| 口语词清理 | ✅ 能做，但小模型爱幻觉改写、啰嗦 | ❌ 完全不管 | ⚠️ 部分能力（偏纠错） |
| 错别字/同音字 | ⚠️ 0.5B 纠错能力弱，会漏改或乱改 | ✅ 强（SIGHAN 句级 F1 ~0.78） | ✅ 强 |
| 标点 | ⚠️ 不可控 | ❌ | ⚠️ |
| 专有名词 | ❌ 小模型没这知识，需外挂词表 | ❌（可注入混淆词典） | ❌（同左） |
| CPU 延迟 | 0.3B：快；1.5B+：压线/超 | ✅ <100ms，碾压 | ⚠️ 0.5–0.8s，压线但可行 |
| 幻觉/改写风险 | 高（最致命：把用户原话改意） | 无 | 低（训练目标就是保守纠错） |

核心矛盾：语音润色需要"删除口语词"（生成式能力）+ "纠错"（判别式精度），单一模型都补不齐。**口语词清理其实不需要 LLM**——正则 + 位置规则（句首语气词、"那个/就是"填充词）能解决 80%，且零延迟。专有名词同理，热词表放在 SenseVoice 的 hotword/后处理替换即可。

## 4. 推荐

### ✅ 首选方案：规则层 + chinese-text-correction-1.5b（GGUF Q4_K_M）

```
SenseVoice(use_itn=True, 带标点)
  → ① 正则规则清理口语词/语气词（<1ms，可配置词表）
  → ② 专有名词热词替换表（<1ms，用户可维护）
  → ③ chinese-text-correction-1.5b GGUF Q4_K_M，llama.cpp llama-server
      （OpenAI 兼容端点，prompt 限定"只纠错不改写"，温度 0）
```

- 理由：纠错质量接近专用 BERT 类上限且覆盖多字/少字/词序错误；Apache 2.0；Q4_K_M 约 0.9GB，办公笔记本内存无压力；输出≈输入长度（~30 token），桌面 CPU 0.5–0.8s，压线达标。规则层先减掉口语词后，送模型的句子更短更干净，纠错更准。
- 风险与对策：若实测超 1s，降到 IQ4_XS 量化或换备选；llama-server 常驻进程避免冷启动。

### 🥈 备选 A（追求极致延迟）：规则层 + macbert4csc-base-chinese（ONNX int8）

- 102M、~110MB、**<100ms**，延迟预算用不到 1/10。SIGHAN 句级 F1 0.78，错字纠错够用。
- 代价：不管口语词外的语法问题、不管多字少字；但这两项恰好被规则层和 SenseVoice 覆盖大半。**如果实测首选方案超延迟，直接切这个，组合延迟 <150ms。**

### 🥉 备选 B（最简实现）：Qwen2.5-0.5B-Instruct Q4_K_M 单模型

- 0.4GB、~0.3s、一个 llama-server 搞定全部（口语词+纠错+标点兜底），实现最简单。
- 代价：0.5B 纠错和指令遵循能力弱，偶发幻觉改写/复读；建议温度 0 + few-shot 示例 + 输出长度校验（超输入 1.3 倍则回退原文）。

### ❌ 不推荐

- Qwen2.5-3B / Qwen3-4B+：纯 CPU 超 1s。
- Qwen3-0.6B/1.7B：能力不错但默认 thinking 模式是延迟陷阱，且 0.6B 中文纠错无微调加成，不如专用微调模型；除非后续出现 Qwen3-0.6B 的纠错微调版再评估。
- kenlm：纠错质量太差。
- Mengzi-T5：能力不突出，还要引入 T5 推理栈。

## 5. 候选速查表

| 模型 | 参数量 | 量化体积 | 预估 CPU 延迟（30字） | 许可证 | GGUF/模型地址 |
|---|---|---|---|---|---|
| shibing624/chinese-text-correction-1.5b ⭐ | 1.5B | Q4_K_M ~0.9GB | 0.5–0.8s | Apache 2.0 | https://huggingface.co/QuantFactory/chinese-text-correction-1.5b-GGUF |
| shibing624/macbert4csc-base-chinese | 102M | int8 ~110MB | <0.1s | Apache 2.0 | https://huggingface.co/shibing624/macbert4csc-base-chinese |
| Macropodus/macbert4csc_v2 | 102M | ~400MB (fp32) | <0.1s | Apache 2.0 | https://huggingface.co/Macropodus/macbert4csc_v2 |
| shibing624/mengzi-t5-base-chinese-correction | 220M | ~800MB | 0.2–0.4s | Apache 2.0 | https://huggingface.co/shibing624/mengzi-t5-base-chinese-correction |
| Qwen2.5-0.5B-Instruct | 0.5B | Q4_K_M ~0.4GB | 0.3–0.4s | Apache 2.0 | https://huggingface.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF |
| Qwen2.5-1.5B-Instruct | 1.5B | Q4_K_M ~1.1GB | 0.4–0.7s | Apache 2.0 | https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF |
| Qwen2.5-3B-Instruct | 3B | Q4_K_M ~1.9GB | 1–1.7s ❌ | Qwen 许可（可商用） | https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF |
| Qwen3-0.6B | 0.6B | Q8_0 ~0.6GB | 0.3–0.5s | Apache 2.0 | https://huggingface.co/Qwen/Qwen3-0.6B-GGUF |
| Qwen3-1.7B | 1.7B | Q8_0 ~1.7GB | 0.6–1s | Apache 2.0 | https://huggingface.co/Qwen/Qwen3-1.7B-GGUF |

## 6. 落地建议

1. 先用 pycorrector 仓库（github.com/shibing624/pycorrector）在目标笔记本上实测 chinese-text-correction-1.5b 与 macbert4csc 的真实延迟，30 字短句各跑 50 条取 P95。
2. 规则层（口语词、热词）独立成配置文件，用户可编辑自己的专有名词表。
3. llama-server 以 `--threads = 物理核数-2` 常驻，避免与 SenseVoice 抢 CPU。
4. 加输出校验兜底：润色结果与输入编辑距离过大（如 >40%）或含人名数字被改动时，回退原文——语音输入场景"宁可不改，不可改错"。
