from flask import Flask, request, jsonify, render_template, send_file
import anthropic
import requests
import os
import json
import subprocess
import uuid
from pathlib import Path

app = Flask(__name__)

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# ─── Step 1: Analyze script with Claude ───────────────────────────────────────
def analyze_script(script_text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are a professional video editor AI. Analyze this YouTube script and return ONLY a JSON object (no markdown, no backticks).

Script:
{script_text}

Return this exact JSON structure:
{{
  "scenes": [
    {{
      "num": 1,
      "text": "<narration text for this scene>",
      "search_keyword": "<single english keyword for Pexels>",
      "duration_sec": <estimated seconds as integer>
    }}
  ],
  "music_mood": "<happy|sad|dramatic|calm|energetic>",
  "voice_style": "<neutral|excited|serious>"
}}

Rules:
- Split into 5-15 scenes based on topic shifts
- search_keyword must be simple English (1-2 words max)
- duration_sec between 3 and 8 seconds per scene
- text is the narration for that scene only"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

# ─── Step 2: Download video clips from Pexels ─────────────────────────────────
def download_pexels_clip(keyword, duration, output_path):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": keyword, "per_page": 5, "min_duration": duration}
    r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params)
    data = r.json()

    if not data.get("videos"):
        # fallback keyword
        params["query"] = "nature"
        r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params)
        data = r.json()

    video = data["videos"][0]
    # pick HD file
    video_files = sorted(video["video_files"], key=lambda x: x.get("width", 0), reverse=True)
    video_url = video_files[0]["link"]

    r = requests.get(video_url, stream=True)
    with open(output_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return output_path

# ─── Step 3: Generate voiceover with ElevenLabs ───────────────────────────────
def generate_voice(text, output_path):
    voice_id = "21m00Tcm4TlvDq8ikWAM"  # Rachel - clear neutral voice
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    body = {
        "text": text,
        "model_id": "eleven_monolingual_v1",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }
    r = requests.post(url, headers=headers, json=body)
    with open(output_path, "wb") as f:
        f.write(r.content)
    return output_path

# ─── Step 4: Trim clip to duration ────────────────────────────────────────────
def trim_clip(input_path, duration, output_path):
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-t", str(duration),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-an", str(output_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path

# ─── Step 5: Merge everything into final video ────────────────────────────────
def merge_video(session_id, scenes_data, voice_paths, clip_paths):
    # Build concat list
    concat_file = TEMP_DIR / f"{session_id}_concat.txt"
    trimmed_paths = []

    for i, (scene, clip_path) in enumerate(zip(scenes_data, clip_paths)):
        trimmed = TEMP_DIR / f"{session_id}_trimmed_{i}.mp4"
        trim_clip(clip_path, scene["duration_sec"], trimmed)
        trimmed_paths.append(trimmed)

    with open(concat_file, "w") as f:
        for p in trimmed_paths:
            f.write(f"file '{p.absolute()}'\n")

    # Concat video clips
    concat_output = TEMP_DIR / f"{session_id}_concat.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file), "-c", "copy", str(concat_output)
    ], check=True, capture_output=True)

    # Merge all voice files
    voice_concat_file = TEMP_DIR / f"{session_id}_voice_concat.txt"
    with open(voice_concat_file, "w") as f:
        for p in voice_paths:
            f.write(f"file '{Path(p).absolute()}'\n")

    voice_merged = TEMP_DIR / f"{session_id}_voice.mp3"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(voice_concat_file), "-c", "copy", str(voice_merged)
    ], check=True, capture_output=True)

    # Final merge: video + voice
    final_output = OUTPUT_DIR / f"{session_id}_final.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(concat_output),
        "-i", str(voice_merged),
        "-c:v", "copy", "-c:a", "aac",
        "-shortest", str(final_output)
    ], check=True, capture_output=True)

    return final_output

# ─── Main route ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate():
    try:
        data = request.json
        script = data.get("script", "").strip()
        if not script:
            return jsonify({"error": "السكريبت فاضي"}), 400

        session_id = str(uuid.uuid4())[:8]

        # Step 1: Analyze
        analysis = analyze_script(script)
        scenes = analysis["scenes"]

        voice_paths = []
        clip_paths = []

        for i, scene in enumerate(scenes):
            # Step 2: Download clip
            clip_path = TEMP_DIR / f"{session_id}_clip_{i}.mp4"
            download_pexels_clip(scene["search_keyword"], scene["duration_sec"], clip_path)
            clip_paths.append(clip_path)

            # Step 3: Generate voice
            voice_path = TEMP_DIR / f"{session_id}_voice_{i}.mp3"
            generate_voice(scene["text"], voice_path)
            voice_paths.append(voice_path)

        # Step 4+5: Merge
        final_video = merge_video(session_id, scenes, voice_paths, clip_paths)

        return jsonify({
            "success": True,
            "video_id": session_id,
            "scenes_count": len(scenes),
            "download_url": f"/download/{session_id}"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/download/<session_id>")
def download(session_id):
    path = OUTPUT_DIR / f"{session_id}_final.mp4"
    if path.exists():
        return send_file(path, as_attachment=True, download_name="scriptcut_video.mp4")
    return jsonify({"error": "الفيديو مش موجود"}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
