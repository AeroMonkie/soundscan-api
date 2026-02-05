"""
SoundScan - Audio Copyright Song Detector
Accepts audio file uploads and detects copyrighted music with timestamps.
"""

import os
import json
import tempfile
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# Configuration
AUDD_API_TOKEN = os.environ.get('AUDD_API_TOKEN', '')

# Chunk configuration
CHUNK_DURATION = 12  # seconds per chunk
OVERLAP = 4  # seconds overlap between chunks


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
    if result.returncode != 0:
        raise Exception(f"ffprobe failed: {result.stderr}")
    
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
        '-b:a', '128k',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise Exception(f"ffmpeg failed: {result.stderr}")


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
        'confidence': 100,
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


def analyze_audio_file(audio_path: str, max_duration: float = None) -> dict:
    """Main function to analyze an audio file for copyrighted music."""
    results = {
        'songs': [],
        'analysis_chunks': 0,
        'audio_duration': 0,
        'errors': []
    }
    
    temp_dir = tempfile.mkdtemp()
    
    try:
        # Get duration
        duration = get_audio_duration(audio_path)
        results['audio_duration'] = duration
        results['audio_duration_formatted'] = format_timestamp(duration)
        
        # Apply max_duration limit if set
        analyze_duration = duration
        if max_duration and max_duration < duration:
            analyze_duration = max_duration
            results['scan_mode'] = f'First {format_timestamp(max_duration)}'
        else:
            results['scan_mode'] = 'Full audio'
        
        # Calculate chunks
        chunks = []
        current_time = 0
        while current_time < analyze_duration:
            chunk_end = min(current_time + CHUNK_DURATION, analyze_duration)
            chunks.append((current_time, chunk_end - current_time))
            current_time += CHUNK_DURATION - OVERLAP
        
        results['analysis_chunks'] = len(chunks)
        
        # Analyze each chunk
        detected_songs = {}
        
        for i, (start_time, chunk_duration) in enumerate(chunks):
            chunk_path = os.path.join(temp_dir, f'chunk_{i}.mp3')
            
            try:
                extract_audio_chunk(audio_path, start_time, chunk_duration, chunk_path)
                
                result = recognize_with_audd(chunk_path)
                parsed = parse_audd_result(result)
                
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
                
                # Clean up chunk file
                if os.path.exists(chunk_path):
                    os.remove(chunk_path)
                    
            except Exception as e:
                results['errors'].append(f"Chunk {i} ({format_timestamp(start_time)}): {str(e)}")
        
        # Merge consecutive time ranges for each song
        # Use a 30-second gap tolerance — if the same song is detected
        # again within 30 seconds of the last detection, treat it as
        # one continuous occurrence (covers speech breaks, quiet moments, etc.)
        MERGE_GAP = 30  # seconds
        
        for song_key, song_data in detected_songs.items():
            merged_ranges = []
            ranges = sorted(song_data['time_ranges'], key=lambda x: x['start_seconds'])
            
            for r in ranges:
                if merged_ranges and r['start_seconds'] <= merged_ranges[-1]['end_seconds'] + MERGE_GAP:
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
    
    finally:
        # Clean up temp directory
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    return results


@app.route('/')
def index():
    return jsonify({'status': 'SoundScan API is running', 'version': '2.0'})


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """API endpoint to analyze an uploaded audio file."""
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Check file extension
    allowed_extensions = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.aac', '.wma', '.mp4', '.webm'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        return jsonify({'error': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'}), 400
    
    # Save uploaded file temporarily
    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, f'upload{file_ext}')
    
    try:
        file.save(temp_path)
        
        # Check for max_duration parameter (in seconds)
        max_duration = request.form.get('max_duration', None)
        if max_duration:
            max_duration = float(max_duration)
        
        results = analyze_audio_file(temp_path, max_duration=max_duration)
        results['filename'] = file.filename
        return jsonify(results)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
    finally:
        # Clean up
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route('/api/config', methods=['GET'])
def get_config():
    """Check which APIs are configured."""
    return jsonify({
        'audd_configured': bool(AUDD_API_TOKEN),
        'max_file_size': '50MB',
        'supported_formats': ['mp3', 'wav', 'm4a', 'ogg', 'flac', 'aac', 'wma', 'mp4', 'webm']
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
