# Track Identification Setup Guide

## The Problem with AcoustID
AcoustID is designed for **full song fingerprinting**, not partial track recognition. It needs complete songs to work properly, making it unsuitable for real-time "now playing" detection.

## Better Alternatives

### 1. Shazam-like Identification (Recommended)
**Install shazamio library:**
```bash
pip install shazamio
```

**Pros:**
- Designed for partial track recognition (like the Shazam app)
- Works with short audio clips (10-15 seconds)
- Free to use
- Very accurate for popular music

**Cons:**
- Requires internet connection
- May not recognize very obscure tracks

### 2. AudD API (Alternative)
**No installation required** - uses web API

**Pros:**
- Free tier available (limited requests)
- Good accuracy
- Works with partial clips

**Cons:**
- Limited free requests
- Requires internet connection

### 3. AcoustID (Fallback)
Keep as fallback option for when you have full songs

## Current Configuration

The system now tries services in this order:
1. **Shazam** (if shazamio is installed)
2. **AudD API** (free tier)
3. **AcoustID** (if enabled as fallback)

## Installation Steps

1. **Install shazamio for best results:**
   ```bash
   pip install shazamio
   ```

2. **Run the updated script:**
   ```bash
   python pynowplaying.py
   ```

3. **Test with popular music:**
   - Play well-known songs
   - Let them play for 15+ seconds
   - Should get much better recognition rates!

## Configuration Options

In `pynowplaying.py`, you can adjust:

```python
USE_SHAZAM_API = True              # Enable Shazam identification
USE_ACOUSTID_FALLBACK = False      # Enable AcoustID as fallback
CHUNK_SECONDS = 15                 # Recording length (15s works well)
```

## Troubleshooting

If you get import errors:
```bash
# Install required packages
pip install shazamio requests sounddevice numpy pydub flask
```

If no tracks are identified:
- Ensure music is playing clearly
- Try popular/mainstream songs first
- Check audio levels are good
- Reduce background noise

## Why This Works Better

- **Shazam** and **AudD** are designed for partial audio recognition
- They use different algorithms optimized for real-time identification
- Much faster results (seconds vs minutes)
- Better accuracy for partial clips
- Designed for "now playing" use cases like yours!
