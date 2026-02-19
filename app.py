import os
import asyncio
import threading
import time
import requests
import queue
import json
from flask import Flask, request, jsonify, send_file, render_template_string, Response, stream_with_context, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import edge_tts
import yt_dlp
from faster_whisper import WhisperModel
import subprocess
import cv2
import numpy as np

app = Flask(__name__)

# Database & Login Setup
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SECRET_KEY'] = 'my-secret-key-change-this-123'
db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User Model
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

# Processing status သိမ်းမယ့် dictionary
processing_status = {}

# ဖိုင်တွေ သိမ်းမယ့် Folder
UPLOAD_FOLDER = "downloads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Whisper Model ကို Load လုပ်မယ်
print("Loading Whisper model...")
model = WhisperModel("base", device="cpu", compute_type="int8")
print("Whisper model loaded!")

# Voice List
VOICES = {
    "my": ["my-MM-ThihaNeural"],
    "en": ["en-US-JennyNeural"],
    "th": ["th-TH-PremwadeeNeural"],
    "zh": ["zh-CN-XiaoxiaoNeural"],
    "ja": ["ja-JP-NanamiNeural"]
}

def download_video(url):
    """YouTube လင့်ခ်ကနေ Video နဲ့ Audio ဆွဲထုတ်မယ်"""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(UPLOAD_FOLDER, '%(title)s.%(ext)s'),
        'quiet': True,
        'merge_output_format': 'mp4'
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        video_file = filename.rsplit('.', 1)[0] + '.mp4'
        return video_file, info['title']

def download_audio_only(url):
    """Audio ချည်းပဲ ဆွဲထုတ်မယ်"""
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': os.path.join(UPLOAD_FOLDER, '%(title)s.%(ext)s'),
        'quiet': True
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        audio_file = filename.rsplit('.', 1)[0] + '.mp3'
        return audio_file, info['title']

def transcribe_audio(audio_path):
    """Audio ကနေ စာသားထုတ်မယ်"""
    segments, info = model.transcribe(audio_path, language=None)
    full_text = ""
    for segment in segments:
        full_text += segment.text + " "
    return full_text.strip(), info.language

def translate_text(text, dest_lang='my'):
    """Google Translate API ကိုသုံးပြီး ဘာသာပြန်မယ်"""
    try:
        from googletrans import Translator
        translator = Translator()
        result = translator.translate(text, dest=dest_lang)
        return result.text
    except:
        try:
            lang_map = {
                'my': 'my',
                'en': 'en',
                'th': 'th',
                'zh': 'zh',
                'ja': 'ja'
            }
            target = lang_map.get(dest_lang, 'my')
            
            url = "https://api.mymemory.translated.net/get"
            params = {
                "q": text[:450],
                "langpair": f"en|{target}"
            }
            response = requests.get(url, params=params)
            data = response.json()
            if response.status_code == 200 and 'responseData' in data:
                return data['responseData']['translatedText']
        except:
            pass
        return "ဘာသာပြန်ရာတွင် အဆင်မပြေပါ"

async def text_to_speech(text, output_file, voice="en-US-JennyNeural"):
    """စာသားကို အသံဖိုင်အဖြစ် ပြောင်းမယ်"""
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_file)

def run_async_in_thread(coro, *args):
    asyncio.run(coro(*args))

def create_video_with_audio(video_path, audio_path, output_path, mirror=True, color_adjust=True):
    """Video နဲ့ Audio ကိုပေါင်းမယ်"""
    try:
        filters = []
        if mirror:
            filters.append('hflip')
        if color_adjust:
            filters.append('eq=brightness=0.05:contrast=1.2:saturation=1.2')
        
        filter_str = ','.join(filters) if filters else 'null'
        
        cmd = [
            'ffmpeg', '-i', video_path, '-i', audio_path,
            '-filter_complex', f'[0:v]fps=20,scale=640:360,{filter_str}[v]',
            '-map', '[v]', '-map', '1:a:0',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30',
            '-c:a', 'aac', '-b:a', '96k',
            '-shortest', '-y', output_path
        ]
        
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except Exception as e:
        print(f"Video creation error: {e}")
        return False

def generate_events(process_id):
    """Server-Sent Events ကို stream လုပ်မယ်"""
    while True:
        if process_id in processing_status:
            data = processing_status[process_id]
            yield f"data: {json.dumps(data)}\n\n"
            if data.get('status') == 'completed' or data.get('status') == 'error':
                timer = threading.Timer(5.0, lambda: processing_status.pop(process_id, None))
                timer.start()
                break
        time.sleep(0.5)

