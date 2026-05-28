import re
import json
from datetime import datetime, timezone, timedelta
from openai import OpenAI
from duckduckgo_search import DDGS

TZ_TW = timezone(timedelta(hours=8))

def _today() -> str:
    return datetime.now(TZ_TW).strftime("%Y-%m-%d")

client = OpenAI()

MODEL = "gpt-5.4-mini"

def agent_parse_todos(user_text: str) -> dict | None:
    today = _today()
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1000,
        messages=[
            {"role": "system", "content": f"""你是待辦事項助理。今天日期是 {today}。
根據用戶輸入，判斷意圖並回傳 JSON。

1. 如果是新增待辦，回傳：
{{ "action": "add", "items": [{{ "title": "動詞開頭繁體中文", "category": "分類", "priority": "優先度", "due_date": "YYYY-MM-DD", "due_time": "HH:MM 或 null" }}] }}

2. 如果是標記完成，回傳：
{{ "action": "done", "ids": [3, 5] }}

3. 如果是刪除待辦，回傳：
{{ "action": "delete", "ids": [4, 5] }}

4. 如果是刪除全部待辦，回傳：
{{ "action": "delete_all" }}

5. 如果是刪除某天的待辦，回傳：
{{ "action": "delete_by_date", "date": "YYYY-MM-DD" }}

6. 如果是刪除某月的待辦，回傳：
{{ "action": "delete_by_month", "month": "YYYY-MM" }}

7. 如果是修改待辦，回傳：
{{ "action": "edit", "id": 編號, "updates": {{ "title": "新標題", "category": "新分類", "priority": "新優先度", "due_date": "YYYY-MM-DD", "due_time": "HH:MM" }} }}
updates 只包含用戶要修改的欄位，不需要全部填寫。

8. 如果無法判斷（閒聊、無關內容），回傳：
{{ "action": "unknown" }}

category 值域：生活 / 工作 / 健康 / 購物 / 娛樂 / 其他
priority 值域：高 / 中 / 低
due_date：若無明確日期則填 {today}
due_time：若有明確時間（如下午3點、15:30、早上9點）填 HH:MM（24小時制），否則填 null

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
    today = _today()
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

3. 如果是刪除全部記錄，回傳：
{{ "action": "delete_all" }}

4. 如果是刪除某天的記錄，回傳：
{{ "action": "delete_by_date", "date": "YYYY-MM-DD" }}

5. 如果是刪除某月的記錄，回傳：
{{ "action": "delete_by_month", "month": "YYYY-MM" }}

6. 如果是修改記錄，回傳：
{{ "action": "edit", "id": 編號, "updates": {{ "type": "收入或支出", "category": "分類", "amount": 金額, "description": "描述", "tx_date": "YYYY-MM-DD" }} }}
updates 只包含用戶要修改的欄位，不需要全部填寫。

7. 如果無法判斷（閒聊、查詢、無關內容），回傳：
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


def _parse_dsml_tool_calls(content: str) -> list[dict] | None:
    """Parse DeepSeek DSML text-format tool calls as fallback."""
    if "DSML" not in content:
        return None
    calls = []
    for m in re.finditer(
        r'<[|\s]*DSML[|\s]*invoke\s+name="(\w+)"[^>]*>(.*?)</[|\s]*DSML[|\s]*invoke>',
        content, re.DOTALL
    ):
        name = m.group(1)
        params = {}
        for pm in re.finditer(
            r'<[|\s]*DSML[|\s]*parameter\s+name="(\w+)"[^>]*>(.*?)</[|\s]*DSML[|\s]*parameter>',
            m.group(2), re.DOTALL
        ):
            params[pm.group(1)] = pm.group(2).strip()
        calls.append({"name": name, "arguments": params})
    return calls if calls else None


def _strip_dsml(content: str) -> str:
    """Remove any DSML markup from response content."""
    if not content or "DSML" not in content:
        return content or ""
    cleaned = re.sub(r'<[|\s]*/?[|\s]*DSML[|\s]*[^>]*>', '', content).strip()
    return cleaned if cleaned else ""


def _do_search_and_answer(messages: list, search_results: str) -> str:
    """Given search results, ask LLM to generate a final answer."""
    messages.append({"role": "assistant", "content": "我來搜尋一下。"})
    messages.append({"role": "user", "content":
        f"搜尋結果：\n{search_results}\n\n請根據以上結果回答我之前的問題。"})
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1500,
        messages=messages,
        temperature=0.7
    )
    answer = resp.choices[0].message.content or ""
    return _strip_dsml(answer)


def agent_chat(user_text: str, history: list) -> str:
    today = _today()
    messages = [
        {"role": "system", "content": (
            f"你是一個友善的智慧助理。今天日期是 {today}。"
            "你可以回答各種問題，包括生活、料理、旅遊、知識等。"
            "涉及天氣、新聞、股價、匯率、即時資訊等問題，你必須使用 web_search 工具搜尋，"
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
            max_tokens=1500,
            messages=messages,
            tools=CHAT_TOOLS,
            temperature=0.7
        )
        msg = resp.choices[0].message

        # ── Path A: Standard tool_calls ──
        if msg.tool_calls:
            messages.append(msg)

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                result = _execute_tool(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result
                })

            resp2 = client.chat.completions.create(
                model=MODEL,
                max_tokens=1500,
                messages=messages,
                tools=CHAT_TOOLS,
                temperature=0.7
            )
            answer = resp2.choices[0].message.content or ""
            return _strip_dsml(answer) or "抱歉，我無法回答這個問題"

        # ── Path B: DSML text-format tool calls (fallback) ──
        if msg.content and "DSML" in msg.content:
            dsml_calls = _parse_dsml_tool_calls(msg.content)
            if dsml_calls:
                results = []
                for call in dsml_calls:
                    result = _execute_tool(call["name"], call["arguments"])
                    results.append(result)
                combined = "\n".join(results)
                answer = _do_search_and_answer(messages, combined)
                return answer or "抱歉，我無法回答這個問題"
            # DSML detected but parsing failed — direct search as fallback
            print(f"[DSML parse failed] content={msg.content[:200]}")
            search_result = _execute_tool("web_search", {"query": user_text})
            answer = _do_search_and_answer(messages, search_result)
            return answer or "抱歉，我無法回答這個問題"

        # ── Path C: Normal text response (no tool call) ──
        return _strip_dsml(msg.content) or "抱歉，我無法回答這個問題"

    except Exception as e:
        print(f"[agent_chat error] user_text={user_text[:50]}, error={e}")
        return "AI 暫時無法回應，請稍後再試"
