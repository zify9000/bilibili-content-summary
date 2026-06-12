# 远程 ASR 转写（xiaomimimo / LiteLLM / llama.cpp）

## Endpoint: `/chat/completions` with `input_audio`

用 `/chat/completions` + `input_audio` 调用远程 ASR（`/audio/transcriptions` 不支持），支持 xiaomimimo、LiteLLM、llama.cpp 等兼容接口。

ASR 凭据从 `scripts/env/.llm.env` 加载（`asr_base_url` / `asr_api_key` / `asr_model`），通过 `common.py` 的 `load_llm_env()` + `get_asr_creds()` 读取。

## Provider 配置

`base.yaml` 的 `asr.provider` 控制 payload 格式：

| provider | 场景 | data 格式 | system prompt |
|----------|------|-----------|---------------|
| `xiaomimimo` | xiaomimimo proxy | `data:audio/mp3;base64,...` | 无（网关自动注入） |
| `standard` | LiteLLM / llama.cpp | 裸 base64 | 有 |

## 代码示例

```python
import requests, base64
from common import load_llm_env, get_asr_creds

load_llm_env()
base_url, api_key, model = get_asr_creds()

with open('/tmp/audio.mp3', 'rb') as f:
    raw_b64 = base64.b64encode(f.read()).decode()

# 根据 provider 构建 payload（实际由 summarize.py 的 _build_asr_payload 处理）
# xiaomimimo: data URL + 无 system/text
# standard:   裸 base64 + system message + text parts

url = f"{base_url}/chat/completions"
headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": "你是一个语音识别助手，请将语音内容转写为文字，保留所有细节。"},
        {"role": "user", "content": [
            {"type": "text", "text": "请转写这段语音的内容"},
            {"type": "input_audio", "input_audio": {"data": raw_b64, "format": "mp3"}}
        ]}
    ]
}

resp = requests.post(url, headers=headers, json=payload, timeout=180)
content = resp.json()['choices'][0]['message']['content']

# Strip the language tag prefix if present
if '<asr_text>' in content:
    content = content.split('<asr_text>', 1)[1]
```

## Chunking: 5min / 64kbps

分段参数从 `scripts/config/base.yaml` 的 `asr` 段读取：

```yaml
asr:
  chunk_duration: 300     # 分段时长（秒），5分钟一段
  chunk_bitrate: "64k"    # 分段比特率
```

> **注意**：5分钟/64kbps 单段约 2.4MB，需确保 ASR 服务 nginx 请求体限制 ≥ 3MB。

```bash
FFMPEG=$(python3 -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
$FFMPEG -i /tmp/audio.m4a -f segment -segment_time 300 -c:a libmp3lame -b:a 64k /tmp/chunk_%03d.mp3 -y
```

## Pre-flight probe

批量转写前先用一个 chunk 测试（超时从 `base.yaml` 的 `asr.probe_timeout` 读取）：

```python
resp = requests.post(url, headers=headers, json=payload, timeout=120)
if resp.status_code != 200:
    raise RuntimeError(f"ASR probe failed: {resp.status_code}")
```

## Parallel transcription

用 `ThreadPoolExecutor` 并行转写，并发数从 `base.yaml` 的 `asr.max_workers` 读取（默认3），避免压垮 ASR 服务。

## No external network

本机无外网，只能用远程 ASR，无法下载本地 whisper 模型。
