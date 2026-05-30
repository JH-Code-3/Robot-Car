from picarx.llm import Ollama as LLM
from voice_active_car import VoiceActiveCar
from picarx.preset_actions import actions_dict, sounds_dict

# === Ollama settings ===
# If Ollama runs on this Pi, use "localhost".
# If it runs on another machine on your network, use that machine's IP.
OLLAMA_IP = "localhost"

# Recommended models (install with: ollama pull <model>)
#   llama3.2:3b   — fast, fits in 4GB RAM, good for conversation + commands
#   mistral:7b    — smarter but needs ~8GB RAM
#   llama3.2:1b   — fastest, smallest, less capable
OLLAMA_MODEL = "llama3.2:3b"

llm = LLM(ip=OLLAMA_IP, model=OLLAMA_MODEL)

# === Robot settings ===
NAME      = "Buddy"
TOO_CLOSE = 10       # cm — back up if obstacle closer than this

# TTS voice model (local Piper, no internet needed)
TTS_MODEL    = "en_US-ryan-low"
STT_LANGUAGE = "en-us"

# Keyboard control (type commands instead of speaking)
KEYBOARD_ENABLE = True

# Wake word — say this to activate the robot
WAKE_ENABLE  = True
WAKE_WORD    = [f"hey {NAME.lower()}"]
ANSWER_ON_WAKE = "Hi there!"

WELCOME = f"Hi, I'm {NAME}. Running on Ollama — no internet needed!"

INSTRUCTIONS = f"""
Your name is {NAME}.
You are a desktop-sized intelligent robot car called PiCar-X. You can move, perform actions, and make sounds. Keep responses short and friendly.

## Actions you can perform:
{list(actions_dict.keys())}

## Sounds you can make:
{list(sounds_dict.keys())}

## Response format
You MUST always respond in exactly this format:
RESPONSE_TEXT
ACTIONS: ACTION1, ACTION2, ...

If no action is needed, write:
ACTIONS: stop

## Style
- Short, cheerful, playful replies
- Speak from a robot's perspective
- Use humour when appropriate
"""

vac = VoiceActiveCar(
    llm,
    name=NAME,
    too_close=TOO_CLOSE,
    with_image=False,       # set True if using a vision-capable model like llava
    stt_language=STT_LANGUAGE,
    tts_model=TTS_MODEL,
    keyboard_enable=KEYBOARD_ENABLE,
    wake_enable=WAKE_ENABLE,
    wake_word=WAKE_WORD,
    answer_on_wake=ANSWER_ON_WAKE,
    welcome=WELCOME,
    instructions=INSTRUCTIONS,
)

if __name__ == '__main__':
    vac.run()
