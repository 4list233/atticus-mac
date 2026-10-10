<p align="center"><img src="docs/banner.png" alt="Atticus. Your whole Mac, one sentence away." width="100%"></p>

<p align="center">
  Hold <kbd>⌃ control</kbd> <kbd>⌥ option</kbd> <kbd>space</kbd>, say what you need, let go.<br>
  Atticus moves your windows, finds your files, reads your screen and gets it done.
</p>

<p align="center">
  <img alt="macOS" src="https://img.shields.io/badge/macOS-1d1d1f?style=for-the-badge&logo=apple&logoColor=white">
  <img alt="Swift" src="https://img.shields.io/badge/Swift-1d1d1f?style=for-the-badge&logo=swift&logoColor=white">
  <img alt="Claude" src="https://img.shields.io/badge/Claude_Haiku-1d1d1f?style=for-the-badge&logo=anthropic&logoColor=white">
  <img alt="Cost" src="https://img.shields.io/badge/~0.14%C2%A2_per_ask-1d1d1f?style=for-the-badge">
</p>

<br>

https://github.com/user-attachments/assets/e3444422-02e8-4dd7-bb44-d39f2b0f2bef

<br>

## Just ask

| You say | Atticus |
|---|---|
| *"Code on my big screen, Chrome and Terminal on the laptop."* | Arranges three windows across two displays. |
| *"Open Spotify."* · *"Terminal on the left."* | Runs instantly, with no AI at all. |
| *"Find my notes about the Tokyo trip."* | Searches your files by what's inside them, opens the match on your other screen. |
| *"What's wrong with this code?"* | Looks at your screen and answers in a line. |
| *"Summarize this article in three bullets."* | Reads the page for you. |
| *"Reply: sounds good, see you at 7."* | Types it into the app you're in. It never sends. |
| *"Play my late night coding playlist on Spotify."* | Plays it in a second, no clicks. Any song, artist or playlist. |
| *"When I say focus mode, close Slack and open my to-do list."* | Remembers it. Next time, two words do it all. |

<p align="center"><img src="docs/demo.gif" alt="Atticus arranging windows across two screens" width="820"></p>

## Why Atticus

<table>
<tr>
<td width="33%" valign="top"><h3>No API key</h3>Runs on the Claude plan you already have, through <code>claude -p</code>. Nothing new to pay for.</td>
<td width="33%" valign="top"><h3>~700 asks for $1</h3>About 0.14¢ per ask in real use, priced at API list rates. Every ask is logged with its cost in the menu bar.</td>
<td width="33%" valign="top"><h3>Instant when it can be</h3>Opening apps and sites, moving windows, web searches and music skip the AI entirely. Everything else starts while you are still talking.</td>
</tr>
<tr>
<td valign="top"><h3>Private ears</h3>Apple's on-device speech turns your voice into text. Audio never leaves your Mac.</td>
<td valign="top"><h3>Your whole Mac</h3>Apps, windows, files, settings, the web and the screen, with <a href="https://github.com/openclaw/Peekaboo">Peekaboo</a> for clicking inside any app.</td>
<td valign="top"><h3>Safe by default</h3>Safe mode runs the AI's commands in Claude Code's sandbox: they can only write to a scratch folder. Before anything deletes, sends, buys or changes a file, Atticus asks you first. <code>rm</code>, <code>sudo</code>, disk tools and shutdown are always blocked. Every exact command is in the ask log.</td>
</tr>
</table>

Esc stops an answer. Pressing the keys again cancels it.

## Install

You need:
- **macOS 26** or later (Atticus uses Apple's new on-device speech)
- **A paid Claude plan** (Pro or Max) and [Claude Code](https://claude.com/claude-code). No Claude Code yet? The setup window installs it and signs you in.
- Apple's command line tools: `xcode-select --install`
- Optional: [Peekaboo](https://github.com/openclaw/Peekaboo) (see its install steps) so Atticus can click inside apps

Paste this in Terminal:

```
curl -fsSL https://raw.githubusercontent.com/SaarthurR/atticus-mac/main/install.sh | bash
```

It checks your Mac, downloads Atticus to `~/.atticus`, builds `~/Applications/Atticus.app`, and opens it. Atticus starts at login and updates itself from there (turn that off in Settings → About & Updates).

Working on the code instead? Clone the repo anywhere and run `./build.sh` after each change.

The first launch opens a setup window. It checks your Claude plan, then asks for each macOS permission.
After that, Atticus lives in the menu bar. Open it from Spotlight (⌘Space, "Atticus") for Settings: shortcut, sounds, model, speed, screen, memory and usage.
If you sign out of Claude later, the setup window comes back by itself.
To see it again: `defaults delete com.atticus.app onboarded`, then restart the app.

Test: `python3 atticus.py --selftest`. Remove: `launchctl bootout gui/$(id -u)/com.atticus.app`, then delete `~/Applications/Atticus.app`, `~/.atticus`, `~/.config/atticus` (your settings and history) and `~/Library/LaunchAgents/com.atticus.app.plist`.

## Usage count

Once a day, Atticus sends one random ID (made on your Mac, not tied to you) to PostHog, so I can see how many people use it. It never sends your name, your asks or your screen. Turn it off in Settings → Account & Access → Count me as a user.

## Files

- `Atticus.swift`: the app (hotkey, speech, overlay, onboarding window)
- `atticus.py`: the brain (fast commands, system prompt, helpers for windows, web and screen)
- `ui/ui.html`, `ui/onboarding.html`, `ui/settings.html`: the overlay, the welcome flow, and Settings
- `ui/AppIcon.icns`: the app icon
- `ui/orbs.js`: the [thinking-orbs](https://github.com/Jakubantalik/thinking-orbs) engine (MIT, Jakub Antalik)

## License

MIT. See `LICENSE`. `ui/orbs.js` is from [thinking-orbs](https://github.com/Jakubantalik/thinking-orbs), also MIT.
