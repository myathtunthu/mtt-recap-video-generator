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
app.config['SECRET_KEY'] = 'mtt-recap-super-secret'
db = SQLAlchemy(app)

UPLOAD_FOLDER = "downloads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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

# --- Whisper Model (RAM သက်သာအောင် tiny သုံးသည်) ---
print("Loading Whisper model (tiny)...")
model = WhisperModel("tiny", device="cpu", compute_type="int8")

processing_status = {}

# --- Utility Functions ---
def get_ffmpeg():
    return './ffmpeg' if os.path.exists('./ffmpeg') else 'ffmpeg'

def download_media(url):
    ydl_opts = {
        'format': 'best', # Error မတက်အောင် best ပဲထားလိုက်ပါတယ်
        'outtmpl': os.path.join(UPLOAD_FOLDER, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }
    if os.path.exists('cookies.txt'):
        ydl_opts['cookiefile'] = 'cookies.txt'
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info.get('title', 'Untitled')

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
        return text

# --- Routes ---

@app.route('/')
@login_required
def index():
    return render_template_html()

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        if User.query.filter_by(username=request.form.get('username')).first():
            return 'အမည်ရှိပြီးသားပါ'
        user = User(username=request.form.get('username'))
        user.set_password(request.form.get('password'))
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('login'))
    return '''<body style="text-align:center;padding:50px;"><h2>📝 Sign Up</h2><form method="post"><input name="username" placeholder="Name" required><br><br><input name="password" type="password" placeholder="Password" required><br><br><button type="submit">Register</button></form></body>'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and user.check_password(request.form.get('password')):
            login_user(user)
            return redirect(url_for('index'))
    return '''<body style="text-align:center;padding:50px;"><h2>🔐 Login</h2><form method="post"><input name="username" placeholder="Name" required><br><br><input name="password" type="password" placeholder="Password" required><br><br><button type="submit">Login</button></form><p><a href="/signup">Sign Up</a></p></body>'''

@app.route('/process', methods=['POST'])
@login_required
def process():
    data = request.json
    url, option, target_lang = data.get('url'), data.get('option'), data.get('target_lang', 'my')
    process_id = str(int(time.time()))
    processing_status[process_id] = {'status': 'processing', 'percent': 5, 'message': 'စတင်နေသည်...'}

    def run_task():
        try:
            # 1. Download
            processing_status[process_id].update({'percent': 20, 'message': 'ဒေါင်းလုဒ်ဆွဲနေသည်...'})
            file_path, title = download_media(url)
            
            # 2. Extract Audio (Transcript တွေအတွက် လိုအပ်တယ်)
            audio_path = file_path.rsplit('.', 1)[0] + ".mp3"
            subprocess.run([get_ffmpeg(), '-i', file_path, '-q:a', '0', '-map', 'a', audio_path, '-y'], check=True)
            
            result = {'title': title}
            
            # 3. Handle Options
            if option in ['transcript', 'transcript_only', 'audio']:
                processing_status[process_id].update({'percent': 60, 'message': 'စာသားပြောင်းနေသည်...'})
                text, lang = transcribe_audio(audio_path)
                result['transcribed_text'] = text
                
                if option in ['transcript', 'audio']:
                    processing_status[process_id].update({'percent': 80, 'message': 'ဘာသာပြန်နေသည်...'})
                    result['translated_text'] = translate_text(text, target_lang)

            # Assign Files
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

def generate_events(process_id):
    while True:
        if process_id in processing_status:
            yield f"data: {json.dumps(processing_status[process_id])}\n\n"
            if processing_status[process_id]['status'] in ['completed', 'error']: break
        time.sleep(1)

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename))

@app.route('/logout')
def logout():
    logout_user(); return redirect(url_for('login'))

def render_template_html():
    return '''
    <!DOCTYPE html><html><head><meta charset="UTF-8"><title>AI Video Generator</title>
    <style>
        body { font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background: #f0f2f5; }
        .box { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        input, select, button { width: 100%; padding: 12px; margin: 10px 0; border: 1px solid #ddd; border-radius: 8px; }
        button { background: #28a745; color: white; border: none; font-weight: bold; cursor: pointer; }
        .progress { height: 10px; background: #eee; border-radius: 5px; display:none; margin: 15px 0; }
        .fill { height: 100%; background: #28a745; width: 0%; transition: 0.3s; }
        #result { margin-top: 20px; display: none; padding: 15px; background: #fff; border: 1px solid #28a745; border-radius: 8px; }
    </style></head>
    <body><div class="box">
        <h3>🎬 AI Video Generator</h3>
        <input type="text" id="url" placeholder="YouTube or TikTok Link">
        <select id="option">
            <option value="original_video">🎬 Original Video Download</option>
            <option value="transcript_only">📝 စာသားချည်းထုတ် (Transcript)</option>
            <option value="transcript">📝 စာသား + ဘာသာပြန်</option>
            <option value="audio">🎵 အသံချည်းထုတ် (Audio)</option>
        </select>
        <button id="btn" onclick="start()">စတင်မည်</button>
        <div class="progress" id="pb"><div class="fill" id="fill"></div></div>
        <p id="msg" style="text-align:center; color:#666;"></p>
        <div id="result"></div>
        <hr><a href="/logout" style="color:#d9534f; text-decoration:none;">Logout</a>
    </div>
    <script>
        async function start() {
            const url = document.getElementById('url').value;
            if(!url) return alert("Link ထည့်ပါ");
            document.getElementById('btn').disabled = true;
            document.getElementById('pb').style.display = 'block';
            document.getElementById('result').style.display = 'none';
            
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
                if(p.status === 'completed') { ev.close(); showResult(p.result); }
                if(p.status === 'error') { ev.close(); alert("Error: " + p.message); document.getElementById('btn').disabled = false; }
            };
        }
        function showResult(res) {
            let h = `<b>✅ ${res.title}</b><hr>`;
            if(res.video_file) h += `<a href="/download/${res.video_file}" download style="display:block;margin:10px 0;color:#007bff;">📥 Download Video</a>`;
            if(res.audio_file) h += `<a href="/download/${res.audio_file}" download style="display:block;margin:10px 0;color:#007bff;">📥 Download Audio</a>`;
            if(res.transcribed_text) h += `<p><b>Original:</b><br>${res.transcribed_text}</p>`;
            if(res.translated_text) h += `<p><b>Translated:</b><br>${res.translated_text}</p>`;
            document.getElementById('result').innerHTML = h;
            document.getElementById('result').style.display = 'block';
            document.getElementById('btn').disabled = false;
        }
    </script></body></html>'''

if __name__ == '__main__':
    app.run(debug=True)