"""B站视频/图文摘要主流程：解析链接 → 获取内容 → 转写/OCR → LLM 摘要"""
import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    SCRIPT_DIR, TMP_DIR, SUMMARY_RESULT_PATH,
    setup_logging, load_base_config,
    load_bili_env, load_asr_env, load_llm_env,
    get_bili_cookies, get_asr_creds, get_llm_creds, get_ocr_creds,
    validate_creds, retry,
)

logger = setup_logging("summarize")

_QUIET = False  # 静默模式，不输出 LLM 流式内容到 stderr


# ═══════════════════════════════════════════════════════════════
# ── Step 1: URL 解析（视频 BV ID / 图文 opus ID）──
# ═══════════════════════════════════════════════════════════════

def resolve_bv_id(url: str) -> str:
    """从 URL 中提取 BV ID，支持短链接 b23.tv 自动跟随重定向"""
    m = re.search(r"(BV[\w]+)", url)
    if m:
        return m.group(1)

    if "b23.tv" in url:
        resp = requests.head(url, allow_redirects=True, timeout=15)
        m = re.search(r"(BV[\w]+)", resp.url)
        if m:
            return m.group(1)

    raise ValueError(f"无法从 URL 提取 BV ID: {url}")


def resolve_opus_id(url: str) -> str:
    """从 URL 中提取 opus ID（纯数字）"""
    m = re.search(r"/opus/(\d+)", url)
    if m:
        return m.group(1)
    # 也支持直接传入纯数字 ID
    if url.strip().isdigit():
        return url.strip()
    raise ValueError(f"无法从 URL 提取 opus ID: {url}")


# ═══════════════════════════════════════════════════════════════
# ── 视频流程 ──
# ═══════════════════════════════════════════════════════════════

# ── B站 API 公共 ──

def _safe_bili_json(resp: requests.Response, endpoint: str) -> dict:
    """安全解析 B站 API 响应，检测 Cookie 过期导致返回 HTML 登录页面"""
    if resp.status_code == 412:
        raise RuntimeError(
            "B站 Cookie 已过期或缺失（HTTP 412）。"
            "请重新登录：python3 scripts/init/bili_get_qr.py → 扫码 → python3 scripts/init/bili_wait_login.py"
        )
    try:
        return resp.json()
    except json.JSONDecodeError:
        raw = resp.text[:200].lower()
        if "<html" in raw or "<!doctype" in raw:
            raise RuntimeError(
                "B站 Cookie 已过期（API 返回登录页面）。"
                "请重新登录：python3 scripts/init/bili_get_qr.py → 扫码 → python3 scripts/init/bili_wait_login.py"
            )
        raise RuntimeError(f"B站 API 返回非 JSON 响应 ({endpoint}): {resp.text[:100]}")


# ── Step 2: 获取视频元数据 ──

def get_video_metadata(bv_id: str, config: dict) -> dict:
    """获取视频元数据：title, cid, duration"""
    user_agent = config.get("bili", {}).get("user_agent", "")
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://www.bilibili.com",
    }

    cookies = get_bili_cookies()
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["Cookie"] = cookie_str

    resp = requests.get(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}",
        headers=headers, timeout=15,
    )
    data = _safe_bili_json(resp, "view")
    if data.get("code") != 0:
        raise RuntimeError(f"获取视频信息失败: code={data.get('code')} msg={data.get('message')}")

    info = data["data"]
    return {
        "bv_id": bv_id,
        "title": info["title"],
        "cid": info["cid"],
        "duration": info["duration"],
        "desc": info.get("desc", ""),
        "owner": info.get("owner", {}).get("name", ""),
    }


# ── Step 3: 下载音频 ──

