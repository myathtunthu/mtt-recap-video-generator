import os
import asyncio
import threading
import time
import requests
from flask import Flask, request, jsonify, send_file
import edge_tts
import yt_dlp
from faster_whisper import WhisperModel
import subprocess
import cv2
import numpy as np

app = Flask(__name__)

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
    """Video နဲ့ Audio ကိုပေါင်းမယ် (FFmpeg တစ်ခုတည်းနဲ့)"""
    try:
        # FFmpeg filters
        filters = []
        if mirror:
            filters.append('hflip')
        if color_adjust:
            filters.append('eq=brightness=0.05:contrast=1.2:saturation=1.2')
        
        filter_str = ','.join(filters) if filters else 'null'
        
        cmd = [
            'ffmpeg', '-i', video_path, '-i', audio_path,
            '-filter_complex', f'[0:v]{filter_str}[v]',
            '-map', '[v]', '-map', '1:a:0',
            '-c:v', 'libx264', '-preset', 'fast',
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest', '-y', output_path
        ]
        
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"FFmpeg completed")
        return True
    except Exception as e:
        print(f"Video creation error: {e}")
        return False

@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>AI Video & Audio Generator</title>
        <style>
            body { 
                font-family: 'Pyidaungsu', Arial, sans-serif; 
                max-width: 800px; 
                margin: 50px auto; 
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            }
            .container {
                background: white;
                padding: 30px;
                border-radius: 15px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            }
            h1 { text-align: center; color: #333; }
            input[type=text], select { 
                width: 100%; 
                padding: 15px; 
                margin: 10px 0; 
                border: 2px solid #ddd;
                border-radius: 8px;
                font-size: 16px;
            }
            .option-group {
                display: flex;
                gap: 10px;
                margin: 20px 0;
                flex-wrap: wrap;
            }
            .option-card {
                flex: 1;
                min-width: 120px;
                padding: 15px;
                border: 2px solid #ddd;
                border-radius: 8px;
                cursor: pointer;
                text-align: center;
            }
            .option-card.selected {
                border-color: #4CAF50;
                background: #e8f5e9;
            }
            .checkbox-group {
                margin: 20px 0;
                padding: 15px;
                background: #f5f5f5;
                border-radius: 8px;
            }
            button { 
                padding: 15px; 
                background: #4CAF50; 
                color: white; 
                border: none; 
                border-radius: 8px;
                cursor: pointer; 
                font-size: 18px;
                width: 100%;
            }
            .result-box { 
                margin-top: 20px; 
                padding: 20px;
                border-radius: 8px;
                background: #f5f5f5;
                max-height: 400px;
                overflow-y: auto;
            }
            .download-btn {
                display: inline-block;
                padding: 10px 20px;
                background: #2196F3;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                margin: 5px;
            }
            video, audio { width: 100%; margin-top: 20px; }
            .loading { display: none; text-align: center; margin: 20px 0; }
            .spinner {
                border: 5px solid #f3f3f3;
                border-top: 5px solid #3498db;
                border-radius: 50%;
                width: 50px;
                height: 50px;
                animation: spin 1s linear infinite;
                margin: 20px auto;
            }
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 AI Video & Audio Generator</h1>
            <p>YouTube, TikTok, Facebook Video လင့်ခ် ထည့်ပါ</p>
            
            <input type="text" id="url" placeholder="Video URL">
            
            <div class="option-group">
                <div class="option-card" onclick="selectOption('transcript_only')" id="opt-transcript_only">
                    <h3>📝 စာသားချည်းထုတ်</h3>
                </div>
                <div class="option-card" onclick="selectOption('transcript')" id="opt-transcript">
                    <h3>📝 စာသား+ဘာသာပြန်</h3>
                </div>
                <div class="option-card" onclick="selectOption('audio')" id="opt-audio">
                    <h3>🎵 အသံချည်းထုတ်</h3>
                </div>
                <div class="option-card" onclick="selectOption('video')" id="opt-video">
                    <h3>🎬 Video အပြည့်ထုတ်</h3>
                </div>
            </div>
            
            <div id="lang-select" style="display: none;">
                <select id="target_lang">
                    <option value="my">မြန်မာ</option>
                    <option value="en">အင်္ဂလိပ်</option>
                    <option value="th">ထိုင်း</option>
                    <option value="zh">တရုတ်</option>
                    <option value="ja">ဂျပန်</option>
                </select>
            </div>
            
            <div id="video-options" style="display: none;" class="checkbox-group">
                <label><input type="checkbox" id="mirror" checked> Mirror (ဘယ်/ညာပြောင်း)</label><br>
                <label><input type="checkbox" id="color" checked> Color Adjustment</label>
            </div>
            
            <button onclick="processVideo()">စတင်မည်</button>
            
            <div class="loading" id="loading">
                <p>လုပ်ဆောင်နေပါသည်... (၂-၅ မိနစ်ခန့်ကြာနိုင်ပါသည်)</p>
                <div class="spinner"></div>
            </div>
            
            <div id="result" class="result-box" style="display: none;"></div>
            <audio id="audioPlayer" controls style="display: none;"></audio>
            <video id="videoPlayer" controls style="display: none;"></video>
        </div>

        <script>
            let selectedOption = 'transcript_only';
            
            function selectOption(option) {
                selectedOption = option;
                document.querySelectorAll('.option-card').forEach(el => {
                    el.classList.remove('selected');
                });
                document.getElementById(`opt-${option}`).classList.add('selected');
                
                if (option === 'video') {
                    document.getElementById('lang-select').style.display = 'block';
                    document.getElementById('video-options').style.display = 'block';
                } else if (option === 'audio') {
                    document.getElementById('lang-select').style.display = 'block';
                    document.getElementById('video-options').style.display = 'none';
                } else {
                    document.getElementById('lang-select').style.display = 'none';
                    document.getElementById('video-options').style.display = 'none';
                }
            }
            
            async function processVideo() {
                const url = document.getElementById('url').value;
                if (!url) { alert('URL ထည့်ပါ'); return; }
                
                document.getElementById('loading').style.display = 'block';
                
                const response = await fetch('/process', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        url: url,
                        target_lang: document.getElementById('target_lang').value,
                        option: selectedOption,
                        mirror: document.getElementById('mirror')?.checked || false,
                        color: document.getElementById('color')?.checked || false
                    })
                });
                
                const data = await response.json();
                document.getElementById('loading').style.display = 'none';
                
                if (data.error) {
                    document.getElementById('result').innerHTML = '❌ ' + data.error;
                    document.getElementById('result').style.display = 'block';
                    return;
                }
                
                let html = `<strong>✅ ${data.title}</strong><br>`;
                html += `<strong>🗣️ မူရင်း:</strong> ${data.original_lang}<br><hr>`;
                
                if (selectedOption === 'video') {
                    html += `<strong>🎬 ဘာသာပြန်:</strong><br>${data.translated_text}<br><br>`;
                    html += `<a href="/download/${data.video_file}" class="download-btn" target="_blank">🎬 ကြည့်ရန်</a> `;
                    html += `<a href="/download/${data.video_file}" class="download-btn" download>📥 Download</a>`;
                } else if (selectedOption === 'audio') {
                    html += `<strong>🎵 ဘာသာပြန်:</strong><br>${data.translated_text}<br><br>`;
                    html += `<a href="/download/${data.audio_file}" class="download-btn" target="_blank">🎵 နားထောင်ရန်</a> `;
                    html += `<a href="/download/${data.audio_file}" class="download-btn" download>📥 Download</a>`;
                } else if (selectedOption === 'transcript') {
                    html += `<strong>📝 မူရင်း:</strong><br>${data.transcribed_text}<br><br>`;
                    html += `<strong>🔄 ဘာသာပြန်:</strong><br>${data.translated_text}`;
                } else {
                    html += `<strong>📝 မူရင်းစာသား:</strong><br>${data.transcribed_text}`;
                }
                
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
    target_lang = data.get('target_lang', 'my')
    option = data.get('option')
    mirror = data.get('mirror', True)
    color = data.get('color', True)
    
    try:
        if option == 'video':
            video_path, title = download_video(url)
            audio_path = video_path.replace('.mp4', '.mp3')
            
            # Extract audio
            subprocess.run(['ffmpeg', '-i', video_path, '-q:a', '0', '-map', 'a', audio_path, '-y'], 
                         check=True, capture_output=True)
            
            transcribed_text, detected_lang = transcribe_audio(audio_path)
            translated_text = translate_text(transcribed_text, target_lang)
            
            # Create new audio
            audio_filename = f"audio_{int(time.time())}.mp3"
            audio_output = os.path.join(UPLOAD_FOLDER, audio_filename)
            voice = VOICES.get(target_lang, ["en-US-JennyNeural"])[0]
            
            thread = threading.Thread(target=run_async_in_thread, 
                                     args=(text_to_speech, translated_text, audio_output, voice))
            thread.start()
            thread.join()
            
            # Create video
            video_filename = f"video_{int(time.time())}.mp4"
            video_output = os.path.join(UPLOAD_FOLDER, video_filename)
            
            create_video_with_audio(video_path, audio_output, video_output, mirror, color)
            
            return jsonify({
                'success': True, 'title': title, 'original_lang': detected_lang,
                'translated_text': translated_text, 'video_file': video_filename
            })
            
        elif option == 'audio':
            audio_path, title = download_audio_only(url)
            transcribed_text, detected_lang = transcribe_audio(audio_path)
            translated_text = translate_text(transcribed_text, target_lang)
            
            audio_filename = f"audio_{int(time.time())}.mp3"
            audio_output = os.path.join(UPLOAD_FOLDER, audio_filename)
            voice = VOICES.get(target_lang, ["en-US-JennyNeural"])[0]
            
            thread = threading.Thread(target=run_async_in_thread, 
                                     args=(text_to_speech, translated_text, audio_output, voice))
            thread.start()
            thread.join()
            
            return jsonify({
                'success': True, 'title': title, 'original_lang': detected_lang,
                'translated_text': translated_text, 'audio_file': audio_filename
            })
            
        elif option == 'transcript':
            audio_path, title = download_audio_only(url)
            transcribed_text, detected_lang = transcribe_audio(audio_path)
            translated_text = translate_text(transcribed_text, target_lang)
            return jsonify({
                'success': True, 'title': title, 'original_lang': detected_lang,
                'transcribed_text': transcribed_text, 'translated_text': translated_text
            })
            
        else:  # transcript_only
            audio_path, title = download_audio_only(url)
            transcribed_text, detected_lang = transcribe_audio(audio_path)
            return jsonify({
                'success': True, 'title': title, 'original_lang': detected_lang,
                'transcribed_text': transcribed_text
            })
            
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/download/<filename>')
def download_file(filename):
    return send_file(os.path.join(UPLOAD_FOLDER, filename))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)