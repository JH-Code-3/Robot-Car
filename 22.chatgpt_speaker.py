from picarx.llm import OpenAI as LLM
from picarx.tts import Piper
from picarx.music import Music
from secret import OPENAI_API_KEY
import wave
import struct
import math
import tempfile
import os
import time

# === TTS setup (local Piper voice) ===
tts = Piper()
tts.set_model("en_US-ryan-low")

# === Music/sound setup ===
music = Music()
music.music_set_volume(80)

# === ChatGPT setup ===
llm = LLM(api_key=OPENAI_API_KEY, model="gpt-4o-mini")
llm.set_instructions("You are a helpful assistant. Keep responses concise and conversational.")
llm.set_max_messages(20)

def play_goodbye_tune():
    notes = [523, 659, 784, 1047]  # C5, E5, G5, C6
    sample_rate = 44100
    duration = 0.25
    samples = []
    for freq in notes:
        for i in range(int(sample_rate * duration)):
            envelope = math.sin(math.pi * i / (sample_rate * duration))
            val = int(32767 * envelope * math.sin(2 * math.pi * freq * i / sample_rate))
            samples.append(val)
    tmp = tempfile.mktemp(suffix=".wav")
    with wave.open(tmp, 'w') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    music.sound_play(tmp)
    time.sleep(len(notes) * duration + 0.2)
    os.remove(tmp)

WELCOME = "Hello! Type something and I'll respond out loud. Type 'volume 50' (0-100) to adjust volume. Press Ctrl+C to quit."
print(WELCOME)
tts.say(WELCOME)

try:
    while True:
        user_input = input("\n>>> ").strip()
        if not user_input:
            continue

        # Volume command: "volume 70"
        parts = user_input.lower().split()
        if len(parts) == 2 and parts[0] == "volume" and parts[1].isdigit():
            vol = max(0, min(100, int(parts[1])))
            music.music_set_volume(vol)
            print(f"Volume set to {vol}")
            continue

        # ChatGPT response
        response_text = ""
        print("Assistant: ", end="", flush=True)
        for chunk in llm.prompt(user_input, stream=True):
            if chunk:
                print(chunk, end="", flush=True)
                response_text += chunk
        print()

        if response_text:
            tts.say(response_text)

except KeyboardInterrupt:
    print("\nGoodbye!")
    play_goodbye_tune()
