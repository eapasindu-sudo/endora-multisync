# Endora MultiSync

Syncs up to 20 cameras and a Zoom recorder for wedding films, with exports for Premiere Pro and DaVinci Resolve.

## Releasing an update

1. Commit the change and push to `main`, with a commit message that says what changed.
   That message becomes the "What's new" text people see.
2. GitHub Actions builds the Windows installer and publishes it as release `v<VERSION>.<build number>`
   on this repo's Releases page.
3. Every installed copy checks for a new release a few seconds after it starts and offers
   **Update now**. It downloads the installer, closes, installs silently and opens again.

To start a new minor line (1.2.x, 2.0.x), change the `VERSION` file.

## One-time setup

- This repo must stay public so the app can download updates without a password.
- The pre-release tagged `tools` holds `ffbin.zip` (the `bin` folder with `ffmpeg.exe`,
  `ffprobe.exe` and their DLLs). The build adds it to every installer.
