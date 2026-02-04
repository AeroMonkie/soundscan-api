"""
YouTube Copyright Song Detector
A web application that analyzes YouTube videos for copyrighted music.
"""

import os
import json
import hashlib
import hmac
import base64
import time
import tempfile
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests

app = Flask(__name__, static_folder='static')
CORS(app)

# Configuration - Users should set these environment variables
ACRCLOUD_HOST = os.environ.get('ACRCLOUD_HOST', 'identify-us-west-2.acrcloud.com')
ACRCLOUD_ACCESS_KEY = os.environ.get('ACRCLOUD_ACCESS_KEY', '')
ACRCLOUD_ACCESS_SECRET = os.environ.get('ACRCLOUD_ACCESS_SECRET', '')

# Alternative: AudD API
AUDD_API_TOKEN = os.environ.get('AUDD_API_TOKEN', '')

# Chunk configuration
CHUNK_DURATION = 15  # seconds per chunk
OVERLAP = 5  # seconds overlap between chunks for better detection


def download_youtube_audio(url: str, output_dir: str) -> str:
    """Download audio from YouTube video using yt-dlp."""
    output_path = os.path.join(output_dir, 'audio.mp3')
    
    cmd = [
        'yt-dlp',
        '-x',  # Extract audio
        '--audio-format', 'mp3',
        '--audio-quality', '192K',
        '-o', output_path,
        '--no-playlist',
        '--max-filesize', '100M',
        url
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    
    if result.returncode != 0:
        raise Exception(f"Failed to download audio: {result.stderr}")
    
    # yt-dlp might add extension, find the actual file
    for f in os.listdir(output_dir):
        if f.startswith('audio') and f.endswith('.mp3'):
            return os.path.join(output_dir, f)
    
    raise Exception("Audio file not found after download")


def get_audio_duration(audio_path: str) -> float:
    """Get duration of audio file using ffprobe."""
    cmd = [
        'ffprobe',
        '-v', 'quiet',
        '-show_entries', 'format=duration',
        '-of', 'json',
        audio_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    return float(data['format']['duration'])


def extract_audio_chunk(audio_path: str, start_time: float, duration: float, output_path: str):
    """Extract a chunk of audio using ffmpeg."""
    cmd = [
        'ffmpeg',
        '-y',
        '-i', audio_path,
        '-ss', str(start_time),
        '-t', str(duration),
        '-acodec', 'libmp3lame',
        '-ar', '44100',
        '-ac', '1',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Exception(f"Failed to extract chunk: {result.stderr}")


def recognize_with_acrcloud(audio_path: str) -> dict:
    """Recognize music using ACRCloud API."""
    if not ACRCLOUD_ACCESS_KEY or not ACRCLOUD_ACCESS_SECRET:
        return {'error': 'ACRCloud credentials not configured'}
    
    http_method = "POST"
    http_uri = "/v1/identify"
    data_type = "audio"
    signature_version = "1"
    timestamp = str(int(time.time()))
    
    string_to_sign = f"{http_method}\n{http_uri}\n{ACRCLOUD_ACCESS_KEY}\n{data_type}\n{signature_version}\n{timestamp}"
    
    sign = base64.b64encode(
        hmac.new(
            ACRCLOUD_ACCESS_SECRET.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha1
        ).digest()
    ).decode('utf-8')
    
    with open(audio_path, 'rb') as f:
        sample = f.read()
    
    files = {'sample': ('audio.mp3', sample, 'audio/mpeg')}
    data = {
        'access_key': ACRCLOUD_ACCESS_KEY,
        'sample_bytes': len(sample),
        'timestamp': timestamp,
        'signature': sign,
        'data_type': data_type,
        'signature_version': signature_version
    }
    
    response = requests.post(
        f"https://{ACRCLOUD_HOST}{http_uri}",
        files=files,
        data=data,
        timeout=30
    )
    
    return response.json()


def recognize_with_audd(audio_path: str) -> dict:
    """Recognize music using AudD API."""
    if not AUDD_API_TOKEN:
        return {'error': 'AudD API token not configured'}
    
    with open(audio_path, 'rb') as f:
        data = {
            'api_token': AUDD_API_TOKEN,
            'return': 'timecode,spotify'
        }
        files = {'file': f}
        
        response = requests.post(
            'https://api.audd.io/',
            data=data,
            files=files,
            timeout=30
        )
    
    return response.json()


def parse_acrcloud_result(result: dict) -> dict:
    """Parse ACRCloud API response."""
    if result.get('status', {}).get('code') != 0:
        return None
    
    metadata = result.get('metadata', {})
    music = metadata.get('music', [])
    
    if not music:
        return None
    
    track = music[0]
    return {
        'title': track.get('title', 'Unknown'),
        'artists': [a.get('name', 'Unknown') for a in track.get('artists', [])],
        'album': track.get('album', {}).get('name', 'Unknown'),
        'release_date': track.get('release_date', 'Unknown'),
        'label': track.get('label', 'Unknown'),
        'duration': track.get('duration_ms', 0) // 1000,
        'confidence': track.get('score', 0),
        'external_ids': track.get('external_ids', {}),
        'external_metadata': track.get('external_metadata', {})
    }


def parse_audd_result(result: dict) -> dict:
    """Parse AudD API response."""
    if result.get('status') != 'success' or not result.get('result'):
        return None
    
    track = result['result']
    return {
        'title': track.get('title', 'Unknown'),
        'artists': [track.get('artist', 'Unknown')],
        'album': track.get('album', 'Unknown'),
        'release_date': track.get('release_date', 'Unknown'),
        'label': track.get('label', 'Unknown'),
        'duration': 0,
        'confidence': 100,  # AudD doesn't provide confidence
        'spotify': track.get('spotify', {}),
        'timecode': track.get('timecode', '')
    }


def format_timestamp(seconds: float) -> str:
    """Format seconds to MM:SS or HH:MM:SS."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def analyze_video(youtube_url: str, use_audd: bool = False) -> dict:
    """Main function to analyze a YouTube video for copyrighted music."""
    results = {
        'url': youtube_url,
        'songs': [],
        'analysis_chunks': 0,
        'video_duration': 0,
        'errors': []
    }
    
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            # Download audio
            audio_path = download_youtube_audio(youtube_url, temp_dir)
            
            # Get duration
            duration = get_audio_duration(audio_path)
            results['video_duration'] = duration
            results['video_duration_formatted'] = format_timestamp(duration)
            
            # Calculate chunks
            chunks = []
            current_time = 0
            while current_time < duration:
                chunk_end = min(current_time + CHUNK_DURATION, duration)
                chunks.append((current_time, chunk_end - current_time))
                current_time += CHUNK_DURATION - OVERLAP
            
            results['analysis_chunks'] = len(chunks)
            
            # Analyze each chunk
            detected_songs = {}  # Track unique songs and their timestamps
            
            for i, (start_time, chunk_duration) in enumerate(chunks):
                chunk_path = os.path.join(temp_dir, f'chunk_{i}.mp3')
                
                try:
                    extract_audio_chunk(audio_path, start_time, chunk_duration, chunk_path)
                    
                    if use_audd:
                        result = recognize_with_audd(chunk_path)
                        parsed = parse_audd_result(result)
                    else:
                        result = recognize_with_acrcloud(chunk_path)
                        parsed = parse_acrcloud_result(result)
                    
                    if parsed:
                        song_key = f"{parsed['title']}|{'|'.join(parsed['artists'])}"
                        
                        if song_key not in detected_songs:
                            detected_songs[song_key] = {
                                **parsed,
                                'timestamps': [],
                                'time_ranges': []
                            }
                        
                        detected_songs[song_key]['timestamps'].append(start_time)
                        detected_songs[song_key]['time_ranges'].append({
                            'start': format_timestamp(start_time),
                            'end': format_timestamp(min(start_time + chunk_duration, duration)),
                            'start_seconds': start_time,
                            'end_seconds': min(start_time + chunk_duration, duration)
                        })
                
                except Exception as e:
                    results['errors'].append(f"Chunk {i} ({format_timestamp(start_time)}): {str(e)}")
            
            # Merge consecutive time ranges for each song
            for song_key, song_data in detected_songs.items():
                merged_ranges = []
                ranges = sorted(song_data['time_ranges'], key=lambda x: x['start_seconds'])
                
                for r in ranges:
                    if merged_ranges and r['start_seconds'] <= merged_ranges[-1]['end_seconds'] + OVERLAP:
                        # Merge with previous range
                        merged_ranges[-1]['end_seconds'] = max(merged_ranges[-1]['end_seconds'], r['end_seconds'])
                        merged_ranges[-1]['end'] = format_timestamp(merged_ranges[-1]['end_seconds'])
                    else:
                        merged_ranges.append(r.copy())
                
                song_data['time_ranges'] = merged_ranges
                results['songs'].append(song_data)
            
            # Sort songs by first appearance
            results['songs'].sort(key=lambda x: x['time_ranges'][0]['start_seconds'] if x['time_ranges'] else 0)
            
        except Exception as e:
            results['errors'].append(str(e))
    
    return results


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """API endpoint to analyze a YouTube video."""
    data = request.get_json()
    
    if not data or 'url' not in data:
        return jsonify({'error': 'No URL provided'}), 400
    
    url = data['url']
    use_audd = data.get('use_audd', False)
    
    # Validate URL
    if 'youtube.com' not in url and 'youtu.be' not in url:
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    try:
        results = analyze_video(url, use_audd)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['GET'])
def get_config():
    """Check which APIs are configured."""
    return jsonify({
        'acrcloud_configured': bool(ACRCLOUD_ACCESS_KEY and ACRCLOUD_ACCESS_SECRET),
        'audd_configured': bool(AUDD_API_TOKEN)
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
