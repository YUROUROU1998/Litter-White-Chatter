import json
from datetime import datetime, timezone, timedelta
from openai import OpenAI
from duckduckgo_search import DDGS

TZ_TW = timezone(timedelta(hours=8))

def _today() -> str:
    return datetime.now(TZ_TW).strftime("%Y-%m-%d")

client = OpenAI()

MODEL = "gpt-5.4-mini"

TODOS_PROMPT = """你是待辦事項助理。今天日期是 {today}。根據用戶輸入回傳 JSON。
action 值域：add / done / delete / delete_all / delete_by_date / delete_by_month / edit / unknown
- add: {{"action":"add","items":[{{"title":"動詞開頭","category":"分類","priority":"優先度","due_date":"YYYY-MM-DD","due_time":"HH:MM或null"}}]}}
- done: {{"action":"done","ids":[3,5]}}
- delete: {{"action":"delete","ids":[4,5]}}
- delete_all: {{"action":"delete_all"}}
- delete_by_date: {{"action":"delete_by_date","date":"YYYY-MM-DD"}}
- delete_by_month: {{"action":"delete_by_month","month":"YYYY-MM"}}
- edit: {{"action":"edit","id":編號,"updates":{{"title":"","category":"","priority":"","due_date":"","due_time":""}}}} updates只含要改的欄位
- unknown: 無法判斷時回傳
category：生活/工作/健康/購物/娛樂/其他　priority：高/中/低
due_date預設{today}　due_time有明確時間填HH:MM(24h)否則null
只回傳JSON。"""

TX_PROMPT = """你是記帳助理。今天日期是 {today}。根據用戶輸入回傳 JSON。
action 值域：add / delete / delete_all / delete_by_date / delete_by_month / edit / unknown
- add: {{"action":"add","type":"收入或支出","category":"分類","amount":正整數,"description":"簡述","tx_date":"YYYY-MM-DD"}}
- delete: {{"action":"delete","ids":[4,5]}}
- delete_all: {{"action":"delete_all"}}
- delete_by_date: {{"action":"delete_by_date","date":"YYYY-MM-DD"}}
- delete_by_month: {{"action":"delete_by_month","month":"YYYY-MM"}}
- edit: {{"action":"edit","id":編號,"updates":{{"type":"","category":"","amount":0,"description":"","tx_date":""}}}} updates只含要改的欄位
- unknown: 無法判斷時回傳
type：收入/支出　category：餐飲/交通/娛樂/購物/醫療/薪資/獎金/其他
amount正整數　tx_date預設{today}　只有明確含金額或消費/收入行為才判斷為交易
只回傳JSON。"""


def _parse_json_response(content: str) -> dict | None:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(content)


def agent_parse_todos(user_text: str) -> dict | None:
    today = _today()
    resp = client.chat.completions.create(
        model=MODEL,
        max_completion_tokens=500,
        messages=[
            {"role": "system", "content": TODOS_PROMPT.format(today=today)},
            {"role": "user", "content": user_text}
        ],
        temperature=0.3
    )
    try:
        return _parse_json_response(resp.choices[0].message.content)
    except (json.JSONDecodeError, IndexError, AttributeError):
        return None


def agent_parse_transaction(user_text: str) -> dict | None:
    today = _today()
    resp = client.chat.completions.create(
        model=MODEL,
        max_completion_tokens=500,
        messages=[
            {"role": "system", "content": TX_PROMPT.format(today=today)},
            {"role": "user", "content": user_text}
        ],
        temperature=0.3
    )
    try:
        return _parse_json_response(resp.choices[0].message.content)
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
                "required": ["query"],
                "additionalProperties": False
            },
            "strict": True
        }
    }
]


def _execute_tool(name: str, args: dict) -> str:
    if name == "web_search":
        try:
            results = DDGS().text(args["query"], max_results=5, region="tw-tzh")
            if results:
                return "\n".join(f"- {r['title']}: {r['body']}" for r in results)
            return "沒有找到相關結果"
        except Exception as e:
            print(f"[web_search error] query={args.get('query')}, error={e}")
            return "搜尋暫時無法使用"
    return "未知工具"


def agent_chat(user_text: str, history: list) -> str:
    today = _today()
    messages = [
        {"role": "system", "content": (
            f"你是一個友善的智慧助理。今天日期是 {today}。"
            "你可以回答各種問題，包括生活、料理、旅遊、知識等。"
            "涉及天氣、新聞、股價、匯率、即時資訊等問題，你可以使用 web_search 工具搜尋，"
            "絕對不要自己猜測或編造即時資訊。"
            "如果是常識或知識性問題，直接回答即可。"
            "回答請使用繁體中文，保持簡潔友善，限制1000字以內。"
        )}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            max_completion_tokens=1000,
            messages=messages,
            tools=CHAT_TOOLS,
            temperature=0.7
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            tool_names = [tc.function.name for tc in msg.tool_calls]
            print(f"[chat debug] tool_calls={tool_names}")
            messages.append(msg)
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = _execute_tool(tc.function.name, args)
                print(f"[chat debug] tool={tc.function.name}, result_len={len(result)}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result
                })

            resp2 = client.chat.completions.create(
                model=MODEL,
                max_completion_tokens=1000,
                messages=messages,
                temperature=0.7
            )
            answer = resp2.choices[0].message.content
            print(f"[chat debug] resp2 content_len={len(answer or '')}, finish={resp2.choices[0].finish_reason}")
            return answer or f"[debug] resp2 empty, finish={resp2.choices[0].finish_reason}, tools={tool_names}"

        print(f"[chat debug] no tool_calls, content_len={len(msg.content or '')}, finish={resp.choices[0].finish_reason}")
        return msg.content or f"[debug] empty content, finish={resp.choices[0].finish_reason}"

    except Exception as e:
        print(f"[agent_chat error] user_text={user_text[:50]}, error={e}")
        return f"AI 暫時無法回應：{type(e).__name__}: {e}"
