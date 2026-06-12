"""推送B站内容摘要到飞书：读取 summary_result.json → 构建飞书卡片 → 发送"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    SUMMARY_RESULT_PATH, setup_logging, load_base_config,
    load_feishu_env, get_feishu_creds,
    get_feishu_token, send_feishu_message, retry,
)

logger = setup_logging("push")


def _build_summary_card(result: dict) -> dict:
    """构建摘要飞书卡片（视频/图文自适应）"""
    content_type = result.get("type", "video")
    title = result.get("title", "")
    url = result.get("url", "")
    summary = result.get("summary", "")

    elements = []

    # 标题链接
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"🎬 [{title}]({url})"}
    })

    # 元信息（按类型）
    if content_type == "opus":
        author = result.get("author", "")
        image_count = result.get("image_count", 0)
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"作者: {author}  |  图片: {image_count}张"}
        })
        header_title = "📝 B站图文摘要"
    else:
        owner = result.get("owner", "")
        duration = result.get("duration", 0)
        minutes, seconds = divmod(duration, 60)
        if minutes < 60:
            duration_str = f"{minutes}:{seconds:02d}"
        else:
            duration_str = f"{minutes // 60}:{minutes % 60}:{seconds:02d}"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"UP主: {owner}  |  时长: {duration_str}"}
        })
        header_title = "📺 B站视频摘要"

    elements.append({"tag": "hr"})

    # 摘要内容（按段落拆分为多个 div，避免单条过长）
    paragraphs = summary.split("\n\n") if summary else ["（无摘要）"]
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > 4000:
            for i in range(0, len(para), 4000):
                elements.append({
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": para[i:i + 4000]}
                })
        else:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": para}
            })

    # ── 原文（折叠面板）──
    original_text = ""
    if content_type == "opus":
        original_text = result.get("original_text", "")
    else:
        original_text = result.get("transcript", "")

    if original_text and original_text.strip():
        text_len = len(original_text)

        # 原文内容按 4000 字符分片，放入 markdown 组件
        ocr_elements = []
        for i in range(0, text_len, 4000):
            chunk = original_text[i:i + 4000]
            ocr_elements.append({
                "tag": "markdown",
                "content": chunk,
            })

        elements.append({"tag": "hr"})
        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📄 原文（共 {text_len} 字）",
                },
                "vertical_align": "center",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "size": "16px 16px",
                },
                "icon_position": "right",
                "icon_expanded_angle": -180,
            },
            "border": {
                "color": "grey",
                "corner_radius": "5px",
            },
            "vertical_spacing": "8px",
            "padding": "8px 8px 8px 8px",
            "elements": ocr_elements,
        })

    # 时间戳
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"🕐 {now_str}"}
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": "blue",
        },
        "elements": elements,
    }


def main():
    if not SUMMARY_RESULT_PATH.exists():
        logger.error(f"摘要结果不存在: {SUMMARY_RESULT_PATH}，请先运行 summarize.py")
        sys.exit(1)

    with open(SUMMARY_RESULT_PATH, encoding="utf-8") as f:
        result = json.load(f)

    # 加载飞书凭据
    load_feishu_env()
    app_id, app_secret, chat_id = get_feishu_creds()

    if not app_id or not app_secret or not chat_id:
        logger.error("飞书凭据不完整，需要 feishu_app_id, feishu_app_secret, feishu_chat_id")
        sys.exit(1)

    # 构建并发送卡片
    card = _build_summary_card(result)
    config = load_base_config()
    feishu_cfg = config.get("feishu", {})
    retry_times = feishu_cfg.get("retry_times", 3)
    retry_delay = feishu_cfg.get("retry_delay", 10)

    token = get_feishu_token(app_id, app_secret)
    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }

    send_with_retry = retry(times=retry_times, delay=retry_delay, logger=logger)(send_feishu_message)
    send_with_retry(token, chat_id, payload)
    logger.info("飞书推送成功")

    print(json.dumps({"status": "sent", "title": result.get("title", "")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
