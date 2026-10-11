# Atticus repository instructions

Atticus is a macOS menu-bar assistant: `Atticus.swift` owns native input and lifecycle, `atticus.py` runs the brain, and `ui/*.html` render in WKWebView.

Before changing UI, copy, motion or shortcut interactions, read `.agents/skills/atticus-design/SKILL.md` and apply its product design language. Keep changes small and use existing components and the standard library.

Hold to talk remains the default; press-to-toggle is optional. Preserve default-on built-in live commands and early AI thinking, with all AI tools and final answers gated behind transcript confirmation. Cancel, close and pause must stop mic, workers and voice.

Test with a scratch `ATTICUS_DIR`, fake app actions and a temporary Swift binary. Never use real apps, user windows or personal configuration for regression tests. Run `python3 -m unittest discover -s tests`, both self-tests and relevant UI fixture checks. `build.sh` installs/restarts the user's app; run it only when requested.

Commit product source, tests and this repository's design skill only. Do not commit runtime state, credentials, external skill symlinks or personal agent configuration. Keep the public mirror free of developer-local files.