def download_audio(bv_id: str, cid: str, config: dict) -> Path:
    """通过 B站 API 下载音频，返回音频文件路径。带重试机制应对 CDN 超时。"""
    user_agent = config.get("bili", {}).get("user_agent", "")
    headers = {
        "User-Agent": user_agent,
        "Referer": f"https://www.bilibili.com/video/{bv_id}",
        "Origin": "https://www.bilibili.com",
    }

    cookies = get_bili_cookies()
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["Cookie"] = cookie_str

    # step 1: 获取音频 CDN 地址（含重试）
    for attempt in range(3):
        try:
            resp = requests.get(
                f"https://api.bilibili.com/x/player/playurl?bvid={bv_id}&cid={cid}&fnval=16&fnver=0&fourk=0",
                headers=headers, timeout=30,
            )
            play_data = _safe_bili_json(resp, "playurl")
            if play_data.get("code") != 0:
                raise RuntimeError(f"获取播放地址失败: code={play_data.get('code')} msg={play_data.get('message')}")
            audio_url = play_data["data"]["dash"]["audio"][0]["baseUrl"]
            break
        except (requests.ConnectionError, requests.ReadTimeout) as e:
            if attempt == 2:
                raise RuntimeError(f"获取播放地址重试3次仍失败: {e}")
            logger.warning(f"获取播放地址失败 (attempt {attempt + 1}/3): {e}，重试中...")
            import time
            time.sleep(2 ** attempt)

    # step 2: 流式下载音频（含重试，CDN URL 可能不稳定）
    audio_path = Path(config.get("temp_dir", "/tmp")) / "bilibili_audio.m4a"
    for attempt in range(3):
        try:
            audio_resp = requests.get(
                audio_url,
                headers={**headers, "Referer": f"https://www.bilibili.com/video/{bv_id}"},
                stream=True,
                timeout=(30, 300),  # (connect, read) — B站 CDN 建立连接慢，放宽到 30s
            )
            with open(audio_path, "wb") as f:
                for chunk in audio_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"音频已下载: {audio_path} ({audio_path.stat().st_size / 1024 / 1024:.1f}MB)")
            return audio_path
        except (requests.ConnectionError, requests.ReadTimeout) as e:
            if attempt == 2:
                raise RuntimeError(f"下载音频重试3次仍失败: {e}")
            logger.warning(f"下载音频失败 (attempt {attempt + 1}/3): {e}，重新获取 CDN 地址...")
            import time
            time.sleep(2 ** attempt)
            # CDN 地址可能过期，重新获取
            resp = requests.get(
                f"https://api.bilibili.com/x/player/playurl?bvid={bv_id}&cid={cid}&fnval=16&fnver=0&fourk=0",
                headers=headers, timeout=30,
            )
            play_data = _safe_bili_json(resp, "playurl")
            audio_url = play_data["data"]["dash"]["audio"][0]["baseUrl"]

    raise RuntimeError("下载音频失败（不可达）")


# ── Step 4: 音频分段 ──

def split_audio(audio_path: Path, config: dict) -> list:
    """将音频分段，返回分段文件路径列表"""
    asr_cfg = config.get("asr", {})
    chunk_duration = asr_cfg.get("chunk_duration", 300)
    chunk_bitrate = asr_cfg.get("chunk_bitrate", "64k")
    temp_dir = config.get("temp_dir", "/tmp")

    ffmpeg_path = _find_ffmpeg()

    chunk_pattern = str(Path(temp_dir) / "bilibili_chunk_%03d.mp3")
    cmd = [
        ffmpeg_path, "-i", str(audio_path),
        "-f", "segment", "-segment_time", str(chunk_duration),
        "-c:a", "libmp3lame", "-b:a", chunk_bitrate,
        chunk_pattern, "-y",
    ]
    subprocess.run(cmd, capture_output=True, check=True)

    chunks = sorted(glob.glob(str(Path(temp_dir) / "bilibili_chunk_*.mp3")))
    if not chunks:
        raise RuntimeError("音频分段失败：未生成任何分段文件")

    logger.info(f"音频分段完成: {len(chunks)} 段 ({chunk_duration}s / {chunk_bitrate})")
    return chunks


