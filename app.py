import os
import asyncio
import threading
import time
import requests
import json
import subprocess
from flask import Flask, request, jsonify, send_file, Response, stream_with_context, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import edge_tts
import yt_dlp
from faster_whisper import WhisperModel

app = Flask(__name__)

# --- Database & Config ---
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SECRET_KEY'] = 'render-deploy-secret-123'
db = SQLAlchemy(app)

UPLOAD_FOLDER = "downloads"
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- User Model ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# --- Whisper Model (Tiny version for low RAM) ---
print("Loading Whisper model (tiny)...")
try:
    # Render Free Plan အတွက် tiny က အသင့်တော်ဆုံးပါ
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    print("Whisper model loaded!")
except Exception as e:
    print(f"Whisper Model Error: {e}")

processing_status = {}

# --- Utility Functions ---
def get_ffmpeg():
    """Render မှာ path ပြဿနာမရှိအောင် စစ်ဆေးပေးတာပါ"""
    if os.path.exists('./ffmpeg'):
        return './ffmpeg'
    return 'ffmpeg'

def download_media(url):
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(UPLOAD_FOLDER, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }
    if os.path.exists('cookies.txt'):
        ydl_opts['cookiefile'] = 'cookies.txt'
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename, info.get('title', 'Untitled Video')

def transcribe_audio(audio_path):
    segments, info = model.transcribe(audio_path)
    text = " ".join([s.text for s in segments])
    return text.strip(), info.language

def translate_text(text, target='my'):
    try:
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target}&dt=t&q={text}"
        res = requests.get(url, timeout=10).json()
        return "".join([s[0] for s in res[0]])
    except:
        return "ဘာသာပြန်မရပါ (Network Error)"

async def text_to_speech(text, output_file, voice="my-MM-ThihaNeural"):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_file)

def generate_events(process_id):
    while True:
        if process_id in processing_status:
            data = processing_status[process_id]
            yield f"data: {json.dumps(data)}\n\n"
            if data.get('status') in ['completed', 'error']:
                break
        time.sleep(1)

# --- Routes ---

@app.route('/')
@login_required
def index():
    # မူရင်း HTML code အားလုံးကို ဤနေရာတွင် ထားပါသည် (အတိုချုံးပြထားသည်)
    return render_template_html()

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and user.check_password(request.form.get('password')):
            login_user(user)
            return redirect(url_for('index'))
    return '''<body style="font-family:sans-serif;text-align:center;padding:50px;">
    <h2>🔐 Login</h2><form method="post">
    <input name="username" placeholder="Name" required><br><br>
    <input name="password" type="password" placeholder="Password" required><br><br>
    <button type="submit">Login</button></form>
    <p><a href="/signup">အကောင့်ဖွင့်ရန်</a></p></body>'''

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        user = User(username=request.form.get('username'))
        user.set_password(request.form.get('password'))
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('login'))
    return '''<body style="font-family:sans-serif;text-align:center;padding:50px;">
    <h2>📝 Sign Up</h2><form method="post">
    <input name="username" placeholder="Name" required><br><br>
    <input name="password" type="password" placeholder="Password" required><br><br>
    <button type="submit">Register</button></form></body>'''

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/process', methods=['POST'])
@login_required
def process():
    data = request.json
    url = data.get('url')
    option = data.get('option', 'original_video')
    target_lang = data.get('target_lang', 'my')
    
    process_id = str(int(time.time()))
    processing_status[process_id] = {'status': 'processing', 'percent': 10, 'message': 'စတင်နေပါပြီ...'}

    def run_task():
        try:
            # 1. Download
            processing_status[process_id].update({'percent': 25, 'message': 'ဒေါင်းလုဒ်ဆွဲနေသည်...'})
            file_path, title = download_media(url)
            
            # 2. Extract Audio
            processing_status[process_id].update({'percent': 45, 'message': 'အသံဖိုင်ထုတ်ယူနေသည်...'})
            audio_path = file_path.rsplit('.', 1)[0] + ".mp3"
            ffmpeg_path = get_ffmpeg()
            subprocess.run([ffmpeg_path, '-i', file_path, '-q:a', '0', '-map', 'a', audio_path, '-y'], check=True)
            
            # 3. Transcribe & Translate
            processing_status[process_id].update({'percent': 70, 'message': 'ဘာသာပြန်နေသည်...'})
            text, lang = transcribe_audio(audio_path)
            translated = translate_text(text, target_lang)
            
            result = {'title': title, 'transcribed_text': text, 'translated_text': translated}
            if option == 'original_video': result['video_file'] = os.path.basename(file_path)
            if option == 'audio': result['audio_file'] = os.path.basename(audio_path)
            
            processing_status[process_id] = {'status': 'completed', 'percent': 100, 'result': result}
        except Exception as e:
            processing_status[process_id] = {'status': 'error', 'message': str(e)}

    threading.Thread(target=run_task).start()
    return jsonify({'process_id': process_id})

@app.route('/progress/<process_id>')
def progress(process_id):
    return Response(stream_with_context(generate_events(process_id)), mimetype='text/event-stream')

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename))

def render_template_html():
    return '''
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>AI Video Generator</title>
    <style>
        body { font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background: #f4f4f9; }
        .box { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        input, select, button { width: 100%; padding: 12px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        button { background: #4CAF50; color: white; border: none; cursor: pointer; font-weight: bold; }
        .progress-bar { height: 10px; background: #eee; border-radius: 5px; overflow: hidden; display:none; }
        .fill { height: 100%; background: #4CAF50; width: 0%; transition: 0.3s; }
        #result { margin-top: 20px; display: none; padding: 15px; background: #e8f5e9; border-radius: 5px; }
    </style></head>
    <body><div class="box">
        <h3>🎬 AI Video & Audio Downloader</h3>
        <input type="text" id="url" placeholder="TikTok or YouTube Link">
        <select id="option">
            <option value="original_video">Original Video Download</option>
            <option value="audio">Extract Audio (MP3)</option>
        </select>
        <button id="btn" onclick="start()">စတင်မည်</button>
        <div class="progress-bar" id="pb"><div class="fill" id="fill"></div></div>
        <p id="msg"></p>
        <div id="result"></div>
        <hr><a href="/logout">Logout</a>
    </div>
    <script>
        async function start() {
            const url = document.getElementById('url').value;
            if(!url) return alert("Link ထည့်ပါ");
            document.getElementById('btn').disabled = true;
            document.getElementById('pb').style.display = 'block';
            
            const res = await fetch('/process', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url: url, option: document.getElementById('option').value})
            });
            const data = await res.json();
            
            const ev = new EventSource('/progress/' + data.process_id);
            ev.onmessage = (e) => {
                const p = JSON.parse(e.data);
                document.getElementById('fill').style.width = p.percent + '%';
                document.getElementById('msg').innerText = p.message || p.status;
                if(p.status === 'completed') {
                    ev.close();
                    showResult(p.result);
                }
                if(p.status === 'error') {
                    ev.close();
                    alert("Error: " + p.message);
                }
            };
        }
        function showResult(res) {
            let h = `<b>✅ ${res.title}</b><br>`;
            if(res.video_file) h += `<a href="/download/${res.video_file}" download>📥 Download Video</a><br>`;
            if(res.audio_file) h += `<a href="/download/${res.audio_file}" download>📥 Download Audio</a><br>`;
            h += `<p><b>Translated:</b> ${res.translated_text}</p>`;
            document.getElementById('result').innerHTML = h;
            document.getElementById('result').style.display = 'block';
            document.getElementById('btn').disabled = false;
        }
    </script></body></html>'''

if __name__ == '__main__':
    app.run(debug=True)