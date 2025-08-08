from fastapi import Request
from fastapi.responses import PlainTextResponse
import json
import httpx
from groq import Groq

SLACK_BOT_TOKEN =os.getenv("SLACK_BOT_TOKEN")
client = Groq()

@app.post("/slack-interact")
async def handle_interaction(request: Request):
    body = await request.body()
    verify_slack_signature(request, body)

    form_data = await request.form()
    payload = json.loads(form_data.get("payload"))

    # Global Shortcut Clicked
    if payload["type"] == "shortcut" and payload["callback_id"] == "start_lama_modal":
        trigger_id = payload["trigger_id"]

        model_select_modal = {
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

        async with httpx.AsyncClient() as http_client:
            await http_client.post(
                "https://slack.com/api/views.open",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"trigger_id": trigger_id, "view": model_select_modal},
            )

        return PlainTextResponse("")

    # Modal 1 Submitted – Open Question Input Modal
    elif payload["type"] == "view_submission" and payload["view"]["callback_id"] == "select_model":
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
                    "label": {"type": "plain_text", "text": "Your Question"},
                }
            ],
        }

        async with httpx.AsyncClient() as http_client:
            await http_client.post(
                "https://slack.com/api/views.open",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"trigger_id": trigger_id, "view": question_modal},
            )

        return PlainTextResponse("")

    # Final Modal Submitted – Process Model + Question
    elif payload["type"] == "view_submission" and payload["view"]["callback_id"] == "submit_question":
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
                stop=None,
            )

            response_text = ""
            for chunk in completion:
                response_text += chunk.choices[0].delta.content or ""

            # Send ephemeral message to user
            async with httpx.AsyncClient() as http_client:
                await http_client.post(
                    "https://slack.com/api/chat.postEphemeral",
                    headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                    json={
                        "channel": user_id,
                        "user": user_id,
                        "text": f"*Answer:*\n{response_text.strip()}",
                    },
                )

        except Exception as e:
            print("Groq Error:", str(e))

        return PlainTextResponse("")

    return PlainTextResponse("No action")
