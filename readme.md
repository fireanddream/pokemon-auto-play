# Summary

One night, I was watching TV while passively doing contests for a Flygon and kept getting stumped in dancing. I got fed up, so I set out to make this to have my revenge.

This is a script that tries to get every contest ribbon in Pokemon Platinum for the Pokemon in your party's first slot.
Supported ROMs are JPN, USA, EU (ENG), and CHN (Xzonn fan patch) for now.
It does NO memory writes and does NOT save the game.
Every change to your save file is made through normal gameplay.
If your Pokemon sucks at contests, it will automatically suck at contests at high speed when running this script.
Check out [buyable-poffin-guide.md](buyable-poffin-guide.md) for optimal contest stats from buyable Poffins if your Pokemon can still eat them.

## Requirements

- [Python](https://www.python.org/downloads/windows/) installed.
- [DeSmuME](https://github.com/TASEmulators/desmume/releases/tag/release_0_9_13) installed.
- Pokemon Platinum (JPN, USA, EU, or CHN) ROM and save file ready.
- Text speed set to FAST.
- In your save file, your character must be standing directly across the counter, facing the contest receptionist.
- The Pokemon in the first party slot must be healthy and have at least 2 moves; their types do not matter.
- If this is your first time using DeSmuME, use "Import Backup Memory..." to import your save file from real hardware.

## How to Run

1. Unzip and run `run_contest.py`.
2. Pick your DeSmuME and ROM locations.
3. If you need to change these locations later, edit the script manually.

To uninstall, delete `run_contest.py` and the `contest` folder. Nothing is hooked into DeSmuME or your save game.
The script runs the game at uncapped speed. If you close the emulator prematurely, restore the game speed and frame-rate limit in Config.

## AI disclaimer

This entire thing was written by GPT 6.1 Sol & Astra, lmao, buuuut I did manage to find it in my heart to go through weeks of testing :)
