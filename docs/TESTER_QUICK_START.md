# CHEATVISION | VOD TESTER
### One-page how-to · Warzone · Printable sheet: [TESTER_QUICK_START.pdf](TESTER_QUICK_START.pdf)

> **Goal:** Drop a labeled video, type the watched player’s tag, get kill clips. Offline helper — **does not** change live detection.

Install and GitHub updates are on the other sheet: [TESTER_SETUP.md](TESTER_SETUP.md) / [TESTER_SETUP.pdf](TESTER_SETUP.pdf).

---

## 1 · Start, then drop

Double-click **`watch_vod_inbox.bat`** and leave it open **before** copying files (it only sees new videos). When the copy finishes, enter the watched player’s exact in-game tag in the popup.

| Drop video here | When | Clips saved as |
|---|---|---|
| `data\vod_inbox\clean\` | Verified-clean footage only | `data\vod_dataset\clean\` |
| `data\vod_inbox\cheating\` | Confirmed-cheating kills only | `data\vod_dataset\suspicious\` |

Folder choice **is** the label. Do not mix unverified footage. The original stays in the inbox.

**What it reads:** bottom-left HUD (player identity) → middle-left kill feed (that player as killer) → mid-right elimination toast (fallback).

## 2 · Long VOD or short clip

Same scan either way. Default window is **10 sec before + 4 sec after** each distinct kill. A short file uses whatever footage it has.

If the kill feed / toast is already gone, **no clip is created** — the original stays in the inbox. That avoids labeling unrelated footage as a kill.

## 3 · Train, review, send

Training runs only if **both** clean and suspicious clips exist. Candidate: `data\models\candidates\`. **Not** loaded by live CheatVision; does not update main.

Before sharing: confirm the inbox folder, spot-check clips for OCR mistakes, send `data\vod_dataset\` on the agreed channel. Do not commit videos to Git.

---

### Troubleshooting

| Problem | Fix |
|---|---|
| No popup | Watcher open first; copy a *new* file into `clean` or `cheating`. Cancelled prompt: copy again with a new filename. |
| No clips | Check tag spelling. Bottom-left name and a kill indicator must be visible. |
| Watcher still running | Keep the window open until the current video finishes. |
