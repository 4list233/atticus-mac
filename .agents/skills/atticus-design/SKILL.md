---
name: atticus-design
description: Preserve Atticus's premium macOS design language when changing its overlay, Settings, onboarding, or product site in this repository.
---

# Atticus design

This skill belongs to this repository only. Use the existing UI as the reference: `ui/settings.html` for controls, `ui/ui.html` for the assistant, `ui/onboarding.html` for setup, and `Atticus.swift` for native behavior. Read the relevant surface before changing it.

## Visual language

- Use the system font. Settings use 13px body text, 24px headings with tight tracking, 11.5px supporting text; emphasize through weight and spacing before adding color.
- Reuse the existing CSS tokens (`--fg`, `--fg2`, `--fg3`, `--card`, `--line`, `--accent`, `--track`). Settings already adapt to light and dark appearances; the overlay stays dark. Do not hardcode a new palette.
- Reuse `.group`, `.row`, `.t`, `.seg`, `.sw`, `.btn`, and keycaps. Settings cards have 11px corners, fine half-point borders and restrained shadows. Use blue for the main action, green for enabled switches, orange/red only for meaningful warnings.
- Keep one clear title, a short explanation, and one appropriate control per choice. Segmented controls suit mutually exclusive modes; switches suit independent on/off preferences. Show the selected state to accessibility APIs and retain visible keyboard focus.
- Keep surfaces quiet: no decorative gradients, extra badges, oversized headings, competing accent colors, or additional animation libraries. The existing orb is the visual signature.

## Interaction language

- Describe the next action in ordinary words: “Press again to send,” “Release keys to send,” “Esc cancels.” Say “shortcut,” not a fixed key combination, unless displaying the actual configured keys.
- Hold mode sends on release. Toggle mode sends on the second press and ignores releases. Pressing during an answer cancels it; during voice playback it stops the voice. Esc and closing cancel without sending.
- AI may think before submission, but every AI tool and final answer waits for the confirmed transcript. Supported built-in live actions remain default-on; learned shell shortcuts wait for confirmation. Never make a pause or recording timeout submit a toggle request.
- Show one listening label. Keep listening feedback accurate while speculative AI reasoning runs. Reveal the transcript only once it contains words. Never discard typed text because an answer is still running.
- Feedback follows the input immediately. Reuse existing short fades/easing; transitions must remain interruptible. Respect Reduce Motion, display bounds, long text and reachable controls after resize. Do not move or focus the user's apps merely to test UI.

## Verification

Use scratch `ATTICUS_DIR` state and fake action workers for regression checks. Run relevant offline tests and a temporary native build; do not install or run `build.sh` without authorization. Use `tests/ui.html` and `tests/settings.html` to inspect the mock-bridge UI, including narrow layouts and keyboard navigation. Report measured timings as measurements and demo timings as illustrations.

Keep runtime settings, transcripts, learned commands, licenses, credentials and personal agent skills out of source control. This self-contained product design skill is the only shared agent skill; do not link to a developer's home directory.
