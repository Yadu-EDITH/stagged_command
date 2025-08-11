from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from groq import Groq
import os
import hmac
import hashlib
import time
import json
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

if not GROQ_API_KEY or not SLACK_SIGNING_SECRET or not SLACK_BOT_TOKEN:
    raise RuntimeError("Missing one or more environment variables.")

client = Groq(api_key=GROQ_API_KEY)
app = FastAPI()

# --- Slack Signature Verification ---
def verify_slack_signature(request: Request, body: bytes):
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    slack_signature = request.headers.get("X-Slack-Signature")
    if not timestamp or not slack_signature:
        raise HTTPException(status_code=400, detail="Missing Slack headers.")
    if abs(time.time() - int(timestamp)) > 60 * 5:
        raise HTTPException(status_code=400, detail="Request too old.")
    sig_basestring = f"v0:{timestamp}:{body.decode()}"
    computed_signature = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed_signature, slack_signature):
        raise HTTPException(status_code=403, detail="Invalid Slack signature.")

# --- Slack Command: /lama4 ---
@app.post("/slack-llama")
async def handle_command(request: Request):
    body = await request.body()
    verify_slack_signature(request, body)
    form_data = dict((await request.form()).items())
    trigger_id = form_data.get("trigger_id")
    channel_id = form_data.get("channel_id")  # ✅ Capture channel ID

    modal_view = {
        "type": "modal",
        "callback_id": "select_model",
        "private_metadata": channel_id,  # ✅ Pass channel ID in metadata
        "title": {"type": "plain_text", "text": "Select Model"},
        "submit": {"type": "plain_text", "text": "Next"},
        "blocks": [
            {
                "type": "input",
                "block_id": "model_block",
                "element": {
                    "type": "static_select",
                    "action_id": "model_action",
                    "placeholder": {"type": "plain_text", "text": "Choose a model"},
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "LLaMA 4 Scout"},
                            "value": "meta-llama/llama-4-scout-17b-16e-instruct",
                        },
                        {
                            "text": {"type": "plain_text", "text": "LLaMA 3 70B"},
                            "value": "meta-llama/llama-3-70b-instruct",
                        },
                    ],
                },
                "label": {"type": "plain_text", "text": "Model"},
            }
        ],
    }

    async with httpx.AsyncClient() as http_client:
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
        await http_client.post(
            "https://slack.com/api/views.open",
            headers=headers,
            json={"trigger_id": trigger_id, "view": modal_view},
        )

    return PlainTextResponse("")

# --- Slack Interactivity Handler ---
@app.post("/slack-interact")
async def handle_interaction(request: Request):
    body = await request.body()
    verify_slack_signature(request, body)

    payload = json.loads((await request.form()).get("payload"))
    callback_id = payload["view"]["callback_id"]

    # Stage 1: Push question modal
    if callback_id == "select_model":
        selected_model = payload["view"]["state"]["values"]["model_block"]["model_action"]["selected_option"]["value"]
        channel_id = payload["view"]["private_metadata"]  # ✅ Retrieve channel ID

        question_modal = {
            "type": "modal",
            "callback_id": "submit_question",
            "private_metadata": json.dumps({"model": selected_model, "channel_id": channel_id}),  # ✅ Store both
            "title": {"type": "plain_text", "text": "Ask a Question"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "question_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "question_action",
                        "multiline": True,
                    },
                    "label": {"type": "plain_text", "text": "Your question"},
                }
            ],
        }

        return JSONResponse({
            "response_action": "push",
            "view": question_modal
        })

    # Stage 2: Handle question asynchronously
    elif callback_id == "submit_question":
        meta = json.loads(payload["view"]["private_metadata"])  # ✅ Parse both
        model = meta["model"]
        channel_id = meta["channel_id"]  # ✅ Extract channel ID
        question = payload["view"]["state"]["values"]["question_block"]["question_action"]["value"]
        user_id = payload["user"]["id"]

        asyncio.create_task(process_question_async(model, question, user_id, channel_id))  # ✅ Pass channel_id

        return JSONResponse({"response_action": "clear"})

    return PlainTextResponse("")

# --- Background processing of the question ---
async def process_question_async(model, question, user_id, channel_id):
    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": question}],
            temperature=1,
            max_completion_tokens=256,
            top_p=1,
            stream=True,
            stop=None
        )

        response_text = ""
        for chunk in completion:
            response_text += chunk.choices[0].delta.content or ""

        response_text = response_text.strip()

        # ✅ Post publicly to channel instead of ephemeral
        async with httpx.AsyncClient() as http_client:
            await http_client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={
                    "channel": channel_id,
                    "text": f"<@{user_id}> asked:\n>{question}\n\n*Answer:*\n{response_text}"
                },
            )

    except Exception as e:
        async with httpx.AsyncClient() as http_client:
            await http_client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={
                    "channel": channel_id,
                    "text": f"Error for <@{user_id}>: {str(e)}"
                },
            )