def _find_ffmpeg() -> str:
    """查找 ffmpeg 可执行文件"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        pass
    result = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    raise RuntimeError("未找到 ffmpeg，请安装: pip install imageio-ffmpeg 或 apt install ffmpeg")


# ── ASR payload 构建 ──

def _build_asr_payload(audio_bytes: bytes, model: str, provider: str) -> dict:
    """根据 provider 构建 ASR 请求 payload

    - xiaomimimo: data URL 格式 + 无 system/text（proxy 自动注入提示词）
    - standard:   裸 base64 + system message + text parts（LiteLLM/llama.cpp 标准格式）
    """
    raw_b64 = base64.b64encode(audio_bytes).decode()

    if provider == "xiaomimimo":
        audio_data = f"data:audio/mp3;base64,{raw_b64}"
        return {
            "model": model,
            "messages": [
                {"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": audio_data, "format": "mp3"}},
                ]},
            ],
        }
    else:
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个语音识别助手，请将语音内容转写为文字，保留所有细节。"},
                {"role": "user", "content": [
                    {"type": "text", "text": "请转写这段语音的内容"},
                    {"type": "input_audio", "input_audio": {"data": raw_b64, "format": "mp3"}},
                ]},
            ],
        }


# ── Step 5: 探针 ──

def probe_asr(chunks: list, base_url: str, api_key: str, model: str, config: dict) -> bool:
    """用第一个 chunk 测试 ASR 服务可用性"""
    asr_cfg = config.get("asr", {})
    probe_timeout = asr_cfg.get("probe_timeout", 120)
    provider = asr_cfg.get("provider", "standard")

    with open(chunks[0], "rb") as f:
        audio_bytes = f.read()

    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = _build_asr_payload(audio_bytes, model, provider)

    resp = requests.post(url, headers=headers, json=payload, timeout=probe_timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"ASR 探针失败: HTTP {resp.status_code} {resp.text[:200]}")

    logger.info("ASR 探针通过")
    return True


# ── Step 6: 并行转写 ──

def transcribe_chunks(chunks: list, base_url: str, api_key: str, model: str, config: dict) -> str:
    """并行转写所有分段，返回带时间戳的完整转写文本"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    asr_cfg = config.get("asr", {})
    max_workers = asr_cfg.get("max_workers", 3)
    transcribe_timeout = asr_cfg.get("transcribe_timeout", 360)
    provider = asr_cfg.get("provider", "standard")
    chunk_duration = asr_cfg.get("chunk_duration", 300)

    def _format_ts(seconds: int) -> str:
        h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def transcribe_one(chunk_path: str, index: int) -> tuple:
        with open(chunk_path, "rb") as f:
            audio_bytes = f.read()

        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = _build_asr_payload(audio_bytes, model, provider)

        resp = requests.post(url, headers=headers, json=payload, timeout=transcribe_timeout)
        if resp.status_code != 200:
            return (index, f"[ERROR: chunk {index} failed with {resp.status_code}]")

        content = resp.json()["choices"][0]["message"]["content"]
        if "<asr_text>" in content:
            content = content.split("<asr_text>", 1)[1]
        # 添加时间戳前缀（显示该分段的时间区间）
        seg_start = index * chunk_duration
        seg_end = seg_start + chunk_duration
        start_ts = _format_ts(seg_start)
        end_ts = _format_ts(seg_end)
        return (index, f"[{start_ts}-{end_ts}]\n{content}")

    results = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(transcribe_one, chunk, i): i for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            index, text = future.result()
            results[index] = text
            logger.info(f"  分段 {index + 1}/{len(chunks)} 完成")

    full_transcript = "\n\n".join(r for r in results if r is not None)
    logger.info(f"转写完成: {len(full_transcript)} 字")
    return full_transcript


# ── Step 7: LLM 摘要（视频）──

