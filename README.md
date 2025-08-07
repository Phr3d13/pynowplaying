# PyNowPlaying 🎵

Real-time audio track identification system that recognizes music playing on your computer and displays it on a web interface. Uses multiple services including AudD API (free, no signup required) for partial track recognition.

## ✨ Features

- **Real-time Music Recognition**: Identifies tracks as they play using 15-second audio samples
- **Multiple Recognition Services**: AudD API (primary), Shazam, and AcoustID fallback
- **Interactive Device Selection**: Choose from available audio input devices with testing
- **Visual Audio Feedback**: VU meters and waveforms to verify audio capture
- **Web Interface**: Clean web dashboard showing current track and history
- **Windows Audio Integration**: Automatic detection of Windows default audio devices
- **Free Tier Friendly**: Primary service (AudD) requires no signup or API keys

## 🚀 Quick Start

### Prerequisites

1. **Python 3.7+** installed on your system
2. **Windows OS** (tested on Windows, may work on other platforms)
3. **Audio playing** on your computer (music streaming, local files, etc.)

### Installation

1. **Clone or download** this project to your computer

2. **Install Python dependencies**:
   ```bash
   pip install sounddevice numpy pydub requests flask
   ```

3. **Install optional Shazam support** (recommended):
   ```bash
   pip install shazamio
   ```

4. **Download audio tools**:
   - Download `fpcalc.exe` from [AcoustID](https://acoustid.org/chromaprint) and place it in the project folder
   - Download `ffmpeg.exe` from [FFmpeg](https://ffmpeg.org/download.html) and place it in the project folder

### Running the Application

1. **Start music playing** on your computer
2. **Run the script**:
   ```bash
   python pynowplaying.py
   ```
3. **Follow the interactive setup**:
   - The app will detect and list available audio input devices
   - Test devices to find the one capturing your music
   - Choose the device that shows good audio levels
4. **Open the web interface**: http://127.0.0.1:5000

## 🎯 Audio Device Selection

### What Device Should I Choose?

For music recognition, you need a device that captures **what's playing on your computer**, not external sounds:

- ✅ **"Stereo Mix"** - Captures all computer audio
- ✅ **"What U Hear"** - Same as Stereo Mix (Creative cards)
- ✅ **Audio interface loopback** - If using external audio equipment
- ❌ **Microphone** - Captures room audio, not direct computer output

### Enabling Stereo Mix (Windows)

If "Stereo Mix" isn't available:

1. Right-click the **speaker icon** in system tray
2. Select **"Open Sound settings"**
3. Scroll down and click **"Sound Control Panel"**
4. Go to **"Recording"** tab
5. Right-click in empty space → **"Show Disabled Devices"**
6. Right-click **"Stereo Mix"** → **"Enable"**
7. Set it as **default recording device** if desired

## 🔧 Configuration

Edit the configuration section in `pynowplaying.py`:

```python
# === CONFIG ===
ACOUSTID_API_KEY = '5LbMXJzpel'        # Free AcoustID key (works as-is)
DEVICE_NAME_CONTAINS = "Analogue 3 + 4"  # Adjust to your device name
CHUNK_SECONDS = 15                     # Audio sample length
SAMPLE_RATE = 48000                    # Audio sample rate
CHANNELS = 2                           # Stereo audio
DEBUG_SAVE_AUDIO = False               # Save audio files for debugging

# Service selection
USE_AUDD_API = True                    # Primary service (free, recommended)
USE_SHAZAM_API = False                 # Secondary service (requires shazamio)
USE_ACOUSTID_FALLBACK = False          # Fallback for full songs only
```

## 🌐 Web Interface

The web interface runs at **http://127.0.0.1:5000** and shows:

- **Current Track**: Currently playing song with timestamp
- **History**: Last 20 identified tracks
- **Auto-refresh**: Updates every 5 seconds

### API Endpoint

Get current track data in JSON format:
```
GET http://127.0.0.1:5000/api/nowplaying
```

Response:
```json
{
  "artist": "Artist Name",
  "title": "Song Title", 
  "time": "14:30:25"
}
```

## 🎵 Recognition Services

### AudD API (Primary) ⭐

- **Free tier**: No signup required
- **Best for**: Partial track recognition (15-second clips)
- **Coverage**: Large music database
- **Setup**: Works out of the box

### Shazam (Optional)

- **Requirements**: `pip install shazamio`
- **Best for**: Popular music recognition
- **Setup**: Enable with `USE_SHAZAM_API = True`

### AcoustID (Fallback)

- **Best for**: Full song fingerprinting
- **Limitation**: Requires longer audio samples (30+ seconds)
- **Setup**: Enable with `USE_ACOUSTID_FALLBACK = True`

## 🛠️ Troubleshooting

### No Audio Detected

1. **Check device selection**: Make sure you selected a device that captures computer audio
2. **Test audio levels**: Use the built-in device tester to verify audio is being captured
3. **Enable Stereo Mix**: See "Enabling Stereo Mix" section above
4. **Check volume levels**: Ensure music is playing at reasonable volume

### No Track Recognition

1. **Try popular songs**: Database coverage is better for mainstream music
2. **Reduce background noise**: Close other audio sources, talking, etc.
3. **Check audio quality**: Ensure clear, uninterrupted music playback
4. **Wait for song portions**: Recognition works better during verses/chorus vs. instrumental breaks

### Sample Rate Issues

1. **Try different sample rates**: Edit `SAMPLE_RATE` to 44100 or 22050
2. **Check device compatibility**: Some devices only support specific sample rates
3. **Use device tester**: The interactive selector tests sample rate compatibility

### Installation Issues

**Missing packages**:
```bash
pip install --upgrade pip
pip install sounddevice numpy pydub requests flask shazamio
```

**Audio backend issues** (Windows):
```bash
pip install sounddevice --upgrade
```

## 📁 File Structure

```
pynowplaying/
├── pynowplaying.py      # Main application
├── fpcalc.exe          # Audio fingerprinting tool
├── ffmpeg.exe          # Audio processing (optional)
└── README.md           # This file
```

## 🔍 Debug Mode

Enable debug features for troubleshooting:

```python
DEBUG_SAVE_AUDIO = True  # Saves audio samples for manual inspection
```

This creates `debug_audio_*.wav` files you can play to verify correct audio capture.

## ⚡ Performance Tips

1. **Close unnecessary programs** to reduce system load
2. **Use wired connections** instead of Bluetooth when possible
3. **Ensure stable internet** for API calls
4. **Keep music volume consistent** for better recognition

## 🤝 Contributing

This project uses multiple free APIs for music recognition. Consider:

- Testing with different music genres and sources
- Reporting issues with specific audio device configurations
- Suggesting improvements for the web interface

## 📋 System Requirements

- **OS**: Windows (tested), macOS/Linux (may work)
- **Python**: 3.7 or higher
- **RAM**: 512MB+ available
- **Internet**: Required for music recognition APIs
- **Audio**: Computer audio output/input device

## 🎯 Use Cases

- **Radio stations**: Track what's currently playing
- **Music discovery**: Identify unknown songs
- **Streaming analysis**: Log music listening history
- **Party playlists**: Keep track of played songs
- **Research**: Analyze music consumption patterns

## 📞 Support

If you encounter issues:

1. **Check the debug output** in the terminal for error messages
2. **Test different audio devices** using the interactive selector
3. **Verify internet connectivity** for API access
4. **Try popular, mainstream songs** for initial testing
5. **Check Windows audio settings** for device availability

---

**Note**: This software is for personal use and respects music recognition service terms of use. AudD API usage is free for reasonable personal use.
