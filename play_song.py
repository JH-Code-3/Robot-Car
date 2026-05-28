#!/usr/bin/env python3

from pathlib import Path
from time import sleep
from picarx.music import Music
import readchar


# This looks for the song inside the music folder.
SONG_FILE = Path(__file__).parent / "music" / "song.mp3"

VOLUME = 50


def main():
    music = Music()
    music.music_set_volume(VOLUME)

    if not SONG_FILE.exists():
        print("Song file not found.")
        print(f"Expected location: {SONG_FILE}")
        print("Put your MP3 file inside the music folder and name it song.mp3")
        return

    print("PiCar-X Music Player")
    print("--------------------")
    print("P = Play song")
    print("S = Stop song")
    print("Q = Quit")
    print()

    while True:
        key = readchar.readkey().lower()

        if key == "p":
            print("Playing song...")
            music.music_play(str(SONG_FILE))

        elif key == "s":
            print("Stopping song...")
            music.music_stop()

        elif key == "q":
            print("Stopping music and exiting...")
            music.music_stop()
            sleep(0.2)
            break

        else:
            print("Press P to play, S to stop, or Q to quit.")


if __name__ == "__main__":
    main()
