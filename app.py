from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from groq import Groq
import os
import hmac
import hashlib
import time
import json
import httpx
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

    modal_view = {
        "type": "modal",
        "callback_id": "select_model",
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

    # Open modal
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

    # Stage 1: After selecting model, show question modal
    if callback_id == "select_model":
        selected_model = payload["view"]["state"]["values"]["model_block"]["model_action"]["selected_option"]["value"]
        trigger_id = payload["trigger_id"]

        question_modal = {
            "type": "modal",
            "callback_id": "submit_question",
            "private_metadata": selected_model,
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

        async with httpx.AsyncClient() as http_client:
            headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
            await http_client.post(
                "https://slack.com/api/views.open",
                headers=headers,
                json={"trigger_id": trigger_id, "view": question_modal},
            )

        return PlainTextResponse("")

    # Stage 2: Final Submission
    elif callback_id == "submit_question":
        model = payload["view"]["private_metadata"]
        question = payload["view"]["state"]["values"]["question_block"]["question_action"]["value"]
        user_id = payload["user"]["id"]

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

            # Send response as ephemeral message
            response_text = response_text.strip()
            async with httpx.AsyncClient() as http_client:
                await http_client.post(
                    "https://slack.com/api/chat.postEphemeral",
                    headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                    json={
                        "channel": payload["user"]["id"],  # ephemeral to user
                        "user": payload["user"]["id"],
                        "text": f"*Answer:*\n{response_text}"
                    },
                )

        except Exception as e:
            return PlainTextResponse(f"Error: {str(e)}")

        return PlainTextResponse("")

    return PlainTextResponse("")
