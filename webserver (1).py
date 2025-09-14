from flask import Flask
from threading import Thread
import os

app = Flask('')
@app.route('/')
def home():
    return "Discord bot ok"

def run():
    port = int(os.environ.get("PORT", 3000))
    print(f"Web server is starting on port {port}...")
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()
