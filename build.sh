#!/bin/bash
# Installs Atticus: builds Atticus.app, starts it at login, and (re)starts it now. Run it again after any change.
set -e; cd "$(dirname "$0")"
[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 26 ] || { echo "Atticus needs macOS 26 or later (it uses Apple's new on-device speech)."; exit 1; }
command -v swiftc >/dev/null || { echo "Needs Apple's command line tools. Run: xcode-select --install   then run ./build.sh again."; exit 1; }
command -v claude >/dev/null || [ -x ~/.local/bin/claude ] || echo "Note: Claude Code is not installed yet. Atticus's setup window will install it and sign you in."
APP=~/Applications/Atticus.app   # in Applications so System Settings can list it for permissions
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/ui"
cp atticus.py "$APP/Contents/Resources/"
cp ui/*.html ui/*.js "$APP/Contents/Resources/ui/"
cat > "$APP/Contents/Info.plist" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.atticus.app</string>
<key>CFBundleName</key><string>Atticus</string>
<key>CFBundleExecutable</key><string>Atticus</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
<key>NSMicrophoneUsageDescription</key><string>Atticus listens while you hold Control+Option+Space.</string>
<key>NSAppleEventsUsageDescription</key><string>Atticus controls apps like Finder, Chrome and Music when you ask it to.</string>
<key>NSSpeechRecognitionUsageDescription</key><string>Atticus turns your words into text on this Mac.</string>
</dict></plist>
P
swiftc -O -target "$(uname -m)-apple-macos26.0" Atticus.swift -o "$APP/Contents/MacOS/Atticus"
# With a code-signing identity named "Atticus Local Signing" in the keychain, macOS keeps permissions across
# rebuilds; without one it signs ad hoc, and permissions must be granted again after each build.
ID=$(security find-identity -p codesigning | awk '/Atticus Local Signing/{print $2; exit}')
codesign -s "${ID:--}" --force "$APP"
# register with macOS, or System Settings can't list it for permissions
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
AGENT=~/Library/LaunchAgents/com.atticus.app.plist   # starts at login, restarts if it quits
mkdir -p ~/Library/LaunchAgents ~/.config/atticus
cat > "$AGENT" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.atticus.app</string>
<key>ProgramArguments</key><array><string>$APP/Contents/MacOS/Atticus</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>$HOME/.config/atticus/app.err</string>
</dict></plist>
P
launchctl kickstart -k gui/$(id -u)/com.atticus.app 2>/dev/null || launchctl bootstrap gui/$(id -u) "$AGENT"
echo "Atticus is running. Hold Control+Option+Space and talk."
