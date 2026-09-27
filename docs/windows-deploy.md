# Windows 部署指南（含 DeepSeek 润色层）

## 前置
- Windows 10/11 + PowerShell
- Node.js LTS、Rust (rustup)、Git、Python 3.10+
- DeepSeek API key（platform.deepseek.com → API Keys）

## 步骤

### 1. 克隆
```powershell
git clone https://github.com/LoveCraftCCC/OrderlySTT.git
cd OrderlySTT
```

### 2. 配置密钥（用户级环境变量，永久生效）
```powershell
[Environment]::SetEnvironmentVariable('POLISH_API_KEY', 'sk-你的key', 'User')
```
> key 只进本机环境变量，不进任何文件/仓库。

### 3. 下载 SenseVoice 模型（~120MB，自动走国内镜像）
```powershell
powershell -ExecutionPolicy Bypass -File download_model.ps1
```

### 4. 构建 GUI
```powershell
cd ui-tauri
npm install
npm run tauri build
```
产物在 `src-tauri/target/release/bundle/nsis/`。

### 5. 润色服务（可选开机自启）
```powershell
setx POLISH_BACKEND openai
setx POLISH_BASE_URL https://api.deepseek.com
setx POLISH_MODEL deepseek-v4-flash
python polish\server.py
```
开机自启：任务计划程序（Task Scheduler）建一个登录触发的基本任务即可。

### 6. 主程序配置启用润色
```json
{ "polish": { "enabled": true, "budget_ms": 1500 } }
```

## 备注
- deepseek-v4-flash 是混合推理模型：server.py 已按模型名自动加 `thinking: {"type": "disabled"}`（commit e405b55），否则返回空 content
- 实测润色延迟 625-805ms（Rome/代理链路），Windows 直连预计更快
- 铁律：润色任何失败/超时自动降级为原文，不阻塞输入
