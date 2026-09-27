# CHEATVISION | TESTER SETUP
### One-page install and update · Printable sheet: [TESTER_SETUP.pdf](TESTER_SETUP.pdf)

> How to install CheatVision and pick up GitHub updates. VOD drop-and-clip steps are on the other sheet: [TESTER_QUICK_START.md](TESTER_QUICK_START.md) / [TESTER_QUICK_START.pdf](TESTER_QUICK_START.pdf).

---

## 1 · First time — `setup.bat`

1. Install **Python 3.11 or newer** from https://www.python.org/downloads/ and tick **Add python.exe to PATH**.
2. Clone the repo (needed later for updates):

```powershell
git clone https://github.com/SensoredRooster/CheatVision.git
```

3. Open the CheatVision folder and double-click **`setup.bat`**.
4. Answer the GPU and player-detector prompts (Y is the usual choice).

`setup.bat` creates `.venv` and installs the app, training extras, VOD OCR, ffmpeg if missing, and the optional player detector. You do not need to run pip yourself.

A ZIP download works for a one-off install, but **`update.bat` cannot pull GitHub changes unless this folder is a git clone**.

## 2 · Later — `update.bat`

Whenever you want the latest from GitHub, double-click **`update.bat`**.

It fast-forwards to `origin/main`, then refreshes app packages, training extras, and VOD OCR. Inbox videos and extracted clips are **not** in git and stay on your PC.

If the pull fails, local file changes may be in the way. Leave those files as they are and tell the project owner.

## 3 · After setup or update

| Start | File |
|---|---|
| Live CheatVision app | `run.bat` |
| VOD kill-clip watcher | `watch_vod_inbox.bat` |

If something looks missing, run `.venv\Scripts\python.exe tools\setup_check.py`.

---

### Troubleshooting

| Problem | Fix |
|---|---|
| Python not found | Reinstall Python and tick **Add python.exe to PATH**, then run `setup.bat` again. |
| Not a git clone | Clone the repo. ZIP folders cannot use `update.bat`. |
| Pull failed | Do not reset. Contact the project owner; your clips were not deleted. |
| ffmpeg missing after install | Close the window and open a new one, then run `ffmpeg -version`. |
