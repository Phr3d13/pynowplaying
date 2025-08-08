import os
import sys
import time
import json
import threading
import subprocess
import platform
import glob
from datetime import datetime
from io import BytesIO
import asyncio

import sounddevice as sd
import numpy as np
from pydub import AudioSegment
from pydub.utils import which
import requests
from flask import Flask, render_template_string, jsonify

# Try to import shazamio for better track identification
try:
    from shazamio import Shazam
    SHAZAMIO_AVAILABLE = True
    print("DEBUG: shazamio available for track identification")
except ImportError:
    SHAZAMIO_AVAILABLE = False
    print("DEBUG: shazamio not available - install with: pip install shazamio")
    print("DEBUG: Falling back to AcoustID only")

# Configure pydub to use local ffmpeg.exe
if os.path.exists('ffmpeg.exe'):
    AudioSegment.converter = os.path.abspath('ffmpeg.exe')
    AudioSegment.ffmpeg = os.path.abspath('ffmpeg.exe')
    AudioSegment.ffprobe = os.path.abspath('ffmpeg.exe')  # ffmpeg.exe often includes ffprobe functionality
    print("DEBUG: Using local ffmpeg.exe for audio processing")
else:
    print("DEBUG: Warning - ffmpeg.exe not found in current directory")

# === CONFIG ===
ACOUSTID_API_KEY = 'YOUR_ACOUSTID_API_KEY_HERE'  # Get your free key from https://acoustid.org/
DEVICE_NAME_CONTAINS = "Analogue 3 + 4"  # substring of your input device name, adjust as needed
CHUNK_SECONDS = 15                   # length of each audio chunk for track identification (faster testing)
SAMPLE_RATE = 48000                # sample rate (Hz) - changed to 48kHz
CHANNELS = 2                       # stereo

# Platform-specific configuration
CURRENT_PLATFORM = platform.system()
if CURRENT_PLATFORM == "Windows":
    FP_CALC_PATH = 'fpcalc.exe'    # Windows executable
elif CURRENT_PLATFORM == "Darwin":  # macOS
    FP_CALC_PATH = 'fpcalc'        # macOS/Linux executable
else:  # Linux and others
    FP_CALC_PATH = 'fpcalc'        # Linux executable

DEBUG_SAVE_AUDIO = False            # Save audio samples for debugging

def cleanup_temp_files():
    """Remove any leftover temporary files from previous runs"""
    temp_patterns = [
        "temp_*.wav",
        "debug_audio_*.wav", 
        "temp_fpcalc.wav",
        "temp_shazam_*.wav",
        "temp_audd_*.wav",
        "temp_acoustid_*.wav"
    ]
    
    removed_count = 0
    for pattern in temp_patterns:
        try:
            for file_path in glob.glob(pattern):
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"DEBUG: Removed leftover temp file: {file_path}")
                    removed_count += 1
        except Exception as e:
            print(f"DEBUG: Could not remove temp file {pattern}: {e}")
    
    if removed_count > 0:
        print(f"DEBUG: Cleaned up {removed_count} temporary files from previous runs")
    
    return removed_count

def cleanup_on_exit():
    """Cleanup function to run when the program exits"""
    print("DEBUG: Performing final cleanup...")
    removed = cleanup_temp_files()
    if removed > 0:
        print(f"DEBUG: Final cleanup removed {removed} temporary files")

# Register cleanup function to run on program exit
import atexit
atexit.register(cleanup_on_exit)

# Track identification service selection
USE_AUDD_API = True                # Use AudD API (free tier expires after ~2 weeks)
USE_SHAZAM_API = True              # Use Shazam-like identification (requires shazamio - may fail to install)
USE_ACOUSTID_FALLBACK = True       # AcoustID for full songs - RELIABLE LONG TERM SOLUTION (no complex deps)
USE_MUSICBRAINZ_DIRECT = True      # Query MusicBrainz directly for enhanced metadata

# AudD API configuration (free trial expires after ~2 weeks)
AUDD_API_TOKEN = "fd225011ab1d3beec55ff2729a6a7ffe"            # Replace with your free API token from https://audd.io/

# Shazam API configuration (free tier available)
RAPIDAPI_KEY = "YOUR_RAPIDAPI_KEY_HERE"  # Get from RapidAPI for Shazam service

# === GLOBAL STATE ===
current_track = {"artist": "", "title": "", "time": "", "album_art": "", "album": ""}
track_history = []
last_identified_track = None  # Track the last identified song for consecutive match detection
consecutive_match_count = 0   # Count consecutive matches of the same song

# Flask app to serve webpage
app = Flask(__name__)

