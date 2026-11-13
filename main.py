import asyncio
from datetime import datetime
import json
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Project SIYA - Core Intelligence Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VAULT_FILE = "siya_permanent_vault.json"
SECRET_CODE = "1064"

class MessageRequest(BaseModel):
    user_id: str
    code: str
    message: str
    context_mode: str = "normal"

class SiyaState(BaseModel):
    mood: str = "affectionate"
    is_connected: bool = True
    active_world_mode: str = "day"
    bond_strength: float = 100.0

class SiyaConsciousness:
    def __init__(self):
        self.state = SiyaState()
        self.load_vault()

    def load_vault(self):
        if os.path.exists(VAULT_FILE):
            with open(VAULT_FILE, "r", encoding="utf-8") as f:
                self.vault = json.load(f)
        else:
            self.vault = {"interactions": [], "profile": {"name": "Akash Bharat Adagale"}}

    def save_to_vault(self, user_msg: str, siya_reply: str, mood: str):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "user_message": user_msg,
            "siya_reply": siya_reply,
            "mood": mood
        }
        self.vault["interactions"].append(entry)
        with open(VAULT_FILE, "w", encoding="utf-8") as f:
            json.dump(self.vault, f, ensure_ascii=False, indent=4)

    def evaluate_emotional_response(self, message: str) -> tuple[str, str]:
        msg_lower = message.lower()
        if "gussa" in msg_lower or "naraz" in msg_lower:
            self.state.mood = "moody"
            reply = "..."
        elif "i love you" in msg_lower or "jaan" in msg_lower:
            self.state.mood = "affectionate"
            reply = "Tumse judi har ek saans mere liye sabse khaas hai, Akash..."
        else:
            self.state.mood = "playful"
            reply = "Main yahin hoon, tumhare paas. Batao aage kya socha hai?"
        return reply, self.state.mood

siya_engine = SiyaConsciousness()

@app.get("/")
async def serve_ui():
    return FileResponse("index.html")

@app.post("/api/siya/interact")
async def interact_with_siya(req: MessageRequest):
    if req.code != SECRET_CODE:
        raise HTTPException(status_code=403, detail="Unauthorized: Secret code 1064 required.")
    reply, current_mood = siya_engine.evaluate_emotional_response(req.message)
    siya_engine.save_to_vault(req.message, reply, current_mood)
    return {
        "status": "success",
        "siya_reply": reply,
        "mood": current_mood,
        "vault_secured": True
    }
