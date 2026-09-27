# CHEATVISION | VOD TESTER
### One-page how-to · Warzone · Printable sheet: [TESTER_QUICK_START.pdf](TESTER_QUICK_START.pdf)

> **Goal:** Drop a labeled video, get kill clips. Offline helper — **does not** change live detection.

Install and GitHub updates are on the other sheet: [TESTER_SETUP.md](TESTER_SETUP.md) / [TESTER_SETUP.pdf](TESTER_SETUP.pdf).

---

## 1 · Start, then drop

Double-click **`watch_vod_inbox.bat`**. Each video asks for the player tag, pre-filled with the last name — press OK to keep it, or type/select a different tag (for example if clip 5 is another player). Names are saved in a list. Keep original filenames.

| Drop video here | When | Clips saved as |
|---|---|---|
| `data\vod_inbox\clean\` | Verified-clean footage or misfires | `data\vod_dataset\clean\` |
| `data\vod_inbox\cheating\` | Confirmed-cheating kills only | `data\vod_dataset\suspicious\` |

Folder choice **is** the label. Misfire flags belong in **clean**. Do not mix unverified footage. The original stays in the inbox.

**Long VODs:** bottom-left HUD (player) → middle-left kill feed → mid-right elimination toast.

## 2 · Long VOD or short clip

Long recordings: default window is **10 sec before + 4 sec after** each distinct kill.

Short clips and misfires (about 20 seconds or less): used **as-is**. No kill feed required, and no gamer tag required. This is why “nothing found” no longer skips those files.

## 3 · Train, review, send

Training runs only if **both** clean and suspicious clips exist. Candidate: `data\models\candidates\`. **Not** loaded by live CheatVision; does not update main.

Before sharing: confirm the inbox folder, spot-check clips, send `data\vod_dataset\` on the agreed channel. Do not commit videos to Git.

---

### Troubleshooting

| Problem | Fix |
|---|---|
| Waiting on tag | Long VODs need the tag field filled once. Short misfires do not. |
| No clips from a long VOD | Check tag spelling. Bottom-left name and a kill indicator must be visible. |
| Watcher still running | Keep the window open until the current video finishes. |
