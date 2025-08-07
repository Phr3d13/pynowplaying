import os
import sys
import time
import json
import threading
import subprocess
from datetime import datetime
from io import BytesIO
import asyncio

import sounddevice as sd
import numpy as np
from pydub import AudioSegment
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

# === CONFIG ===
ACOUSTID_API_KEY = 'YOUR_ACOUSTID_API_KEY_HERE'  # Get your free key from https://acoustid.org/
DEVICE_NAME_CONTAINS = "Analogue 3 + 4"  # substring of your input device name, adjust as needed
CHUNK_SECONDS = 15                  # length of each audio chunk for track identification
SAMPLE_RATE = 48000                # sample rate (Hz) - changed to 48kHz
CHANNELS = 2                       # stereo
FP_CALC_PATH = 'fpcalc.exe'        # full path to fpcalc CLI tool if not in PATH
DEBUG_SAVE_AUDIO = False            # Save audio samples for debugging

# Track identification service selection
USE_AUDD_API = True                # Use AudD API (free, no signup required) - PRIMARY
USE_SHAZAM_API = False             # Use Shazam-like identification (requires shazamio)
USE_ACOUSTID_FALLBACK = False      # Fallback to AcoustID for full songs
USE_MUSICBRAINZ_DIRECT = True      # Query MusicBrainz directly for enhanced metadata

# Shazam API configuration (free tier available)
RAPIDAPI_KEY = "YOUR_RAPIDAPI_KEY_HERE"  # Get from RapidAPI for Shazam service