# Login Pages
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if User.query.filter_by(username=username).first():
            return 'ဒီအမည်ရှိပြီးသားပါ'
        
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        return redirect(url_for('login'))
    
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Sign Up - Video Generator</title>
        <style>
            body { font-family: Arial; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); height: 100vh; display: flex; justify-content: center; align-items: center; margin: 0; }
            .container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); width: 300px; }
            h2 { text-align: center; color: #333; }
            input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
            button { width: 100%; padding: 10px; background: #4CAF50; color: white; border: none; border-radius: 5px; cursor: pointer; }
            .link { text-align: center; margin-top: 15px; }
            .link a { color: #667eea; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>📝 အကောင့်အသစ်ဆောက်မယ်</h2>
            <form method="post">
                <input type="text" name="username" placeholder="နာမည်" required>
                <input type="password" name="password" placeholder="လျှို့ဝှက်နံပါတ်" required>
                <button type="submit">ဆောက်မယ်</button>
            </form>
            <div class="link"><a href="/login">အကောင့်ရှိပြီးသားလား? Login ဝင်မယ်</a></div>
        </div>
    </body>
    </html>
    '''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        
        return 'နာမည် သို့မဟုတ် လျှို့ဝှက်နံပါတ် မှားနေတယ်'
    
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Login - Video Generator</title>
        <style>
            body { font-family: Arial; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); height: 100vh; display: flex; justify-content: center; align-items: center; margin: 0; }
            .container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); width: 300px; }
            h2 { text-align: center; color: #333; }
            input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
            button { width: 100%; padding: 10px; background: #4CAF50; color: white; border: none; border-radius: 5px; cursor: pointer; }
            .link { text-align: center; margin-top: 15px; }
            .link a { color: #667eea; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>🔐 Login ဝင်မယ်</h2>
            <form method="post">
                <input type="text" name="username" placeholder="နာမည်" required>
                <input type="password" name="password" placeholder="လျှို့ဝှက်နံပါတ်" required>
                <button type="submit">ဝင်မယ်</button>
            </form>
            <div class="link"><a href="/signup">အကောင့်မရှိသေးဘူးလား? အသစ်ဆောက်မယ်</a></div>
        </div>
    </body>
    </html>
    '''

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/progress/<process_id>')
def progress(process_id):
    return Response(stream_with_context(generate_events(process_id)), mimetype='text/event-stream')

@app.route('/')
@login_required
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>AI Video & Audio Generator</title>
        <style>
            body { font-family: 'Pyidaungsu', Arial; max-width: 800px; margin: 50px auto; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
            .container { background: white; padding: 30px; border-radius: 15px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
            .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
            .logout-btn { padding: 8px 15px; background: #f44336; color: white; text-decoration: none; border-radius: 5px; }
            h1 { text-align: center; color: #333; margin: 0; }
            input, select { width: 100%; padding: 15px; margin: 10px 0; border: 2px solid #ddd; border-radius: 8px; font-size: 16px; }
            .option-group { display: flex; gap: 10px; margin: 20px 0; flex-wrap: wrap; }
            .option-card { flex: 1; min-width: 120px; padding: 15px; border: 2px solid #ddd; border-radius: 8px; cursor: pointer; text-align: center; }
            .option-card.selected { border-color: #4CAF50; background: #e8f5e9; }
            button { padding: 15px; background: #4CAF50; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 18px; width: 100%; }
            .progress-container { margin: 20px 0; display: none; }
            .progress-bar { width: 100%; height: 20px; background: #ddd; border-radius: 10px; overflow: hidden; }
            .progress-fill { height: 100%; background: #4CAF50; width: 0%; transition: width 0.3s ease; }
            .progress-text { text-align: center; margin-top: 10px; font-weight: bold; }
            .result-box { margin-top: 20px; padding: 20px; border-radius: 8px; background: #f5f5f5; display: none; }
            .download-btn { display: inline-block; padding: 10px 20px; background: #2196F3; color: white; text-decoration: none; border-radius: 5px; margin: 5px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎬 AI Video Generator</h1>
                <a href="/logout" class="logout-btn">🚪 Logout</a>
            </div>
            <input type="text" id="url" placeholder="YouTube/TikTok Link">
            
            <div class="option-group">
                <div class="option-card" onclick="selectOption('transcript_only')" id="opt-transcript_only">📝 စာသားချည်းထုတ်</div>
                <div class="option-card" onclick="selectOption('transcript')" id="opt-transcript">📝 စာသား+ဘာသာပြန်</div>
                <div class="option-card" onclick="selectOption('audio')" id="opt-audio">🎵 အသံချည်းထုတ်</div>
                <div class="option-card" onclick="selectOption('video')" id="opt-video">🎬 Video အပြည့်ထုတ်</div>
            </div>
            
            <div id="lang-select" style="display:none;">
                <select id="target_lang">
                    <option value="my">မြန်မာ</option>
                    <option value="en">အင်္ဂလိပ်</option>
                </select>
            </div>
            
            <button onclick="processVideo()">စတင်မည်</button>
            
            <div class="progress-container" id="progressContainer">
                <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
                <div class="progress-text" id="progressText">0%</div>
            </div>
            
            <div id="result" class="result-box"></div>
        </div>

        <script>
            let selectedOption = 'transcript_only';
            function selectOption(opt) {
                selectedOption = opt;
                document.querySelectorAll('.option-card').forEach(el => el.classList.remove('selected'));
                document.getElementById(`opt-${opt}`).classList.add('selected');
                document.getElementById('lang-select').style.display = (opt=='audio'||opt=='video')?'block':'none';
            }
            
            async function processVideo() {
                const url = document.getElementById('url').value;
                if(!url) return alert('URL ထည့်ပါ');
                
                document.getElementById('progressContainer').style.display = 'block';
                
                const res = await fetch('/process', {
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({
                        url, option:selectedOption,
                        target_lang: document.getElementById('target_lang').value
                    })
                });
                const data = await res.json();
                
                const eventSource = new EventSource(`/progress/${data.process_id}`);
                eventSource.onmessage = (e) => {
                    const p = JSON.parse(e.data);
                    document.getElementById('progressFill').style.width = p.percent+'%';
                    document.getElementById('progressText').innerHTML = p.status+' ('+p.percent+'%)';
                    
                    if(p.status === 'completed') {
                        eventSource.close();
                        showResult(p.result);
                    }
                };
            }
            
            function showResult(data) {
                let html = `<strong>✅ ${data.title}</strong><br><hr>`;
                if(data.translated_text) html += '<strong>ဘာသာပြန်:</strong><br>'+data.translated_text;
                if(data.transcribed_text) html += '<br><strong>မူရင်း:</strong><br>'+data.transcribed_text;
                document.getElementById('result').innerHTML = html;
                document.getElementById('result').style.display = 'block';
            }
        </script>
    </body>
    </html>
    '''

@app.route('/process', methods=['POST'])
def process():
    data = request.json
    url = data.get('url')
    option = data.get('option', 'transcript_only')
    target_lang = data.get('target_lang', 'my')
    
    process_id = str(int(time.time()))
    processing_status[process_id] = {'status':'starting','percent':0}
    
    try:
        if option == 'video':
            processing_status[process_id] = {'status':'downloading','percent':10}
            video_path, title = download_video(url)
            audio_path = video_path.replace('.mp4','.mp3')
            
            subprocess.run(['ffmpeg','-i',video_path,'-q:a','0','-map','a',audio_path,'-y'], check=True)
            
            processing_status[process_id] = {'status':'transcribing','percent':40}
            text, lang = transcribe_audio(audio_path)
            
            processing_status[process_id] = {'status':'translating','percent':60}
            translated = translate_text(text, target_lang)
            
            processing_status[process_id] = {'status':'generating audio','percent':80}
            audio_file = f"audio_{int(time.time())}.mp3"
            audio_out = os.path.join(UPLOAD_FOLDER, audio_file)
            voice = VOICES.get(target_lang, ["en-US-JennyNeural"])[0]
            thread = threading.Thread(target=run_async_in_thread, args=(text_to_speech, translated, audio_out, voice))
            thread.start(); thread.join()
            
            processing_status[process_id] = {'status':'rendering video','percent':95}
            video_file = f"video_{int(time.time())}.mp4"
            video_out = os.path.join(UPLOAD_FOLDER, video_file)
            create_video_with_audio(video_path, audio_out, video_out, True, True)
            
            result = {'success':True, 'title':title, 'original_lang':lang, 'translated_text':translated}
            processing_status[process_id] = {'status':'completed','percent':100,'result':result}
            
        elif option == 'audio':
            audio_path, title = download_audio_only(url)
            text, lang = transcribe_audio(audio_path)
            translated = translate_text(text, target_lang)
            
            audio_file = f"audio_{int(time.time())}.mp3"
            audio_out = os.path.join(UPLOAD_FOLDER, audio_file)
            voice = VOICES.get(target_lang, ["en-US-JennyNeural"])[0]
            thread = threading.Thread(target=run_async_in_thread, args=(text_to_speech, translated, audio_out, voice))
            thread.start(); thread.join()
            
            result = {'success':True, 'title':title, 'original_lang':lang, 'translated_text':translated}
            processing_status[process_id] = {'status':'completed','percent':100,'result':result}
            
        elif option == 'transcript':
            audio_path, title = download_audio_only(url)
            text, lang = transcribe_audio(audio_path)
            translated = translate_text(text, target_lang)
            result = {'success':True, 'title':title, 'original_lang':lang, 'transcribed_text':text, 'translated_text':translated}
            processing_status[process_id] = {'status':'completed','percent':100,'result':result}
            
        else:
            audio_path, title = download_audio_only(url)
            text, lang = transcribe_audio(audio_path)
            result = {'success':True, 'title':title, 'original_lang':lang, 'transcribed_text':text}
            processing_status[process_id] = {'status':'completed','percent':100,'result':result}
            
    except Exception as e:
        processing_status[process_id] = {'status':'error','error':str(e)}
    
    return jsonify({'process_id':process_id})

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)