import os
import json
from datetime import date
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
)

MODEL = "deepseek-chat"

def agent_parse_todos(user_text: str) -> list | None:
    today = date.today().isoformat()
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": f"""你是待辦事項解析助理。今天日期是 {today}。
將用戶的自然語言輸入拆解為一或多筆待辦事項，回傳 JSON Array。

每筆格式：
{{ "title": "動詞開頭的繁體中文標題", "category": "分類", "priority": "優先度", "due_date": "YYYY-MM-DD" }}

category 值域：生活 / 工作 / 健康 / 購物 / 娛樂 / 其他
priority 值域：高 / 中 / 低
due_date：若無明確日期則填 {today}

只回傳 JSON Array，不要任何其他文字。"""},
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
            {"role": "system", "content": f"""你是記帳解析助理。今天日期是 {today}。
將用戶的自然語言輸入解析為一筆交易記錄，回傳 JSON Object。

格式：
{{ "type": "收入或支出", "category": "分類", "amount": 金額正整數, "description": "簡短描述", "tx_date": "YYYY-MM-DD" }}

type 值域：收入 / 支出
category 值域：餐飲 / 交通 / 娛樂 / 購物 / 醫療 / 薪資 / 獎金 / 其他
amount：正整數（無論收支都填正數）
tx_date：交易發生日期，若無明確日期則填 {today}

只回傳 JSON Object，不要任何其他文字。"""},
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