# === GLOBAL STATE ===
current_track = {"artist": "", "title": "", "time": ""}
track_history = []

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
        body { background: #111; color: #eee; font-family: Arial, sans-serif; padding: 20px; }
        h1 { font-size: 2em; }
        #current { margin-bottom: 20px; }
        #history { font-size: 0.9em; max-height: 300px; overflow-y: auto; }
        .track { margin-bottom: 5px; }
        .timestamp { color: #666; margin-right: 10px; }
    </style>
    <meta http-equiv="refresh" content="5" />
</head>
<body>
    <div id="current">
        <h1>Now Playing:</h1>
        {% if track.artist and track.title %}
          <p><strong>{{ track.artist }} - {{ track.title }}</strong></p>
          <p><em>Started at {{ track.time }}</em></p>
        {% else %}
          <p><em>No track detected yet</em></p>
        {% endif %}
    </div>
    <div id="history">
        <h2>History</h2>
        {% for t in history %}
            <div class="track">
                <span class="timestamp">{{ t.time }}</span>
                <span>{{ t.artist }} - {{ t.title }}</span>
            </div>
        {% endfor %}
    </div>
</body>
</html>
"""

def get_windows_default_input_device():
    """Get the Windows default audio input device"""
    try:
        # sounddevice uses the default device when device=None
        # We can get the default device info
        default_device = sd.query_devices(kind='input')
        if default_device:
            print(f"DEBUG: Windows default input device: {default_device['name']}")
            return default_device
        return None
    except Exception as e:
        print(f"DEBUG: Could not get default device: {e}")
        return None

def find_default_device_index():
    """Find the index of the Windows default input device"""
    try:
        default_device_info = get_windows_default_input_device()
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

def find_input_device():
    global SAMPLE_RATE
    devices = sd.query_devices()
    
    # First, try to find the Windows default input device
    default_device_index = find_default_device_index()
    
    print(f"DEBUG: Looking for device containing '{DEVICE_NAME_CONTAINS}' with {SAMPLE_RATE}Hz support")
    if default_device_index is not None:
        default_device = devices[default_device_index]
        print(f"DEBUG: Windows default input device: [{default_device_index}] {default_device['name']}")
    
    print("DEBUG: Available input devices:")
    input_devices = []
    
    print("\nDEBUG: === DETAILED DEVICE ANALYSIS ===")
    for i, dev in enumerate(devices):
        if dev['max_input_channels'] > 0:
            print(f"\nDevice [{i}]: {dev['name']}")
            print(f"  Type: {'🎤 MICROPHONE' if 'microphone' in dev['name'].lower() or 'mic' in dev['name'].lower() else '🔊 LINE/LOOPBACK' if any(x in dev['name'].lower() for x in ['stereo mix', 'what u hear', 'loopback', 'mix', 'wave out']) else '❓ UNKNOWN'}")
            print(f"  Channels: {dev['max_input_channels']} input, {dev['max_output_channels']} output")
            print(f"  Default Rate: {dev['default_samplerate']} Hz")
            print(f"  Host API: {sd.query_hostapis(dev['hostapi'])['name']}")
            
            # Check if device supports our target sample rate
            supports_target_rate = check_device_sample_rate(i, SAMPLE_RATE)
            rate_indicator = "✅" if supports_target_rate else "❌"
            
            # Mark if this is the Windows default device
            default_indicator = " 🎯 [WINDOWS DEFAULT]" if i == default_device_index else ""
            
            print(f"  {SAMPLE_RATE}Hz Support: {rate_indicator}")
            print(f"  Status: {default_indicator}")
            
            # Only add devices that support our target sample rate
            if supports_target_rate:
                input_devices.append((i, dev))
            else:
                print(f"  ⚠️  Skipping - doesn't support {SAMPLE_RATE}Hz")
    
    print(f"\nDEBUG: === END DEVICE ANALYSIS ===")
    print(f"\n💡 HINT: For music detection, you typically want:")
    print(f"   - 'Stereo Mix' or 'What U Hear' (captures what's playing)")
    print(f"   - NOT a microphone (captures external sound)")
    print(f"   - A loopback device from your audio interface")
    
    print(f"\nDEBUG: Found {len(input_devices)} input devices supporting {SAMPLE_RATE}Hz")
    
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
                    default_indicator = " 🎯 [WINDOWS DEFAULT]" if i == default_device_index else ""
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
    # 1. Windows default device that supports sample rate
    # 2. Device matching DEVICE_NAME_CONTAINS
    # 3. First available device
    
    auto_selected = None
    selection_reason = ""
    
    # Check if Windows default device supports our sample rate
    if default_device_index is not None:
        for i, dev in input_devices:
            if i == default_device_index:
                auto_selected = i
                selection_reason = f"Windows default input device"
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
        use_auto = input(f"Use auto-selected device [{auto_selected}]? (y/n/s for selector): ").lower()
        if use_auto == 'y':
            return auto_selected
        elif use_auto == 'n':
            print("Exiting...")
            sys.exit(1)
    else:
        use_selector = input("No device auto-selected. Use interactive selector? (y/n): ").lower()
        if use_selector != 'y':
            print("Exiting...")
            sys.exit(1)
    
    return select_audio_device(input_devices)

def select_audio_device(input_devices):
    """Interactive audio device selector with testing capability"""
    # Get Windows default device index for highlighting
    default_device_index = find_default_device_index()
    
    while True:
        print(f"\n{'='*60}")
        print(f"INTERACTIVE AUDIO DEVICE SELECTOR ({SAMPLE_RATE}Hz)")
        print(f"{'='*60}")
        print(f"Available input devices (all support {SAMPLE_RATE}Hz):")
        
        for idx, (device_index, dev) in enumerate(input_devices):
            default_indicator = " 🎯 [WINDOWS DEFAULT]" if device_index == default_device_index else ""
            print(f"  {idx + 1}. [{device_index}] {dev['name']}{default_indicator}")
            print(f"      Channels: {dev['max_input_channels']}, Default Rate: {dev['default_samplerate']} Hz")
        
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
                    default_indicator = " (Windows Default)" if device_index == default_device_index else ""
                    print(f"\nTesting device [{device_index}]: {dev['name']}{default_indicator} at {SAMPLE_RATE}Hz")
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
                default_indicator = " (Windows Default)" if device_index == default_device_index else ""
                print(f"\nSelected device [{device_index}]: {dev['name']}{default_indicator} ({SAMPLE_RATE}Hz)")
                
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
                result = await shazam.recognize_song(temp_filename)
                
                if result and 'track' in result:
                    track = result['track']
                    artist = track.get('subtitle', 'Unknown Artist')
                    title = track.get('title', 'Unknown Title')
                    
                    if artist != 'Unknown Artist' and title != 'Unknown Title':
                        print(f"DEBUG: Shazam identified: {artist} - {title}")
                        return {'artist': artist, 'title': title, 'source': 'shazam'}
                
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
                    'api_token': 'test',  # Free tier - no signup required!
                    'return': 'apple_music,spotify'  # Get additional metadata
                }
                
                print(f"DEBUG: Uploading to AudD API (free tier)...")
                response = requests.post(url, files=files, data=data, timeout=30)
                response.raise_for_status()
                
                result = response.json()
                print(f"DEBUG: AudD API response status: {result.get('status')}")
                
                if result.get('status') == 'success' and result.get('result'):
                    track_info = result['result']
                    artist = track_info.get('artist', 'Unknown Artist')
                    title = track_info.get('title', 'Unknown Title')
                    
                    if artist != 'Unknown Artist' and title != 'Unknown Title':
                        print(f"DEBUG: ✅ AudD identified: {artist} - {title}")
                        
                        # Check for additional metadata
                        if 'apple_music' in track_info:
                            print(f"DEBUG: Additional info: Apple Music URL available")
                        if 'spotify' in track_info:
                            print(f"DEBUG: Additional info: Spotify info available")
                        
                        return {'artist': artist, 'title': title, 'source': 'audd'}
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
    
    # Service priority order - AudD first since it's working well and free
    services = []
    
    # Primary: AudD - no signup, no API key, works great!
    if USE_AUDD_API:
        services.append(("AudD", lookup_audd_api))
    
    # Secondary: Shazam (if available)
    if SHAZAMIO_AVAILABLE and USE_SHAZAM_API:
        services.append(("Shazam", lookup_shazam))
    
    # Fallback: AcoustID (for full songs only)
    if USE_ACOUSTID_FALLBACK:
        services.append(("AcoustID", lambda data: lookup_acoustid_from_wav(data)))
    
    for service_name, service_func in services:
        print(f"DEBUG: Trying {service_name}...")
        try:
            result = service_func(wav_data)
            if result and result.get('artist') and result.get('title'):
                print(f"DEBUG: ✅ {service_name} success: {result['artist']} - {result['title']}")
                return result
            else:
                print(f"DEBUG: ❌ {service_name} - no match")
        except Exception as e:
            print(f"DEBUG: ❌ {service_name} error: {e}")
    
    print("DEBUG: No services could identify the track")
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
            return {'artist': artist, 'title': title, 'mbid': mbid}
        
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
                
                if artist and title:
                    print(f"DEBUG: Selected AcoustID match: {artist} - {title}")
                    
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
                    
                    return {'artist': artist, 'title': title, 'mbid': mbid}
                    
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
    global current_track, track_history
    print("DEBUG: Starting audio loop...")
    print(f"DEBUG: Recording {CHUNK_SECONDS}s chunks for track identification")
    print(f"DEBUG: Primary service: AudD API (free, no signup required)")
    
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
            if track_changed(track_info):
                now_str = datetime.now().strftime("%H:%M:%S")
                source = track_info.get('source', 'unknown')
                print(f"DEBUG: 🎵 Track changed: {track_info['artist']} - {track_info['title']} at {now_str} (via {source})")
                current_track = {
                    "artist": track_info['artist'],
                    "title": track_info['title'],
                    "time": now_str
                }
                track_history.insert(0, current_track.copy())
                # Keep last 20 tracks
                track_history = track_history[:20]
            else:
                source = track_info.get('source', 'unknown')
                print(f"DEBUG: 🔄 Same track detected: {track_info['artist']} - {track_info['title']} (via {source})")
        else:
            print("DEBUG: ❌ No match found across all services.")
            print("DEBUG: 💡 Tips for better recognition:")
            print("DEBUG:   - Ensure music is playing clearly (not paused)")
            print("DEBUG:   - Try popular/mainstream songs (better database coverage)")
            print("DEBUG:   - Check audio levels are good (not too quiet/loud)")
            print("DEBUG:   - Reduce background noise and talking")
        
        # Wait before next sample
        print(f"DEBUG: ⏳ Waiting before next {CHUNK_SECONDS}s sample...")
        time.sleep(3)

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
    print("🎯 Primary Service: AudD API (free, no signup required!)")
    print("📡 Real-time track identification optimized for partial audio")
    print("=" * 50)
    
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
    print("🎯 Service: AudD API (free tier)")
    print("⏱️  Sample rate: 15 seconds")
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