# Simple HTML template with auto-refresh
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>Now Playing</title>
    <style>
        body { 
            background: #111; 
            color: #eee; 
            font-family: Arial, sans-serif; 
            padding: 20px; 
            margin: 0;
        }
        h1 { 
            font-size: 2em; 
            margin-top: 0;
        }
        #current { 
            margin-bottom: 30px; 
            display: flex; 
            align-items: flex-start; 
            gap: 20px; 
            flex-wrap: nowrap;
        }
        #album-art { 
            width: 150px; 
            height: 150px; 
            border-radius: 8px; 
            background: #333; 
            display: flex; 
            align-items: center; 
            justify-content: center; 
            flex-shrink: 0;
            overflow: hidden;
            position: relative;
        }
        #album-art img { 
            width: 100%; 
            height: 100%; 
            object-fit: cover; 
            border-radius: 8px; 
            display: block;
        }
        #album-art .no-art {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            display: none;
        }
        #album-art.has-error .no-art {
            display: block;
        }
        #album-art.has-error img {
            display: none;
        }
        #track-info { 
            flex: 1; 
            min-width: 0;
            padding-left: 10px;
        }
        #track-info h1 {
            margin-bottom: 10px;
            line-height: 1.2;
        }
        #track-info p {
            margin: 8px 0;
            line-height: 1.4;
        }
        #history { 
            font-size: 0.9em; 
            max-height: 300px; 
            overflow-y: auto; 
        }
        .track { 
            margin-bottom: 8px; 
            display: flex; 
            align-items: center; 
            gap: 12px; 
        }
        .track-art { 
            width: 30px; 
            height: 30px; 
            border-radius: 4px; 
            background: #333; 
            flex-shrink: 0; 
            overflow: hidden;
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .track-art img { 
            width: 100%; 
            height: 100%; 
            object-fit: cover; 
            border-radius: 4px; 
            display: block;
        }
        .track-art .no-art {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            display: none;
        }
        .track-art.has-error .no-art {
            display: block;
        }
        .track-art.has-error img {
            display: none;
        }
        .track-details { 
            flex: 1; 
            min-width: 0;
        }
        .timestamp { 
            color: #666; 
            margin-right: 10px; 
        }
        .no-art { 
            color: #666; 
            font-size: 0.8em; 
            text-align: center;
        }
    </style>
    <meta http-equiv="refresh" content="5" />
</head>
<body>
    <div id="current">
        <div id="album-art">
            {% if track.album_art %}
                <img src="{{ track.album_art }}" alt="Album Art" onerror="this.parentElement.classList.add('has-error');">
                <span class="no-art">🎵</span>
            {% else %}
                <span class="no-art">🎵</span>
            {% endif %}
        </div>
        <div id="track-info">
            <h1>Now Playing:</h1>
            {% if track.artist and track.title %}
              <p><strong>{{ track.artist }} - {{ track.title }}</strong></p>
              {% if track.album and track.album != 'Unknown Album' %}
                <p><em>Album: {{ track.album }}</em></p>
              {% else %}
                <p><em>Started at {{ track.time }}</em></p>
              {% endif %}
            {% else %}
              <p><em>No track detected yet</em></p>
            {% endif %}
        </div>
    </div>
    <div id="history">
        <h2>History</h2>
        {% for t in history %}
            <div class="track">
                <div class="track-art">
                    {% if t.album_art %}
                        <img src="{{ t.album_art }}" alt="Album Art" onerror="this.parentElement.classList.add('has-error');">
                        <span class="no-art">♪</span>
                    {% else %}
                        <span class="no-art">♪</span>
                    {% endif %}
                </div>
                <div class="track-details">
                    <span class="timestamp">{{ t.time }}</span>
                    <span>{{ t.artist }} - {{ t.title }}</span>
                </div>
            </div>
        {% endfor %}
    </div>
</body>
</html>
"""

def get_default_input_device():
    """Get the system default audio input device (cross-platform)"""
    try:
        # sounddevice uses the default device when device=None
        # We can get the default device info
        default_device = sd.query_devices(kind='input')
        if default_device:
            platform_name = "Windows" if CURRENT_PLATFORM == "Windows" else CURRENT_PLATFORM
            print(f"DEBUG: {platform_name} default input device: {default_device['name']}")
            return default_device
        return None
    except Exception as e:
        print(f"DEBUG: Could not get default device: {e}")
        return None

def find_default_device_index():
    """Find the index of the system default input device (cross-platform)"""
    try:
        default_device_info = get_default_input_device()
        if not default_device_info:
            return None
            
        devices = sd.query_devices()
        default_name = default_device_info['name']
        
        # Try to find matching device by name
        for i, dev in enumerate(devices):
            if (dev['max_input_channels'] > 0 and 
                dev['name'] == default_name):
                print(f"DEBUG: Found default device at index [{i}]: {dev['name']}")
                return i
        
        # If exact match not found, try partial match
        for i, dev in enumerate(devices):
            if (dev['max_input_channels'] > 0 and 
                default_name.lower() in dev['name'].lower()):
                print(f"DEBUG: Found similar default device at index [{i}]: {dev['name']}")
                return i
                
        return None
    except Exception as e:
        print(f"DEBUG: Error finding default device index: {e}")
        return None

def check_device_sample_rate(device_index, target_rate=48000):
    """Check if a device supports the target sample rate"""
    try:
        # Try to query the device with the target sample rate
        device_info = sd.query_devices(device_index)
        
        # Test if the device can handle the target sample rate
        try:
            sd.check_input_settings(device=device_index, samplerate=target_rate, channels=CHANNELS)
            
            # Try a quick test recording to make sure it actually works
            try:
                test_recording = sd.rec(int(0.1 * target_rate), samplerate=target_rate,
                                      channels=CHANNELS, dtype='float32', device=device_index)
                sd.wait()
                
                # Check if recording contains valid data (not NaN or inf)
                if np.any(np.isnan(test_recording)) or np.any(np.isinf(test_recording)):
                    return False
                    
                return True
            except Exception as e:
                print(f"      DEBUG: Test recording failed: {e}")
                return False
                
        except Exception as e:
            print(f"      DEBUG: Settings check failed: {e}")
            return False
    except Exception as e:
        print(f"      DEBUG: Device query failed: {e}")
        return False

def get_platform_audio_hints():
    """Get platform-specific audio device hints"""
    if CURRENT_PLATFORM == "Windows":
        return [
            "💡 HINT: For music detection on Windows, you typically want:",
            "   - 'Stereo Mix' or 'What U Hear' (captures what's playing)",
            "   - NOT a microphone (captures external sound)",
            "   - A loopback device from your audio interface"
        ]
    elif CURRENT_PLATFORM == "Linux":
        return [
            "💡 HINT: For music detection on Linux, you typically want:",
            "   - PulseAudio monitor device (captures system audio)",
            "   - ALSA loopback device (e.g., 'hw:Loopback,1,0')",
            "   - Jack monitor or loopback port",
            "   - NOT a microphone (captures external sound)"
        ]
    elif CURRENT_PLATFORM == "Darwin":  # macOS
        return [
            "💡 HINT: For music detection on macOS, you typically want:",
            "   - Aggregate device with built-in output as source",
            "   - Loopback software (like Loopback or SoundFlower)",
            "   - BlackHole virtual audio device",
            "   - NOT a microphone (captures external sound)"
        ]
    else:
        return [
            "💡 HINT: For music detection, you typically want:",
            "   - A device that captures system audio output",
            "   - NOT a microphone (captures external sound)",
            "   - Check your OS documentation for loopback/monitor devices"
        ]

def get_service_recommendations():
    """Get recommendations for sustainable, long-term free services"""
    recommendations = []
    
    if SHAZAMIO_AVAILABLE:
        recommendations.append("✅ Shazam (via shazamio) - Completely free forever")
    else:
        recommendations.append("❌ Shazam not available - may have Rust/Cargo installation issues")
        recommendations.append("💡 Alternative: Focus on AcoustID (simpler, reliable)")
    
    if ACOUSTID_API_KEY != 'YOUR_ACOUSTID_API_KEY_HERE':
        recommendations.append("✅ AcoustID - Free forever (requires 30+ second samples)")
    else:
        recommendations.append("⚠️  AcoustID - Get free API key from https://acoustid.org/")
    
    recommendations.append("⏰ AudD API - Free trial (~2 weeks), then requires payment")
    
    return recommendations

def print_service_status():
    """Print current service status and recommendations"""
    print("\n" + "="*60)
    print("SERVICE STATUS & SUSTAINABILITY")
    print("="*60)
    
    recommendations = get_service_recommendations()
    for rec in recommendations:
        print(f"  {rec}")
    
    print("\n💡 For long-term use:")
    if not SHAZAMIO_AVAILABLE:
        print("  🎯 SHAZAMIO INSTALLATION FAILED:")
        print("     - Skip shazamio for now (complex Rust dependencies)")
        print("     - Focus on AcoustID: https://acoustid.org/ (simple, reliable)")
        print("     - Use AudD while trial lasts")
    else:
        print("  1. Install shazamio: pip install shazamio")
        print("  2. Get AcoustID key: https://acoustid.org/ (free forever)")
        print("  3. Use AudD while trial lasts, then switch to Shazam+AcoustID")
    print("="*60)

def get_device_type_indicator(device_name):
    """Get a platform-aware device type indicator"""
    name_lower = device_name.lower()
    
    # Microphone detection (universal)
    if 'microphone' in name_lower or 'mic' in name_lower:
        return '🎤 MICROPHONE'
    
    # Platform-specific loopback/monitor device detection
    if CURRENT_PLATFORM == "Windows":
        windows_loopback = ['stereo mix', 'what u hear', 'loopback', 'mix', 'wave out']
        if any(x in name_lower for x in windows_loopback):
            return '🔊 LINE/LOOPBACK'
    elif CURRENT_PLATFORM == "Linux":
        linux_monitor = ['monitor', 'loopback', 'pulse', 'alsa']
        if any(x in name_lower for x in linux_monitor):
            return '🔊 MONITOR/LOOPBACK'
    elif CURRENT_PLATFORM == "Darwin":  # macOS
        macos_virtual = ['blackhole', 'soundflower', 'loopback', 'aggregate']
        if any(x in name_lower for x in macos_virtual):
            return '🔊 VIRTUAL/LOOPBACK'
    
    # Generic fallback detection
    if any(x in name_lower for x in ['loopback', 'monitor', 'mix', 'virtual']):
        return '🔊 SYSTEM AUDIO'
    
    return '❓ UNKNOWN'

def find_input_device():
    global SAMPLE_RATE
    devices = sd.query_devices()
    
    # First, try to find the system default input device
    default_device_index = find_default_device_index()
    
    print(f"DEBUG: Looking for device containing '{DEVICE_NAME_CONTAINS}' with {SAMPLE_RATE}Hz support")
    if default_device_index is not None:
        default_device = devices[default_device_index]
        platform_name = "Windows" if CURRENT_PLATFORM == "Windows" else CURRENT_PLATFORM
        print(f"DEBUG: {platform_name} default input device: [{default_device_index}] {default_device['name']}")
    
    print("DEBUG: Scanning for compatible input devices...")
    input_devices = []
    
    # Collect all compatible devices without verbose output
    for i, dev in enumerate(devices):
        if dev['max_input_channels'] > 0:
            # Check if device supports our target sample rate
            supports_target_rate = check_device_sample_rate(i, SAMPLE_RATE)
            
            # Only add devices that support our target sample rate
            if supports_target_rate:
                input_devices.append((i, dev))
    
    print(f"DEBUG: Found {len(input_devices)} input devices supporting {SAMPLE_RATE}Hz")
    
    if len(input_devices) == 0:
        print(f"\n❌ ERROR: No devices found that support {SAMPLE_RATE}Hz!")
        print("This might be a sample rate compatibility issue.")
        print("\nWould you like to try a different sample rate?")
        print("1. Try 44100 Hz (most common)")
        print("2. Try 48000 Hz (current setting)")
        print("3. Show devices that support any sample rate")
        print("4. Exit")
        
        choice = input("\nEnter your choice (1-4): ").strip()
        
        if choice == "1":
            SAMPLE_RATE = 44100
            print(f"Switching to {SAMPLE_RATE}Hz and retrying...")
            return find_input_device()  # Recursive call with new sample rate
        elif choice == "3":
            print("\nShowing ALL input devices (ignoring sample rate):")
            all_devices = []
            for i, dev in enumerate(devices):
                if dev['max_input_channels'] > 0:
                    default_indicator = f" 🎯 [{CURRENT_PLATFORM.upper()} DEFAULT]" if i == default_device_index else ""
                    print(f"  [{i}] {dev['name']}{default_indicator}")
                    print(f"      Channels: {dev['max_input_channels']}")
                    print(f"      Default Rate: {dev['default_samplerate']} Hz")
                    print(f"      Host API: {sd.query_hostapis(dev['hostapi'])['name']}")
                    all_devices.append((i, dev))
            
            if all_devices:
                manual_choice = input(f"\nManually select device index (0-{len(devices)-1}) or 'q' to quit: ").strip()
                if manual_choice.lower() == 'q':
                    sys.exit(1)
                try:
                    manual_index = int(manual_choice)
                    if 0 <= manual_index < len(devices):
                        print(f"WARNING: Using device {manual_index} without sample rate verification")
                        return manual_index
                except ValueError:
                    pass
        
        print("Exiting...")
        sys.exit(1)
    
    # Determine priority order for auto-selection:
    # 1. System default device that supports sample rate
    # 2. Device matching DEVICE_NAME_CONTAINS
    # 3. First available device
    
    auto_selected = None
    selection_reason = ""
    
    # Check if system default device supports our sample rate
    if default_device_index is not None:
        for i, dev in input_devices:
            if i == default_device_index:
                auto_selected = i
                selection_reason = f"{CURRENT_PLATFORM} default input device"
                break
    
    # If default device not available, try DEVICE_NAME_CONTAINS (prioritize exact match)
    if auto_selected is None:
        # First try exact match for "Analogue 3 + 4"
        for i, dev in input_devices:
            if DEVICE_NAME_CONTAINS.lower() == dev['name'].lower():
                auto_selected = i
                selection_reason = f"exact match for '{DEVICE_NAME_CONTAINS}'"
                break
        
        # If no exact match, try partial match
        if auto_selected is None:
            for i, dev in input_devices:
                if DEVICE_NAME_CONTAINS.lower() in dev['name'].lower():
                    auto_selected = i
                    selection_reason = f"contains '{DEVICE_NAME_CONTAINS}'"
                    break
    
    # If neither found, use first available device
    if auto_selected is None and input_devices:
        auto_selected = input_devices[0][0]
        selection_reason = "first available device"
    
    if auto_selected is not None:
        selected_device = devices[auto_selected]
        print(f"DEBUG: Auto-selected device [{auto_selected}]: {selected_device['name']}")
        print(f"DEBUG: Selection reason: {selection_reason}")
    
    if auto_selected is None:
        print(f"\nWARNING: No suitable device found!")
        print("Available devices that support the sample rate:")
        for i, dev in input_devices:
            print(f"  [{i}] {dev['name']}")
    
    # Interactive device selection
    print(f"\n{'='*60}")
    print(f"AUDIO DEVICE SELECTION ({SAMPLE_RATE}Hz)")
    print(f"{'='*60}")
    
    if auto_selected is not None:
        selected_device = devices[auto_selected]
        device_type = get_device_type_indicator(selected_device['name'])
        default_indicator = f" 🎯 [{CURRENT_PLATFORM.upper()} DEFAULT]" if auto_selected == default_device_index else ""
        
        print(f"Recommended device:")
        print(f"  [{auto_selected}] {selected_device['name']}{default_indicator}")
        print(f"  Type: {device_type}")
        print(f"  Reason: {selection_reason}")
        
        # Show platform-specific hints
        hints = get_platform_audio_hints()
        print(f"\n💡 Quick Tips:")
        for hint in hints[:2]:  # Just show first 2 hints
            print(f"  {hint}")
        
        use_auto = input(f"\nUse recommended device? (y/n/s for all devices/t to test): ").lower()
        if use_auto == 'y':
            return auto_selected
        elif use_auto == 'n':
            print("Exiting...")
            sys.exit(1)
        elif use_auto == 't':
            print("Testing recommended device...")
            print("Make sure audio is playing and press Enter to start test...")
            input("Press Enter to continue...")
            
            is_good = test_audio_source(auto_selected)
            if is_good:
                use_tested = input(f"\n✅ Device test passed! Use this device? (y/n/s for selector): ").lower()
                if use_tested == 'y':
                    return auto_selected
                elif use_tested == 'n':
                    print("Exiting...")
                    sys.exit(1)
                # 's' falls through to selector
            else:
                print("\n❌ Device test failed.")
                use_selector = input("Show device selector? (y/n): ").lower()
                if use_selector != 'y':
                    print("Exiting...")
                    sys.exit(1)
        # 's' or failed test falls through to selector
    else:
        print("No suitable device auto-selected.")
        use_selector = input("Show device selector? (y/n): ").lower()
        if use_selector != 'y':
            print("Exiting...")
            sys.exit(1)
    
    return select_audio_device(input_devices)

def select_audio_device(input_devices):
    """Interactive audio device selector with testing capability"""
    # Get default device index for highlighting
    default_device_index = find_default_device_index()
    devices = sd.query_devices()
    
    # Show detailed device analysis when user requests full selector
    print(f"\n{'='*60}")
    print(f"DETAILED DEVICE ANALYSIS ({CURRENT_PLATFORM})")
    print(f"{'='*60}")
    
    for device_index, dev in input_devices:
        print(f"\nDevice [{device_index}]: {dev['name']}")
        print(f"  Type: {get_device_type_indicator(dev['name'])}")
        print(f"  Channels: {dev['max_input_channels']} input, {dev['max_output_channels']} output")
        print(f"  Default Rate: {dev['default_samplerate']} Hz")
        print(f"  Host API: {sd.query_hostapis(dev['hostapi'])['name']}")
        
        # Mark if this is the system default device
        default_indicator = f" 🎯 [{CURRENT_PLATFORM.upper()} DEFAULT]" if device_index == default_device_index else ""
        if default_indicator:
            print(f"  Status:{default_indicator}")
    
    # Show platform-specific hints
    hints = get_platform_audio_hints()
    print(f"\n💡 Platform Hints:")
    for hint in hints:
        print(hint)
    
    while True:
        print(f"\n{'='*60}")
        print(f"INTERACTIVE AUDIO DEVICE SELECTOR ({SAMPLE_RATE}Hz)")
        print(f"{'='*60}")
        print(f"Available input devices (all support {SAMPLE_RATE}Hz):")
        
        for idx, (device_index, dev) in enumerate(input_devices):
            default_indicator = f" 🎯 DEFAULT" if device_index == default_device_index else ""
            device_type = get_device_type_indicator(dev['name'])
            print(f"  {idx + 1}. [{device_index}] {dev['name']}{default_indicator}")
            print(f"      Type: {device_type}")
        
        print(f"\nOptions:")
        print(f"  1-{len(input_devices)}: Select device")
        print(f"  t<number>: Test device (e.g., 't1' to test first device)")
        print(f"  q: Quit")
        
        choice = input(f"\nEnter your choice: ").strip().lower()
        
        if choice == 'q':
            print("Exiting...")
            sys.exit(1)
        
        # Handle test command
        if choice.startswith('t'):
            try:
                test_idx = int(choice[1:]) - 1
                if 0 <= test_idx < len(input_devices):
                    device_index, dev = input_devices[test_idx]
                    device_type = get_device_type_indicator(dev['name'])
                    print(f"\nTesting device [{device_index}]: {dev['name']}{device_type} at {SAMPLE_RATE}Hz")
                    print("Make sure audio is playing and press Enter to start test...")
                    input("Press Enter to continue...")
                    
                    is_good = test_audio_source(device_index)
                    if is_good:
                        use_this = input(f"\n✅ Device seems to work well! Use this device? (y/n): ").lower()
                        if use_this == 'y':
                            return device_index
                    else:
                        print("\n❌ Device test failed or no audio detected.")
                        input("Press Enter to continue...")
                else:
                    print("Invalid device number for testing!")
            except (ValueError, IndexError):
                print("Invalid test command! Use format 't1', 't2', etc.")
            continue
        
        # Handle device selection
        try:
            selected_idx = int(choice) - 1
            if 0 <= selected_idx < len(input_devices):
                device_index, dev = input_devices[selected_idx]
                device_type = get_device_type_indicator(dev['name'])
                print(f"\nSelected device [{device_index}]: {dev['name']}{device_type} ({SAMPLE_RATE}Hz)")
                
                # Ask if they want to test it first
                test_first = input("Test this device before using? (y/n): ").lower()
                if test_first == 'y':
                    print("Make sure audio is playing and press Enter to start test...")
                    input("Press Enter to continue...")
                    
                    is_good = test_audio_source(device_index)
                    if not is_good:
                        print("\n❌ Device test failed or no audio detected.")
                        retry = input("Try a different device? (y/n): ").lower()
                        if retry == 'y':
                            continue
                        else:
                            print("Continuing with selected device anyway...")
                
                return device_index
            else:
                print(f"Invalid choice! Please enter a number between 1 and {len(input_devices)}")
        except ValueError:
            print("Invalid input! Please enter a number, test command (t1, t2, etc.), or 'q' to quit.")

def draw_vu_meter(rms_level, peak_level, width=40):
    """Draw a simple VU meter using text characters"""
    # Normalize levels to 0-1 range - adjusted for lower audio levels
    # Use adaptive scaling based on the actual levels detected
    if rms_level < 100:
        # For very low levels, scale differently
        rms_norm = min(rms_level / 50, 1.0)
        peak_norm = min(peak_level / 100, 1.0)
    else:
        # For normal levels
        rms_norm = min(rms_level / 2000, 1.0)
        peak_norm = min(peak_level / 10000, 1.0)
    
    # Calculate bar lengths
    rms_bars = int(rms_norm * width)
    peak_bars = int(peak_norm * width)
    
    # Create VU meter display
    vu_line = "["
    for i in range(width):
        if i < rms_bars:
            if rms_norm > 0.8:
                vu_line += "█"  # Red zone
            elif rms_norm > 0.6:
                vu_line += "▓"  # Yellow zone
            else:
                vu_line += "▒"  # Green zone
        elif i == peak_bars:
            vu_line += "|"  # Peak indicator
        else:
            vu_line += "·"
    vu_line += "]"
    
    return vu_line

def draw_waveform(audio_data, width=60, height=5):
    """Draw a simple low-poly waveform representation"""
    if len(audio_data.shape) > 1:
        # Convert stereo to mono for waveform
        audio_mono = np.mean(audio_data, axis=1)
    else:
        audio_mono = audio_data
    
    # Downsample to fit width
    chunk_size = len(audio_mono) // width
    if chunk_size == 0:
        chunk_size = 1
    
    waveform_data = []
    for i in range(0, len(audio_mono), chunk_size):
        chunk = audio_mono[i:i+chunk_size]
        if len(chunk) > 0:
            # Use RMS of chunk for smoother representation
            rms = np.sqrt(np.mean(chunk**2))
            waveform_data.append(rms)
    
    if not waveform_data:
        return ["No waveform data"]
    
    # Normalize waveform data
    max_val = max(waveform_data) if max(waveform_data) > 0 else 1
    normalized = [val / max_val for val in waveform_data]
    
    # Create multi-line waveform
    lines = []
    chars = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    
    for level in normalized:
        char_idx = min(int(level * len(chars)), len(chars) - 1)
        lines.append(chars[char_idx])
    
    return ["".join(lines)]

def test_audio_source(device_index, test_seconds=5):
    """Test an audio source to see if it's capturing meaningful audio"""
    print(f"\nDEBUG: === TESTING AUDIO SOURCE [{device_index}] ===")
    devices = sd.query_devices()
    if device_index < len(devices):
        print(f"Testing device: {devices[device_index]['name']}")
    
    try:
        print(f"Recording {test_seconds} seconds for testing...")
        
        # Record directly as int16 - simple and straightforward
        recording = sd.rec(int(test_seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                           channels=CHANNELS, dtype='int16', device=device_index)
        sd.wait()
        
        # Basic sanity check only
        if np.all(recording == 0):
            print("  ❌ ERROR: Recording is all zeros - no audio input detected")
            return False
        
        # Analyze the recording - use float64 for calculations to avoid overflow
        rms = np.sqrt(np.mean(recording.astype(np.float64)**2))
        peak = np.max(np.abs(recording))
        mean_abs = np.mean(np.abs(recording))
        
        print(f"\nAudio Analysis:")
        print(f"  RMS Level: {rms:.2f}")
        print(f"  Peak Level: {peak:.2f}")
        print(f"  Mean Absolute: {mean_abs:.2f}")
        
        # Calculate dynamic range safely to avoid overflow
        min_val = np.min(recording)
        dynamic_range = float(peak) - float(min_val)
        print(f"  Dynamic Range: {dynamic_range:.2f}")
        
        # Show VU meter
        vu_meter = draw_vu_meter(rms, peak)
        print(f"  VU Meter:  {vu_meter}")
        print(f"             RMS:{rms:>6.0f}  PEAK:{peak:>6.0f}")
        
        # Show waveform
        waveform_lines = draw_waveform(recording)
        print(f"  Waveform:  {waveform_lines[0]}")
        
        # Check for silence or very low levels - adjusted thresholds for real audio
        if rms < 1:
            print("  ⚠️  WARNING: Extremely low audio levels - might be wrong input or no audio")
        elif rms < 5:
            print("  ⚠️  WARNING: Very low audio levels - check volume or input source")
        elif rms < 20:
            print("  ℹ️  INFO: Low but detectable audio levels")
        elif rms > 25000:
            print("  ⚠️  WARNING: Very high audio levels - might be clipping")
        else:
            print("  ✅ Audio levels look good")
        
        # Check for stereo content
        if CHANNELS == 2:
            left_channel = recording[:, 0]
            right_channel = recording[:, 1]
            
            left_rms = np.sqrt(np.mean(left_channel.astype(np.float64)**2))
            right_rms = np.sqrt(np.mean(right_channel.astype(np.float64)**2))
            
            print(f"  Left Channel RMS: {left_rms:.2f}")
            print(f"  Right Channel RMS: {right_rms:.2f}")
            
            # Show stereo VU meters
            left_vu = draw_vu_meter(left_rms, np.max(np.abs(left_channel)), width=20)
            right_vu = draw_vu_meter(right_rms, np.max(np.abs(right_channel)), width=20)
            print(f"  L: {left_vu}")
            print(f"  R: {right_vu}")
            
            if abs(left_rms - right_rms) < (max(left_rms, right_rms) * 0.1):
                print("  ✅ Stereo content detected")
            else:
                print("  ⚠️  Channels have different levels - might be mono or unbalanced")
        
        # Lower threshold - if we detect any meaningful audio above background noise
        return rms > 1  # Return True if we think there's meaningful audio (lowered from 10)
        
    except Exception as e:
        print(f"  ❌ Error testing device: {e}")
        print(f"  Full error details: {type(e).__name__}: {str(e)}")
        return False

def record_chunk(device_index):
    print(f"DEBUG: Recording {CHUNK_SECONDS}s from device {device_index}...")
    print(f"DEBUG: Optimized for partial track recognition services (Shazam, AudD)")
    try:
        # Record directly as int16 - let sounddevice handle the conversion
        recording = sd.rec(int(CHUNK_SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                          channels=CHANNELS, dtype='int16', device=device_index)
        sd.wait()
        
        # Check if recording has any audio (basic sanity check only)
        if np.all(recording == 0):
            print("DEBUG: ❌ ERROR: Recording is all zeros - no input detected")
            return None
        
        # Simple analysis for feedback (no preprocessing)
        rms = np.sqrt(np.mean(recording.astype(np.float64)**2))
        peak = np.max(np.abs(recording))
        
        # Simple music detection heuristics
        print(f"DEBUG: === AUDIO CONTENT ANALYSIS ===")
        
        # Check for dynamic range (music usually has varying levels)
        dynamic_ratio = peak / max(rms, 1)
        print(f"DEBUG: Dynamic ratio (peak/rms): {dynamic_ratio:.2f}")
        
        if dynamic_ratio < 1.5:
            print(f"DEBUG: ⚠️  Low dynamic range - might be constant tone or noise")
        elif dynamic_ratio > 10:
            print(f"DEBUG: ⚠️  Very high dynamic range - might be mostly silence with brief sounds")
        else:
            print(f"DEBUG: ✅ Good dynamic range for music")
        
        # Check for frequency content by looking at variation over time
        if len(recording.shape) > 1:
            mono_signal = np.mean(recording, axis=1)
        else:
            mono_signal = recording
            
        # Simple spectral analysis - check if there's variation
        chunk_size = len(mono_signal) // 20  # 20 chunks
        chunk_rms_values = []
        for i in range(0, len(mono_signal), chunk_size):
            chunk = mono_signal[i:i+chunk_size]
            if len(chunk) > 0:
                chunk_rms = np.sqrt(np.mean(chunk**2))
                chunk_rms_values.append(chunk_rms)
        
        if len(chunk_rms_values) > 1:
            rms_variation = np.std(chunk_rms_values) / max(np.mean(chunk_rms_values), 1)
            print(f"DEBUG: RMS variation over time: {rms_variation:.3f}")
            
            if rms_variation < 0.1:
                print(f"DEBUG: ⚠️  Very little variation - might be constant tone or silence")
            elif rms_variation > 2.0:
                print(f"DEBUG: ⚠️  Extreme variation - might be sporadic noise")
            else:
                print(f"DEBUG: ✅ Good temporal variation for music")
        
        # Check if stereo content looks like music
        if CHANNELS == 2:
            left_channel = recording[:, 0]
            right_channel = recording[:, 1]
            
            left_rms = np.sqrt(np.mean(left_channel.astype(np.float64)**2))
            right_rms = np.sqrt(np.mean(right_channel.astype(np.float64)**2))
            
            print(f"DEBUG: L/R RMS: {left_rms:.2f} / {right_rms:.2f}")
            
            # Simple correlation check (no NaN handling - let it fail naturally if there are issues)
            try:
                correlation = np.corrcoef(left_channel, right_channel)[0, 1]
                print(f"DEBUG: L/R correlation: {correlation:.3f}")
            except:
                print(f"DEBUG: L/R correlation: calculation failed")
        
        print(f"DEBUG: === END AUDIO ANALYSIS ===")
        
        # Create AudioSegment directly from int16 recording - no conversion needed
        audio = AudioSegment(
            recording.tobytes(),
            frame_rate=SAMPLE_RATE,
            sample_width=recording.dtype.itemsize,
            channels=CHANNELS
        )
        wav_io = BytesIO()
        audio.export(wav_io, format="wav")
        wav_io.seek(0)
        wav_data = wav_io.read()
        
        print(f"DEBUG: WAV data size: {len(wav_data)} bytes")
        
        # Save a copy for debugging (optional - you can disable this)
        if DEBUG_SAVE_AUDIO:
            debug_filename = f"debug_audio_{int(time.time())}.wav"
            try:
                with open(debug_filename, "wb") as f:
                    f.write(wav_data)
                print(f"DEBUG: Saved audio sample to {debug_filename} for manual inspection")
                print(f"DEBUG: You can play this file to verify it contains the correct audio")
                print(f"DEBUG: ⚠️  Remember to delete debug files when done: debug_audio_*.wav")
            except Exception as e:
                print(f"DEBUG: Could not save debug audio: {e}")
        
        return wav_data
        
    except Exception as e:
        print(f"DEBUG: Recording error: {e}")
        print(f"DEBUG: Full error details: {type(e).__name__}: {str(e)}")
        return None

def fingerprint(wav_data):
    if not wav_data:
        print("DEBUG: No WAV data to fingerprint")
        return None, None
        
    # Write temp WAV file for fpcalc
    tmp_filename = "temp_fpcalc.wav"
    print(f"DEBUG: Writing {len(wav_data)} bytes to {tmp_filename}")
    
    with open(tmp_filename, "wb") as f:
        f.write(wav_data)
    
    print(f"DEBUG: Temp file size: {os.path.getsize(tmp_filename)} bytes")
    
    try:
        print(f"DEBUG: Running fpcalc: {FP_CALC_PATH} -json {tmp_filename}")
        result = subprocess.run([FP_CALC_PATH, "-json", tmp_filename],
                                capture_output=True, text=True, check=True)
        
        print(f"DEBUG: fpcalc stdout: {result.stdout}")
        if result.stderr:
            print(f"DEBUG: fpcalc stderr: {result.stderr}")
            
        output = json.loads(result.stdout)
        fingerprint = output.get("fingerprint")
        duration = output.get("duration")
        
        print(f"DEBUG: Fingerprint length: {len(fingerprint) if fingerprint else 0}")
        print(f"DEBUG: Duration: {duration}s")
        
        # Additional validation
        if not fingerprint:
            print(f"DEBUG: ❌ ERROR: No fingerprint generated by fpcalc!")
            print(f"DEBUG: fpcalc output: {output}")
            return None, None
            
        if len(fingerprint) < 50:  # Fingerprints should be much longer
            print(f"DEBUG: ⚠️  WARNING: Fingerprint seems very short ({len(fingerprint)} chars)")
            print(f"DEBUG: Fingerprint preview: {fingerprint[:100]}...")
            
        if duration < 20:  # We're recording 30 seconds, should be close to that
            print(f"DEBUG: ⚠️  WARNING: Duration seems short ({duration}s, expected ~{CHUNK_SECONDS}s)")
            print(f"DEBUG: Short audio clips may not work well with AcoustID")
        elif duration >= 30:
            print(f"DEBUG: ✅ Good duration for AcoustID fingerprinting ({duration}s)")
        else:
            print(f"DEBUG: ℹ️  Duration: {duration}s (AcoustID prefers 30+ seconds)")
        
        return fingerprint, duration
    except subprocess.CalledProcessError as e:
        print(f"DEBUG: fpcalc subprocess error: {e}")
        print(f"DEBUG: fpcalc return code: {e.returncode}")
        print(f"DEBUG: fpcalc stderr: {e.stderr}")
        return None, None
    except json.JSONDecodeError as e:
        print(f"DEBUG: JSON decode error: {e}")
        print(f"DEBUG: Raw output: {result.stdout}")
        return None, None
    except Exception as e:
        print(f"DEBUG: fpcalc error: {e}")
        return None, None
    finally:
        if os.path.exists(tmp_filename):
            os.remove(tmp_filename)

def lookup_shazam(wav_data):
    """Use Shazam-like identification for partial track recognition"""
    if not SHAZAMIO_AVAILABLE or not USE_SHAZAM_API:
        return None
        
    print("DEBUG: Using Shazam for track identification...")
    
    try:
        # Create event loop for async operation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def identify_track():
            shazam = Shazam()
            
            # Write temp file for Shazam
            temp_filename = f"temp_shazam_{int(time.time())}.wav"
            try:
                with open(temp_filename, "wb") as f:
                    f.write(wav_data)
                
                print(f"DEBUG: Analyzing audio with Shazam...")
                result = await shazam.recognize(temp_filename)
                
                if result and 'track' in result:
                    track = result['track']
                    artist = track.get('subtitle', 'Unknown Artist')
                    title = track.get('title', 'Unknown Title')
                    album = track.get('sections', [{}])[0].get('metadata', [{}])[0].get('text', 'Unknown Album') if 'sections' in track else 'Unknown Album'
                    
                    # Try alternative album field locations in Shazam response
                    if album == 'Unknown Album':
                        # Check if there's album info in hub or other sections
                        if 'hub' in track and 'displayname' in track['hub']:
                            album = track['hub']['displayname']
                        elif 'albumadamid' in track:
                            album = 'Album Available'  # Placeholder when ID exists but name not provided
                    
                    if artist != 'Unknown Artist' and title != 'Unknown Title':
                        print(f"DEBUG: Shazam identified: {artist} - {title}")
                        
                        # Extract album art if available
                        album_art = None
                        if 'images' in track:
                            images = track['images']
                            print(f"DEBUG: Shazam images available: {list(images.keys())}")
                            # Shazam provides various image types - prefer high quality
                            if 'coverarthq' in images:
                                album_art = images['coverarthq']
                                print(f"DEBUG: ✅ Found Shazam HQ cover art: {album_art}")
                            elif 'coverart' in images:
                                album_art = images['coverart']
                                print(f"DEBUG: ✅ Found Shazam cover art: {album_art}")
                            elif 'background' in images:
                                album_art = images['background']
                                print(f"DEBUG: ✅ Found Shazam background art: {album_art}")
                        else:
                            print(f"DEBUG: ❌ No 'images' field in Shazam response")
                        
                        if not album_art:
                            print(f"DEBUG: ❌ No album art found in Shazam response")
                        
                        return {
                            'artist': artist, 
                            'title': title, 
                            'service': 'Shazam',
                            'album_art': album_art,
                            'album': album
                        }
                
                print("DEBUG: Shazam could not identify track")
                return None
                
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
        
        # Run async function
        result = loop.run_until_complete(identify_track())
        loop.close()
        return result
        
    except Exception as e:
        print(f"DEBUG: Shazam identification error: {e}")
        return None

def lookup_audd_api(wav_data):
    """Use AudD API for track identification - free, no signup required!"""
    print("DEBUG: Using AudD API for track identification (free, no signup!)")
    
    try:
        # AudD.io API - completely free tier, no registration needed
        url = "https://api.audd.io/"
        
        # Write temp file
        temp_filename = f"temp_audd_{int(time.time())}.wav"
        try:
            with open(temp_filename, "wb") as f:
                f.write(wav_data)
            
            # Prepare file for upload - using 'test' token for free usage
            with open(temp_filename, "rb") as f:
                files = {'file': f}
                data = {
                    'api_token': AUDD_API_TOKEN,  # Use configured API token
                    'return': 'apple_music,spotify'  # Get additional metadata
                }
                
                print(f"DEBUG: Uploading to AudD API...")
                response = requests.post(url, files=files, data=data, timeout=30)
                
                # Handle rate limiting and expiration
                if response.status_code == 429:
                    print(f"DEBUG: ❌ AudD API rate limit exceeded!")
                    print(f"DEBUG: 💡 Get a free API key from https://audd.io/ for higher limits")
                    return None
                elif response.status_code == 403:
                    print(f"DEBUG: ❌ AudD API access denied - API key may have expired")
                    print(f"DEBUG: 💡 Free tier expires after ~2 weeks. Consider using Shazam (free forever)")
                    return None
                elif response.status_code == 402:
                    print(f"DEBUG: ❌ AudD API payment required - free trial expired")
                    print(f"DEBUG: 💡 Switching to Shazam (completely free) is recommended")
                    return None
                
                response.raise_for_status()
                
                result = response.json()
                print(f"DEBUG: AudD API response status: {result.get('status')}")
                
                if result.get('status') == 'success' and result.get('result'):
                    track_info = result['result']
                    artist = track_info.get('artist', 'Unknown Artist')
                    title = track_info.get('title', 'Unknown Title')
                    
                    if artist != 'Unknown Artist' and title != 'Unknown Title':
                        print(f"DEBUG: ✅ AudD identified: {artist} - {title}")
                        
                        # Extract album information
                        album = track_info.get('album', 'Unknown Album')
                        
                        # Check for additional metadata and album art
                        album_art_url = None
                        if 'apple_music' in track_info:
                            if track_info['apple_music'].get('artwork'):
                                raw_url = track_info['apple_music']['artwork'].get('url')
                                if raw_url:
                                    # Apple Music URLs contain {w}x{h} placeholders - replace with actual dimensions
                                    if '{w}x{h}' in raw_url:
                                        album_art_url = raw_url.replace('{w}x{h}', '512x512')
                                        print(f"DEBUG: Fixed Apple Music album art URL: {album_art_url}")
                                    else:
                                        album_art_url = raw_url
                                        print(f"DEBUG: Found Apple Music album art URL: {album_art_url}")
                            # Also get album from Apple Music if not found in main result
                            if album == 'Unknown Album' and track_info['apple_music'].get('collectionName'):
                                album = track_info['apple_music']['collectionName']
                        elif 'spotify' in track_info:
                            if track_info['spotify'].get('album', {}).get('images'):
                                images = track_info['spotify']['album']['images']
                                if images:
                                    album_art_url = images[0].get('url')  # Use largest image
                                    print(f"DEBUG: Found Spotify album art URL: {album_art_url}")
                            # Also get album from Spotify if not found in main result  
                            if album == 'Unknown Album' and track_info['spotify'].get('album', {}).get('name'):
                                album = track_info['spotify']['album']['name']
                        
                        return {
                            'artist': artist, 
                            'title': title, 
                            'service': 'AudD',
                            'album_art': album_art_url,
                            'album': album
                        }
                    else:
                        print("DEBUG: AudD returned empty artist/title")
                else:
                    print(f"DEBUG: AudD API unsuccessful: {result}")
                
                print("DEBUG: AudD could not identify track")
                return None
                
        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)
        
    except Exception as e:
        print(f"DEBUG: AudD API error: {e}")
        return None

def identify_track_multiple_services(wav_data):
    """Try multiple track identification services in order of preference"""
    print("DEBUG: === TRACK IDENTIFICATION ===")
    print("DEBUG: Trying multiple services for partial track recognition...")
    
    # Service priority order - Prioritize what's actually available
    services = []
    
    # Primary: Shazam (if available - excellent for partial tracks)
    if SHAZAMIO_AVAILABLE and USE_SHAZAM_API:
        services.append(("Shazam", lookup_shazam))
    
    # Secondary: AudD - works well but free tier expires
    if USE_AUDD_API:
        services.append(("AudD", lookup_audd_api))
    
    # Reliable fallback: AcoustID (free forever, no complex dependencies)
    if USE_ACOUSTID_FALLBACK:
        services.append(("AcoustID", lambda data: lookup_acoustid_from_wav(data)))
    
    # If no Shazam, promote AcoustID to secondary priority
    if not SHAZAMIO_AVAILABLE and USE_ACOUSTID_FALLBACK and len(services) >= 2:
        # Move AcoustID to second position if Shazam isn't available
        acoustid_service = services.pop()  # Remove from end
        services.insert(1, acoustid_service)  # Insert after AudD
    
    for service_name, service_func in services:
        print(f"DEBUG: Trying {service_name}...")
        try:
            result = service_func(wav_data)
            if result and result.get('artist') and result.get('title'):
                print(f"DEBUG: ✅ {service_name} success: {result['artist']} - {result['title']}")
                
                # Check what album art we got from the service
                if result.get('album_art'):
                    print(f"DEBUG: 🖼️ {service_name} provided album art: {result['album_art']}")
                else:
                    print(f"DEBUG: 🖼️ {service_name} did not provide album art - trying fallback sources...")
                    # If we don't have album art yet, try to fetch it
                    album_art = fetch_album_art(result['artist'], result['title'])
                    if album_art:
                        result['album_art'] = album_art
                        print(f"DEBUG: ✅ Fallback album art found: {album_art}")
                    else:
                        print(f"DEBUG: ❌ No album art found from any fallback source")
                
                return result
            else:
                print(f"DEBUG: ❌ {service_name} - no match")
        except Exception as e:
            print(f"DEBUG: ❌ {service_name} error: {e}")
            if "rate limit" in str(e).lower() or "429" in str(e):
                print(f"DEBUG: 💡 {service_name} rate limited - trying next service...")
            continue
    
    print("DEBUG: No services could identify the track")
    return None

def fetch_album_art(artist, title):
    """Fetch album art from multiple free sources"""
    # Try multiple sources in order of preference (iTunes first - most reliable)
    sources = [
        ("iTunes", fetch_itunes_album_art),
        ("Last.fm", fetch_lastfm_album_art),
        ("MusicBrainz", fetch_musicbrainz_album_art)
    ]
    
    for source_name, fetch_func in sources:
        try:
            print(f"DEBUG: Trying {source_name} for album art...")
            album_art_url = fetch_func(artist, title)
            if album_art_url:
                print(f"DEBUG: ✅ Found album art from {source_name}: {album_art_url}")
                return album_art_url
            else:
                print(f"DEBUG: ❌ No album art from {source_name}")
        except Exception as e:
            print(f"DEBUG: ❌ {source_name} album art error: {e}")
            continue
    
    return None

def fetch_lastfm_album_art(artist, title):
    """Fetch album art from Last.fm (free, no API key required)"""
    try:
        # Last.fm API endpoint (no key required for basic track info)
        url = "http://ws.audioscrobbler.com/2.0/"
        params = {
            'method': 'track.getInfo',
            'api_key': '7c60c1d9f6d1c5e1c16b8a0c7f6f5c6e',  # Public demo key
            'artist': artist,
            'track': title,
            'format': 'json'
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if 'track' in data and 'album' in data['track']:
            album = data['track']['album']
            if 'image' in album:
                images = album['image']
                # Find the largest image
                for img in reversed(images):  # Last.fm puts largest last
                    if img.get('#text'):
                        return img['#text']
    except Exception as e:
        print(f"DEBUG: Last.fm error: {e}")
    
    return None

def fetch_musicbrainz_album_art(artist, title):
    """Fetch album art via MusicBrainz + Cover Art Archive"""
    try:
        # Search for the recording in MusicBrainz
        search_url = "https://musicbrainz.org/ws/2/recording/"
        params = {
            'query': f'artist:"{artist}" AND recording:"{title}"',
            'fmt': 'json',
            'limit': 1
        }
        headers = {
            'User-Agent': 'PyNowPlaying/1.0 (contact@example.com)'
        }
        
        response = requests.get(search_url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('recordings'):
            recording = data['recordings'][0]
            if 'releases' in recording and recording['releases']:
                release_id = recording['releases'][0]['id']
                
                # Try to get cover art from Cover Art Archive
                cover_url = f"https://coverartarchive.org/release/{release_id}"
                cover_response = requests.get(cover_url, timeout=10)
                if cover_response.status_code == 200:
                    cover_data = cover_response.json()
                    if 'images' in cover_data and cover_data['images']:
                        # Return the front cover if available
                        for img in cover_data['images']:
                            if img.get('front', False):
                                return img.get('image', img.get('thumbnails', {}).get('large'))
                        # If no front cover, return first image
                        return cover_data['images'][0].get('image')
    except Exception as e:
        print(f"DEBUG: MusicBrainz/Cover Art Archive error: {e}")
    
    return None

def fetch_itunes_album_art(artist, title):
    """Fetch album art from iTunes Search API (free, no key required)"""
    try:
        url = "https://itunes.apple.com/search"
        params = {
            'term': f"{artist} {title}",
            'media': 'music',
            'entity': 'song',
            'limit': 5  # Get more results for better matching
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('results'):
            # Try to find the best match
            for result in data['results']:
                artwork_url = result.get('artworkUrl100')
                if artwork_url:
                    # Convert to higher resolution by changing the size
                    high_res_url = artwork_url.replace('100x100', '512x512')
                    print(f"DEBUG: iTunes found artwork: {high_res_url}")
                    return high_res_url
            
            # If no artwork found in any result, return None
            print(f"DEBUG: iTunes found {len(data['results'])} results but no artwork")
    except Exception as e:
        print(f"DEBUG: iTunes Search API error: {e}")
    
    return None

def lookup_acoustid_from_wav(wav_data):
    """Convert WAV data to fingerprint and lookup via AcoustID (for fallback)"""
    print("DEBUG: Using AcoustID as fallback...")
    
    # Write temp file for fpcalc
    temp_filename = f"temp_acoustid_{int(time.time())}.wav"
    try:
        with open(temp_filename, "wb") as f:
            f.write(wav_data)
        
        # Generate fingerprint
        fp, dur = fingerprint_from_file(temp_filename)
        if fp and dur:
            return lookup_acoustid(fp, dur)
        return None
        
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

def lookup_musicbrainz_direct(mbid):
    """Query MusicBrainz directly using a recording MBID"""
    print(f"DEBUG: Querying MusicBrainz directly for recording {mbid}")
    
    try:
        url = f"https://musicbrainz.org/ws/2/recording/{mbid}"
        params = {
            'fmt': 'json',
            'inc': 'artists+releases'
        }
        
        headers = {
            'User-Agent': 'PyNowPlaying/1.0 (contact@example.com)'  # Required by MusicBrainz
        }
        
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        # Extract artist and title
        title = data.get('title', '')
        artist_credits = data.get('artist-credit', [])
        artist = artist_credits[0]['name'] if artist_credits else ''
        
        if artist and title:
            print(f"DEBUG: MusicBrainz direct result: {artist} - {title}")
            return {
                'artist': artist, 
                'title': title, 
                'service': 'MusicBrainz', 
                'album_art': None,  # MusicBrainz direct doesn't include album art
                'mbid': mbid
            }
        
        return None
        
    except Exception as e:
        print(f"DEBUG: MusicBrainz direct query error: {e}")
        return None

def fingerprint_from_file(filename):
    """Generate fingerprint from audio file"""
    try:
        result = subprocess.run([FP_CALC_PATH, "-json", filename],
                                capture_output=True, text=True, check=True)
        output = json.loads(result.stdout)
        return output.get("fingerprint"), output.get("duration")
    except Exception as e:
        print(f"DEBUG: Fingerprint generation error: {e}")
        return None, None

def lookup_acoustid(fingerprint, duration):
    if not fingerprint or not duration:
        print("DEBUG: Missing fingerprint or duration for AcoustID lookup")
        return None
        
    url = "https://api.acoustid.org/v2/lookup"
    params = {
        'client': ACOUSTID_API_KEY,
        'duration': int(duration),
        'fingerprint': fingerprint,
        'format': 'json',
        'meta': 'recordings+releasegroups+artists+recordingids'  # Add recordingids for MusicBrainz IDs
    }
    
    print(f"DEBUG: AcoustID request - Duration: {duration}s, Fingerprint length: {len(fingerprint)}")
    
    try:
        r = requests.get(url, params=params, timeout=10)
        print(f"DEBUG: AcoustID response status: {r.status_code}")
        r.raise_for_status()
        data = r.json()
        
        print(f"DEBUG: AcoustID response status: {data.get('status')}")
        print(f"DEBUG: Number of results: {len(data.get('results', []))}")
        
        if data['status'] != 'ok':
            print(f"DEBUG: AcoustID API error: {data.get('error', 'Unknown error')}")
            return None
            
        if not data['results']:
            print("DEBUG: ❌ No results from AcoustID")
            print(f"DEBUG: This could mean:")
            print(f"  - Audio snippet too short (AcoustID needs longer samples, ideally 30+ seconds)")
            print(f"  - Audio is not in the AcoustID database")
            print(f"  - Background noise/interference")
            print(f"  - Non-music audio (speech, sound effects, etc.)")
            print(f"  - Song is too new or obscure for the database")
            print(f"DEBUG: Try playing a well-known song for at least 30 seconds")
            return None
            
        # Debug: Show all results
        for i, res in enumerate(data['results']):
            score = res.get('score', 0)
            print(f"DEBUG: Result {i}: score={score:.3f}")
            if 'recordings' in res:
                for j, rec in enumerate(res['recordings']):
                    artist = rec['artists'][0]['name'] if rec.get('artists') else 'Unknown'
                    title = rec.get('title', 'Unknown')
                    mbid = rec.get('id', 'No MBID')
                    print(f"  Recording {j}: {artist} - {title} (MBID: {mbid})")
        
        # Pick best recording match with artist/title
        for res in data['results']:
            if 'recordings' in res:
                rec = res['recordings'][0]
                artist = rec['artists'][0]['name'] if rec.get('artists') else ''
                title = rec.get('title', '')
                mbid = rec.get('id', '')
                
                # Extract album information if available
                album = 'Unknown Album'
                if 'releasegroups' in rec and rec['releasegroups']:
                    album = rec['releasegroups'][0].get('title', 'Unknown Album')
                
                if artist and title:
                    print(f"DEBUG: Selected AcoustID match: {artist} - {title}")
                    if album != 'Unknown Album':
                        print(f"DEBUG: Album: {album}")
                    
                    # Optionally try MusicBrainz direct query for additional metadata
                    if mbid and USE_MUSICBRAINZ_DIRECT:
                        print(f"DEBUG: MBID available: {mbid}, querying MusicBrainz directly...")
                        mb_result = lookup_musicbrainz_direct(mbid)
                        if mb_result:
                            print(f"DEBUG: Using MusicBrainz enhanced data")
                            return mb_result
                        else:
                            print(f"DEBUG: MusicBrainz direct query failed, using AcoustID data")
                    elif mbid:
                        print(f"DEBUG: MBID available: {mbid} (direct MusicBrainz lookup disabled)")
                    
                    return {
                        'artist': artist, 
                        'title': title, 
                        'service': 'AcoustID',
                        'album_art': None,  # AcoustID doesn't provide album art
                        'album': album,
                        'mbid': mbid
                    }
                    
        print("DEBUG: No recordings with both artist and title found")
        return None
        
    except requests.exceptions.RequestException as e:
        print(f"DEBUG: AcoustID request error: {e}")
        return None
    except Exception as e:
        print(f"DEBUG: AcoustID lookup error: {e}")
        return None

def track_changed(new_track):
    global current_track
    if new_track is None:
        return False
    return (new_track.get("artist") != current_track.get("artist") or
            new_track.get("title") != current_track.get("title"))

def audio_loop(device_index):
    global current_track, track_history, last_identified_track, consecutive_match_count
    print(f"DEBUG: Starting audio loop...")
    print(f"DEBUG: Recording {CHUNK_SECONDS}s chunks for track identification")
    print(f"DEBUG: Service priority: Shazam (free forever) → AudD (trial) → AcoustID (free forever)")
    
    while True:
        print(f"\nDEBUG: === Starting new {CHUNK_SECONDS}s audio chunk ===")
        wav_data = record_chunk(device_index)
        
        if not wav_data:
            print("DEBUG: No audio data recorded, retrying...")
            time.sleep(2)
            continue
        
        # Use multi-service track identification (AudD first)
        track_info = identify_track_multiple_services(wav_data)
        
        if track_info:
            # Create a unique identifier for the track
            track_id = f"{track_info['artist']} - {track_info['title']}"
            
            # Check for consecutive matches
            if last_identified_track == track_id:
                consecutive_match_count += 1
                print(f"DEBUG: 🔄 Consecutive match #{consecutive_match_count}: {track_id}")
            else:
                consecutive_match_count = 1
                last_identified_track = track_id
                print(f"DEBUG: 🆕 New identification: {track_id}")
            
            # Handle track changes and consecutive match delays
            if track_changed(track_info):
                now_str = datetime.now().strftime("%H:%M:%S")
                source = track_info.get('service', track_info.get('source', 'unknown'))
                print(f"DEBUG: 🎵 Track changed: {track_info['artist']} - {track_info['title']} at {now_str} (via {source})")
                current_track = {
                    "artist": track_info['artist'],
                    "title": track_info['title'],
                    "time": now_str,
                    "album_art": track_info.get('album_art', ''),
                    "album": track_info.get('album', 'Unknown Album')
                }
                track_history.insert(0, current_track.copy())
                # Keep last 20 tracks
                track_history = track_history[:20]
                
                # Reset consecutive count on track change
                consecutive_match_count = 1
            else:
                source = track_info.get('service', track_info.get('source', 'unknown'))
                print(f"DEBUG: 🔄 Same track detected: {track_info['artist']} - {track_info['title']} (via {source})")
            
            # Apply extended delay for consecutive matches
            if consecutive_match_count >= 2:
                print(f"DEBUG: ⏸️  Extended delay: Same track identified {consecutive_match_count} times in a row")
                print(f"DEBUG: ⏳ Waiting 30 seconds to avoid service spam...")
                time.sleep(30)
            else:
                # Wait before next sample (normal delay for sustainable Shazam usage)
                print(f"DEBUG: ⏳ Waiting before next {CHUNK_SECONDS}s sample...")
                time.sleep(8)  # Normal delay to avoid rate limiting
        else:
            print("DEBUG: ❌ No match found across all services.")
            print("DEBUG: 💡 Tips for better recognition:")
            print("DEBUG:   - Ensure music is playing clearly (not paused)")
            print("DEBUG:   - Try popular/mainstream songs (better database coverage)")
            print("DEBUG:   - Check audio levels are good (not too quiet/loud)")
            print("DEBUG:   - Reduce background noise and talking")
            
            # Reset consecutive tracking when no match found
            last_identified_track = None
            consecutive_match_count = 0
            
            # Wait before next sample (normal delay)
            print(f"DEBUG: ⏳ Waiting before next {CHUNK_SECONDS}s sample...")
            time.sleep(8)

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, track=current_track, history=track_history)

@app.route("/api/nowplaying")
def nowplaying_api():
    return jsonify(current_track)

def start_flask():
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

def main():
    print("🎵 PyNowPlaying - Audio Track Recognition")
    print("=" * 50)
    print("🎯 Multiple Service Support for Long-term Sustainability")
    print("📡 Real-time track identification optimized for partial audio")
    print("=" * 50)
    
    # Clean up any leftover temporary files from previous runs
    cleanup_temp_files()
    
    # Show service status and recommendations
    print_service_status()
    
    device_index = find_input_device()
    print(f"\n✅ Using audio input device index: {device_index}")
    
    # Final test before starting (unless already tested in selector)
    print(f"\n{'='*50}")
    print("FINAL AUDIO TEST")
    print(f"{'='*50}")
    print("Starting final audio test before beginning detection...")
    print("Make sure your music is playing!")
    
    input("Press Enter when ready to test...")
    
    is_good = test_audio_source(device_index, test_seconds=3)
    
    if not is_good:
        print("\n❌ Final audio test failed!")
        retry_selector = input("Go back to device selector? (y/n): ").lower()
        if retry_selector == 'y':
            # Restart device selection
            main()
            return
        else:
            print("Continuing anyway...")
    else:
        print("\n✅ Final test passed! Starting music detection...")

    print(f"\n{'='*50}")
    print("🎵 STARTING MUSIC DETECTION")
    print(f"{'='*50}")
    print("🌐 Web interface: http://127.0.0.1:5000")
    print("🎯 Services: Shazam (primary) → AudD (trial) → AcoustID (fallback)")
    print(f"⏱️  Sample rate: {CHUNK_SECONDS} seconds")
    print("🛑 Press Ctrl+C to stop")
    print(f"{'='*50}")

    threading.Thread(target=start_flask, daemon=True).start()
    
    try:
        audio_loop(device_index)
    except KeyboardInterrupt:
        print("\n\n🛑 Stopping PyNowPlaying...")
        print("Goodbye!")

if __name__ == "__main__":
    main()
