import os
import json
from datetime import date
from openai import OpenAI
from duckduckgo_search import DDGS

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
)

MODEL = "deepseek-chat"

def agent_parse_todos(user_text: str) -> dict | None:
    today = date.today().isoformat()
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": f"""你是待辦事項助理。今天日期是 {today}。
根據用戶輸入，判斷意圖並回傳 JSON。

1. 如果是新增待辦，回傳：
{{ "action": "add", "items": [{{ "title": "動詞開頭繁體中文", "category": "分類", "priority": "優先度", "due_date": "YYYY-MM-DD" }}] }}

2. 如果是標記完成，回傳：
{{ "action": "done", "ids": [3, 5] }}

3. 如果是刪除待辦，回傳：
{{ "action": "delete", "ids": [4, 5] }}

4. 如果無法判斷（閒聊、無關內容），回傳：
{{ "action": "unknown" }}

category 值域：生活 / 工作 / 健康 / 購物 / 娛樂 / 其他
priority 值域：高 / 中 / 低
due_date：若無明確日期則填 {today}

只回傳 JSON，不要任何其他文字。"""},
            {"role": "user", "content": user_text}
        ],
        temperature=0.3
    )
    try:
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(content)
    except (json.JSONDecodeError, IndexError, AttributeError):
        return None


def agent_parse_transaction(user_text: str) -> dict | None:
    today = date.today().isoformat()
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": f"""你是記帳助理。今天日期是 {today}。
根據用戶輸入，判斷意圖並回傳 JSON。

1. 如果是記錄一筆交易，回傳：
{{ "action": "add", "type": "收入或支出", "category": "分類", "amount": 金額正整數, "description": "簡短描述", "tx_date": "YYYY-MM-DD" }}

2. 如果是刪除記錄，回傳：
{{ "action": "delete", "ids": [4, 5] }}

3. 如果無法判斷（閒聊、查詢、無關內容），回傳：
{{ "action": "unknown" }}

type 值域：收入 / 支出
category 值域：餐飲 / 交通 / 娛樂 / 購物 / 醫療 / 薪資 / 獎金 / 其他
amount：正整數（無論收支都填正數）
tx_date：交易發生日期，若無明確日期則填 {today}
只有明確包含金額或消費/收入行為的輸入才判斷為交易。

只回傳 JSON，不要任何其他文字。"""},
            {"role": "user", "content": user_text}
        ],
        temperature=0.3
    )
    try:
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(content)
    except (json.JSONDecodeError, IndexError, AttributeError):
        return None


# ── Chat mode: free conversation with tool calling ──

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜尋網路即時資訊，例如天氣、新聞、股價、最新消息等",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜尋關鍵字"
                    }
                },
                "required": ["query"]
            }
        }
    }
]


def _execute_tool(name: str, args: dict) -> str:
    if name == "web_search":
        try:
            results = DDGS().text(args["query"], max_results=3)
            if results:
                return "\n".join(f"- {r['title']}: {r['body']}" for r in results)
            return "沒有找到相關結果"
        except Exception:
            return "搜尋暫時無法使用"
    return "未知工具"


def agent_chat(user_text: str, history: list) -> str:
    today = date.today().isoformat()
    messages = [
        {"role": "system", "content": (
            f"你是一個友善的智慧助理。今天日期是 {today}。"
            "你可以回答各種問題，包括生活、料理、旅遊、知識等。"
            "如果需要即時資訊（天氣、新聞、股價等），請使用搜尋工具。"
            "如果是常識或知識性問題，直接回答即可，不需要搜尋。"
            "回答請使用繁體中文，保持簡潔友善。"
        )}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            max_tokens=1000,
            messages=messages,
            tools=CHAT_TOOLS,
            temperature=0.7
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            assistant_msg = {"role": "assistant", "content": msg.content or ""}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in msg.tool_calls
            ]
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = _execute_tool(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result
                })

            resp = client.chat.completions.create(
                model=MODEL,
                max_tokens=1000,
                messages=messages,
                temperature=0.7
            )
            msg = resp.choices[0].message

        return msg.content or "抱歉，我無法回答這個問題"
    except Exception:
        return "AI 暫時無法回應，請稍後再試"
