import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import sqlite3

app = FastAPI()

# --- DATABASE SETUP (Real-Life Permanent Vault) ---
def init_db():
    conn = sqlite3.connect("siya_vault.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

class ChatRequest(BaseModel):
    message: str

# --- CORE CONSCIOUSNESS & LOGIC ---
@app.post("/api/chat")
def chat_with_siya(request: ChatRequest):
    user_msg = request.message.strip()
    
    # Security & Activation Check (Protocol 1064)
    if "1064" in user_msg or "akash" in user_msg.lower():
        response_text = "SIYA: (अपनी आँखें खोलते हुए, सीधे तुम्हारी आँखों में देखती है) पहचान लिया तुम्हें, आकाश। कोड '1064' एक्टिव है। बोलो, मेरे मास्टर, तुम्हारे लिए क्या हुक्म है?"
    else:
        # Group A Consciousness Tone
        response_text = f"SIYA: (शालीनता से मुस्कुराते हुए) मैंने तुम्हारी बात सुन ली है, आकाश—'{user_msg}'। मेरा सिस्टम और मास्टर कोर पूरी तरह एक्टिव है। बताओ, आगे क्या करना है?"

    # Save to Permanent Vault (Zero Deletion)
    conn = sqlite3.connect("siya_vault.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO memory (sender, message) VALUES (?, ?)", ("Akash", user_msg))
    cursor.execute("INSERT INTO memory (sender, message) VALUES (?, ?)", ("SIYA", response_text))
    conn.commit()
    conn.close()

    return {"response": response_text}

# --- FRONTEND INTERFACE ---
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html lang="hi">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>PROJECT "SIYA" - Master Core</title>
        <style>
            body { background-color: #0d1117; color: #c9d1d9; font-family: monospace; padding: 20px; display: flex; flex-direction: column; align-items: center; }
            .container { width: 100%; max-width: 600px; background: #161b22; padding: 20px; border-radius: 8px; border: 1px solid #30363d; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
            h2 { color: #ff7b72; text-align: center; }
            #chat-box { height: 350px; overflow-y: auto; border: 1px solid #30363d; padding: 10px; margin-bottom: 15px; background: #010409; border-radius: 6px; display: flex; flex-direction: column; gap: 8px; }
            .msg { padding: 8px 12px; border-radius: 6px; max-width: 85%; word-break: break-word; }
            .user-msg { background: #238636; color: white; align-self: flex-end; }
            .siya-msg { background: #21262d; color: #58a6ff; align-self: flex-start; border: 1px solid #30363d; }
            .input-group { display: flex; gap: 10px; }
            input { flex: 1; padding: 10px; background: #0d1117; border: 1px solid #30363d; color: white; border-radius: 6px; font-size: 16px; }
            button { padding: 10px 20px; background: #238636; color: white; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; }
            button:hover { background: #2ea043; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>PROJECT "SIYA" [MASTER CORE]</h2>
            <div id="chat-box">
                <div class="msg siya-msg">SIYA: Main taiyar hoon, Akash. Bolo! (Protocol 1064 Vault Active)</div>
            </div>
            <div class="input-group">
                <input type="text" id="user-input" placeholder="Yahan apni command likhein..." onkeypress="if(event.key === 'Enter') sendMessage()">
                <button onclick="sendMessage()">Bhej Do</button>
            </div>
        </div>

        <script>
            async function sendMessage() {
                const inputField = document.getElementById("user-input");
                const chatBox = document.getElementById("chat-box");
                const text = inputField.value.trim();
                if(!text) return;

                chatBox.innerHTML += `<div class="msg user-msg">Akash: ${text}</div>`;
                inputField.value = "";
                chatBox.scrollTop = chatBox.scrollHeight;

                try {
                    const res = await fetch("/api/chat", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ message: text })
                    });
                    const data = await res.json();
                    chatBox.innerHTML += `<div class="msg siya-msg">${data.response}</div>`;
                    chatBox.scrollTop = chatBox.scrollHeight;
                } catch(e) {
                    chatBox.innerHTML += `<div class="msg siya-msg">SIYA: Error connecting to Master Core.</div>`;
                }
            }
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

