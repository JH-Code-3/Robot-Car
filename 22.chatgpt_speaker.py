from picarx.llm import OpenAI as LLM
from picarx.tts import Piper
from secret import OPENAI_API_KEY

# === TTS setup (local Piper voice, no API key needed) ===
tts = Piper()
tts.set_model("en_US-ryan-low")

# Optional: swap to OpenAI TTS for higher quality
# from picarx.tts import OpenAI_TTS
# tts = OpenAI_TTS(api_key=OPENAI_API_KEY)
# tts.set_model("gpt-4o-mini-tts")
# tts.set_voice("alloy")

# === ChatGPT setup ===
llm = LLM(api_key=OPENAI_API_KEY, model="gpt-4o-mini")
llm.set_instructions("You are a helpful assistant. Keep responses concise and conversational.")
llm.set_max_messages(20)

WELCOME = "Hello! Type something and I'll respond out loud. Press Ctrl+C to quit."
print(WELCOME)
tts.say(WELCOME)

try:
    while True:
        user_input = input("\n>>> ").strip()
        if not user_input:
            continue

        # Collect streamed response
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
    tts.say("Goodbye!")