def summarize_transcript(transcript: str, video_meta: dict, config: dict) -> str:
    """用 LLM 生成视频摘要"""
    llm_model, llm_base_url, llm_api_key = get_llm_creds()
    issues = validate_creds(
        ("llm_model", llm_model), ("llm_base_url", llm_base_url), ("llm_api_key", llm_api_key)
    )
    if issues:
        raise RuntimeError(f"LLM 凭据不完整: {', '.join(issues)}")

    llm_cfg = config.get("llm", {})
    title = video_meta.get("title", "")
    owner = video_meta.get("owner", "")
    duration = video_meta.get("duration", 0)

    max_chunk_chars = 50000
    if len(transcript) > max_chunk_chars:
        logger.info(f"转写文本过长 ({len(transcript)} 字)，分段摘要后合并")
        chunk_summaries = []
        for i in range(0, len(transcript), max_chunk_chars):
            chunk_text = transcript[i:i + max_chunk_chars]
            summary = _call_llm_summary(llm_model, llm_base_url, llm_api_key, llm_cfg,
                                        chunk_text, title, owner, duration)
            chunk_summaries.append(summary)
        merged = "\n\n".join(chunk_summaries)
        return _call_llm_summary(llm_model, llm_base_url, llm_api_key, llm_cfg,
                                 merged, title, owner, duration, is_merge=True)
    else:
        return _call_llm_summary(llm_model, llm_base_url, llm_api_key, llm_cfg,
                                 transcript, title, owner, duration)


# ═══════════════════════════════════════════════════════════════
# ── 图文流程 ──
# ═══════════════════════════════════════════════════════════════

def fetch_opus_detail(opus_id: str, config: dict) -> dict:
    """通过 B站 API 获取图文详情，返回 meta + paragraphs"""
    user_agent = config.get("bili", {}).get("user_agent", "")
    headers = {
        "User-Agent": user_agent,
        "Referer": f"https://www.bilibili.com/opus/{opus_id}",
    }
    cookies = get_bili_cookies()
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["Cookie"] = cookie_str

    resp = requests.get(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail",
        params={"id": opus_id},
        headers=headers,
        timeout=15,
    )
    data = _safe_bili_json(resp, "opus/detail")
    if data.get("code") != 0:
        raise RuntimeError(f"获取图文详情失败: code={data.get('code')} msg={data.get('message')}")

    item = data["data"]["item"]
    modules = item.get("modules", [])
    basic = item.get("basic", {})

    author_name = ""
    pub_time = ""
    title = ""
    paragraphs = []

    for m in modules:
        mtype = m.get("module_type", "")
        if mtype == "MODULE_TYPE_TITLE":
            title = m.get("module_title", {}).get("text", "")
        elif mtype == "MODULE_TYPE_AUTHOR":
            author = m.get("module_author", {})
            author_name = author.get("name", "")
            pub_time = author.get("pub_time", "")
        elif mtype == "MODULE_TYPE_CONTENT":
            paragraphs = m.get("module_content", {}).get("paragraphs", [])

    if not title:
        title = basic.get("title", "")

    return {
        "opus_id": opus_id,
        "title": title,
        "author": author_name,
        "pub_time": pub_time,
        "paragraphs": paragraphs,
    }


def extract_opus_content(paragraphs: list) -> tuple:
    """从 paragraphs 中提取文本和图片 URL 列表
    返回 (full_text: str, image_urls: list[str])
    """
    text_parts = []
    image_urls = []

    for p in paragraphs:
        ptype = p.get("para_type")
        if ptype == 1:
            nodes = p.get("text", {}).get("nodes", [])
            for n in nodes:
                word = n.get("word", {})
                words = word.get("words", "")
                if words:
                    text_parts.append(words)
        elif ptype == 2:
            pics = p.get("pic", {}).get("pics", [])
            for pic in pics:
                url = pic.get("url", "")
                if url:
                    image_urls.append(url)

    full_text = "\n".join(text_parts)
    return full_text, image_urls


