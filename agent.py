import os
import json
from datetime import date
from openai import OpenAI

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
