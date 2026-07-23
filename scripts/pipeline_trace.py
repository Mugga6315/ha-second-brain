#!/usr/bin/env python3
"""Drive the real HA Assist pipeline and print thinking, tool calls, results, speech.

The only test that proves anything about tool choice + argument filling: ask the
real assistant over `assist_pipeline/run` (intent stage only, no STT/TTS) and watch
what the model actually does. Unlike `/api/conversation/process`, this produces a
real pipeline run (visible in Assist debug) and streams every tool call.

Config comes from the environment (keep credentials out of the repo):
    HA_URL       e.g. http://192.168.1.129:8123
    HA_TOKEN     long-lived access token
    HA_PIPELINE  pipeline id (see .storage/assist_pipeline.pipelines)

Usage:
    HA_URL=... HA_TOKEN=... HA_PIPELINE=... \
        python3 scripts/pipeline_trace.py "frage eins" "frage zwei"
Prefix a question with 'en::' to run it in English (default is de).
"""
import asyncio
import json
import os
import sys

import aiohttp

HA_URL = os.environ["HA_URL"].rstrip("/")
TOKEN = os.environ["HA_TOKEN"]
PIPELINE = os.environ["HA_PIPELINE"]
WS_URL = HA_URL.replace("http", "ws", 1) + "/api/websocket"


async def ask(ws, ident, text):
    await ws.send_json({
        "id": ident, "type": "assist_pipeline/run",
        "start_stage": "intent", "end_stage": "intent", "pipeline": PIPELINE,
        "input": {"text": text}, "conversation_id": None,
    })
    await ws.receive_json()  # command ack
    content = ""
    while True:
        ev = await asyncio.wait_for(ws.receive_json(), timeout=200)
        if ev.get("type") != "event":
            continue
        et = ev["event"]["type"]
        data = ev["event"].get("data") or {}
        if et == "intent-progress":
            delta = data.get("chat_log_delta", {}) or {}
            if isinstance(delta.get("content"), str):
                content += delta["content"]
            for d in delta.get("tool_calls", []) or []:
                print(f"  TOOL CALL: {d.get('tool_name')} "
                      f"{json.dumps(d.get('tool_args'), ensure_ascii=False)}")
            if (tr := delta.get("tool_result")) is not None:
                print(f"  TOOL RESULT: {json.dumps(tr, ensure_ascii=False)[:500]}")
        if et == "intent-end":
            r = data.get("intent_output", {}).get("response", {})
            sp = r.get("speech", {}).get("plain", {}).get("speech", "").strip()
            if content.strip():
                print(f"  THINKING/TEXT: {content.strip()[:800]}")
            print(f"  SPEECH: {sp[:300]}")
        if et in ("run-end", "error"):
            if et == "error":
                print(f"  ERROR: {json.dumps(data, ensure_ascii=False)[:400]}")
            return


async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS_URL) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            await ws.receive_json()  # auth_ok
            for i, q in enumerate(sys.argv[1:], start=20):
                if q.startswith("en::"):
                    q = q[4:]
                print(f"\nQ: {q}")
                try:
                    await ask(ws, i, q)
                except Exception as e:  # keep going through the rest of the batch
                    print(f"  EXC: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