def _resize_image_if_needed(img_bytes: bytes, max_size: int) -> bytes:
    """若图片长边超过 max_size，等比缩放后返回 JPEG bytes"""
    from PIL import Image

    img = Image.open(BytesIO(img_bytes))
    w, h = img.size
    if max(w, h) <= max_size:
        return img_bytes

    ratio = max_size / max(w, h)
    new_size = (int(w * ratio), int(h * ratio))
    img = img.resize(new_size, Image.LANCZOS)

    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def ocr_images(image_urls: list, config: dict) -> list:
    """并行 OCR 所有图片，返回 [{"index": int, "text": str}] 列表"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ocr_cfg = config.get("ocr", {})
    max_workers = ocr_cfg.get("max_workers", 3)
    ocr_timeout = ocr_cfg.get("timeout", 120)
    image_max_size = ocr_cfg.get("image_max_size", 4096)

    ocr_model, ocr_base_url, ocr_api_key = get_ocr_creds()
    issues = validate_creds(
        ("ocr_model", ocr_model),
        ("ocr_base_url (from asr_base_url)", ocr_base_url),
        ("ocr_api_key (from asr_api_key)", ocr_api_key),
    )
    if issues:
        raise RuntimeError(f"OCR 凭据不完整: {', '.join(issues)}")

    headers = {
        "Authorization": f"Bearer {ocr_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{ocr_base_url}/chat/completions"

    def ocr_one(img_url: str, index: int) -> dict:
        try:
            img_resp = requests.get(img_url, timeout=60, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.bilibili.com",
            })
            img_resp.raise_for_status()
            img_bytes = img_resp.content

            img_bytes = _resize_image_if_needed(img_bytes, image_max_size)
            raw_b64 = base64.b64encode(img_bytes).decode()

            payload = {
                "model": ocr_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请识别并转录图片中的所有文字内容，保留原有排版结构。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{raw_b64}"}},
                    ],
                }],
                "max_tokens": 4096,
            }

            resp = requests.post(url, headers=headers, json=payload, timeout=ocr_timeout)
            if resp.status_code != 200:
                return {"index": index, "text": f"[ERROR: 图片 {index + 1} OCR 失败 HTTP {resp.status_code}]"}

            content = resp.json()["choices"][0]["message"]["content"]
            return {"index": index, "text": content}
        except Exception as e:
            return {"index": index, "text": f"[ERROR: 图片 {index + 1} OCR 异常: {e}]"}

    results = [None] * len(image_urls)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(ocr_one, img_url, i): i for i, img_url in enumerate(image_urls)}
        for future in as_completed(futures):
            result = future.result()
            results[result["index"]] = result
            logger.info(f"  OCR {result['index'] + 1}/{len(image_urls)} 完成")

    return results


def summarize_opus(full_text: str, ocr_results: list, meta: dict, config: dict) -> str:
    """用 LLM 生成图文摘要"""
    llm_model, llm_base_url, llm_api_key = get_llm_creds()
    issues = validate_creds(
        ("llm_model", llm_model), ("llm_base_url", llm_base_url), ("llm_api_key", llm_api_key)
    )
    if issues:
        raise RuntimeError(f"LLM 凭据不完整: {', '.join(issues)}")

    llm_cfg = config.get("llm", {})
    title = meta.get("title", "")
    author = meta.get("author", "")

    ocr_text_parts = []
    for r in ocr_results:
        if r:
            idx = r["index"] + 1
            text = r["text"].strip()
            if text and not text.startswith("[ERROR"):
                ocr_text_parts.append(f"[图片 {idx}]\n{text}")
    ocr_full = "\n\n".join(ocr_text_parts)

    parts = []
    if full_text.strip():
        parts.append(f"图文正文：\n{full_text}")
    if ocr_full.strip():
        parts.append(f"图片内文字（OCR 识别）：\n{ocr_full}")

    combined = "\n\n".join(parts) if parts else "（无内容）"

    prompt = (
        f"以下是B站图文动态「{title}」(作者: {author}) 的内容，请生成：\n"
        f"1. 核心摘要（200字以内）\n"
        f"2. 关键要点（3-5条）\n"
        f"3. 内容结构梳理（如有明显分段）\n\n"
        f"{combined}"
    )

    max_chunk_chars = 50000
    if len(prompt) > max_chunk_chars:
        logger.info(f"图文内容过长 ({len(prompt)} 字)，分段摘要后合并")
        chunk_summaries = []
        for i in range(0, len(prompt), max_chunk_chars):
            chunk_text = prompt[i:i + max_chunk_chars]
            chunk_summaries.append(_call_llm_raw(llm_model, llm_base_url, llm_api_key, llm_cfg, chunk_text))
        merged = "\n\n".join(chunk_summaries)
        merge_prompt = f"以下是图文动态「{title}」的分段摘要，请合并为一份完整、连贯的摘要：\n\n{merged}"
        return _call_llm_raw(llm_model, llm_base_url, llm_api_key, llm_cfg, merge_prompt)
    else:
        return _call_llm_raw(llm_model, llm_base_url, llm_api_key, llm_cfg, prompt)


# ═══════════════════════════════════════════════════════════════
# ── 通用 LLM 调用 ──
# ═══════════════════════════════════════════════════════════════

def _call_llm_raw(model: str, base_url: str, api_key: str, llm_cfg: dict, prompt: str) -> str:
    """通用 LLM 调用（streaming），返回完整响应文本。quiet 模式下不输出流式内容。"""
    import openai

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=llm_cfg.get("temperature", 0.3),
        max_tokens=llm_cfg.get("max_tokens", 131072),
        timeout=llm_cfg.get("timeout", 300),
        stream=True,
    )
    content = ""
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            content += chunk.choices[0].delta.content
            if not _QUIET:
                print(chunk.choices[0].delta.content, end="", flush=True, file=sys.stderr)
    if not _QUIET:
        print(file=sys.stderr)
    return content.strip()


def _call_llm_summary(model: str, base_url: str, api_key: str, llm_cfg: dict,
                      transcript: str, title: str, owner: str, duration: int,
                      is_merge: bool = False) -> str:
    """调用 LLM 生成摘要（视频场景）"""
    if is_merge:
        prompt = (
            f"以下是视频「{title}」(UP主: {owner}, 时长: {duration}秒) 的分段摘要，请合并为一份完整、连贯的摘要：\n\n"
            f"{transcript}"
        )
    else:
        prompt = (
            f"以下是视频「{title}」(UP主: {owner}, 时长: {duration}秒) 的完整转写文本，请生成：\n"
            f"1. 核心摘要（200字以内）\n"
            f"2. 关键要点（3-5条）\n"
            f"3. 章节时间线 —— 文本中 [MM:SS-MM:SS] 为各段音频的时间区间（每段约 5 分钟），仅作定位参考。请**根据内容的实际话题转折**划分 4-8 个章节。\n"
            f"   时间估算法：观察话题转折在段落文本中的位置 —— 若转折出现在段落开头 → 时间接近区间起点；出现在段落中间 → 取区间中点；出现在段落末尾 → 接近区间终点。据此估算出非整点的时间，格式如「03:20-12:45 主题描述」。\n"
            f"   **禁止**：直接把 [MM:SS-MM:SS] 区间当作章节时间。\n\n"
            f"转写文本：\n{transcript}"
        )
    return _call_llm_raw(model, base_url, api_key, llm_cfg, prompt)


# ═══════════════════════════════════════════════════════════════
# ── 清理临时文件 ──
# ═══════════════════════════════════════════════════════════════

def cleanup_temp_files(config: dict):
    """清理分段音频文件"""
    temp_dir = config.get("temp_dir", "/tmp")
    for f in glob.glob(str(Path(temp_dir) / "bilibili_chunk_*.mp3")):
        try:
            Path(f).unlink()
        except OSError:
            pass
    audio_path = Path(temp_dir) / "bilibili_audio.m4a"
    if audio_path.exists():
        try:
            audio_path.unlink()
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════
# ── 主流程 ──
# ═══════════════════════════════════════════════════════════════

def _main_video(args, config):
    """视频摘要流程"""
    global _QUIET
    _QUIET = args.quiet and not args.no_push  # --no-push 时用户需要看到摘要内容
    bv_id = resolve_bv_id(args.url)
    logger.info(f"BV ID: {bv_id}")

    meta = get_video_metadata(bv_id, config)
    logger.info(f"视频: {meta['title']} (UP主: {meta['owner']}, 时长: {meta['duration']}s)")

    audio_path = download_audio(bv_id, meta["cid"], config)
    chunks = split_audio(audio_path, config)

    asr_base_url, asr_api_key, asr_model = get_asr_creds()
    issues = validate_creds(
        ("asr_base_url", asr_base_url), ("asr_api_key", asr_api_key), ("asr_model", asr_model)
    )
    if issues:
        logger.error(f"ASR 凭据不完整: {', '.join(issues)}")
        sys.exit(1)

    probe_asr(chunks, asr_base_url, asr_api_key, asr_model, config)
    transcript = transcribe_chunks(chunks, asr_base_url, asr_api_key, asr_model, config)
    summary = summarize_transcript(transcript, meta, config)

    result = {
        "type": "video",
        "bv_id": bv_id,
        "title": meta["title"],
        "owner": meta["owner"],
        "duration": meta["duration"],
        "url": f"https://www.bilibili.com/video/{bv_id}",
        "transcript_length": len(transcript),
        "transcript": transcript,
        "summary": summary,
    }
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"摘要结果已保存: {SUMMARY_RESULT_PATH}")

    if not args.no_cleanup:
        cleanup_temp_files(config)

    status = {
        "status": "done",
        "type": "video",
        "title": meta["title"],
        "owner": meta["owner"],
        "duration": meta["duration"],
        "transcript_length": len(transcript),
        "next": "python3 scripts/push.py",
    }
    print(json.dumps(status, ensure_ascii=False))


def _main_opus(args, config):
    """图文摘要流程"""
    global _QUIET
    _QUIET = args.quiet and not args.no_push  # --no-push 时用户需要看到摘要内容
    opus_id = resolve_opus_id(args.url)
    logger.info(f"Opus ID: {opus_id}")

    detail = fetch_opus_detail(opus_id, config)
    logger.info(f"图文: {detail['title']} (作者: {detail['author']})")

    full_text, image_urls = extract_opus_content(detail["paragraphs"])
    logger.info(f"文本: {len(full_text)} 字, 图片: {len(image_urls)} 张")

    ocr_results = []
    if image_urls:
        if len(image_urls) > 20:
            logger.warning(f"图片数量 ({len(image_urls)}) 超过上限 20，仅处理前 20 张")
            image_urls = image_urls[:20]
        ocr_results = ocr_images(image_urls, config)
    else:
        logger.info("无图片，跳过 OCR")

    meta = {
        "title": detail["title"],
        "author": detail["author"],
    }
    summary = summarize_opus(full_text, ocr_results, meta, config)

    # 拼接原文：正文 + 各图片 OCR 文本
    original_parts = []
    if full_text.strip():
        original_parts.append(full_text.strip())
    for r in ocr_results:
        if r and r["text"].strip() and not r["text"].startswith("[ERROR"):
            original_parts.append(f"\n--- 图片 {r['index'] + 1} ---\n{r['text'].strip()}")
    original_text = "\n".join(original_parts)

    result = {
        "type": "opus",
        "opus_id": opus_id,
        "title": detail["title"],
        "author": detail["author"],
        "url": f"https://www.bilibili.com/opus/{opus_id}",
        "text_length": len(full_text),
        "image_count": len(image_urls),
        "summary": summary,
        "original_text": original_text,
    }
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"摘要结果已保存: {SUMMARY_RESULT_PATH}")

    status = {
        "status": "done",
        "type": "opus",
        "title": detail["title"],
        "author": detail["author"],
        "text_length": len(full_text),
        "image_count": len(image_urls),
        "next": "python3 scripts/push.py",
    }
    print(json.dumps(status, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="B站视频/图文摘要")
    parser.add_argument("url", help="B站视频 URL / BV ID / 图文 opus URL / opus ID")
    parser.add_argument("--no-push", action="store_true", help="不推送飞书，仅输出摘要")
    parser.add_argument("--no-cleanup", action="store_true", help="不清理临时文件")
    parser.add_argument("--quiet", action="store_true", help="静默模式，不输出 LLM 流式内容到 stderr（agent 调用时使用）")
    args = parser.parse_args()

    config = load_base_config()

    load_bili_env()
    load_llm_env()

    # URL 类型检测
    is_opus = "/opus/" in args.url or (
        args.url.strip().isdigit() and "BV" not in args.url.upper()
    )

    if is_opus:
        _main_opus(args, config)
    else:
        _main_video(args, config)


if __name__ == "__main__":
    main()
