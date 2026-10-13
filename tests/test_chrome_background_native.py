"""Opt-in Chrome integration: only uniquely titled pages in a fresh --user-data-dir."""
import json
import os
import plistlib
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import atticus

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')


@unittest.skipUnless(sys.platform == 'darwin' and CHROME.exists() and os.environ.get('ATTICUS_CHROME_FIXTURE') == '1', 'Opt-in isolated Chrome profile')
class ChromeBackgroundTests(unittest.TestCase):
    def test_pinned_background_window_javascript_and_ax(self):
        with tempfile.TemporaryDirectory(prefix='atticus-chrome-') as tmp:
            root = Path(tmp); profile = root / 'profile'; (profile / 'Default').mkdir(parents=True)
            prefs = {'browser':{'allow_javascript_apple_events':True}}
            (profile / 'Default/Preferences').write_text(json.dumps(prefs)); (profile / 'Local State').write_text(json.dumps(prefs))
            marker = 'ATTICUS SCRATCH ' + root.name
            page = root / 'page.html'
            page.write_text(f'<title>{marker}</title><button onclick="document.getElementById(\'result\').textContent=\'Clicked in background\'">Change text</button><input aria-label="Name"><p id="result">Untouched</p>')
            env = {**os.environ,'ATTICUS_DIR':tmp}
            log = open(root / 'chrome.log','w')
            process = subprocess.Popen([str(CHROME),f'--user-data-dir={profile}','--no-first-run','--no-default-browser-check','--disable-sync','--disable-background-networking',page.as_uri()],env=env,stdout=log,stderr=log,start_new_session=True)
            foreground = None
            def osa(script):
                result = subprocess.run(['osascript','-e',script],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                return result.stdout.strip()
            try:
                # Only request IDs for our unique fixture title, even if AppleScript chose another Chrome instance.
                selector = f'tell application "Google Chrome"\nset ws to every window whose name contains "{marker}"\nif (count ws) is 0 then return ""\nset w to item 1 of ws\nreturn (id of w as text) & (ASCII character 9) & (id of active tab of w as text)\nend tell'
                ids = ''
                for _ in range(20):
                    time.sleep(.3); ids = osa(selector)
                    if ids: break
                if not ids: self.skipTest('AppleScript selected another/windowless Chrome instance; production returns hands fallback')
                window, tab = map(int,ids.split('\t'))
                tell = f'tell application "Google Chrome" to execute (first tab of window id {window} whose id is {tab}) javascript '
                webjs = Path(atticus.WEB_JS).read_text()
                read = osa(tell + json.dumps(f'({webjs})("read", "", "")'))
                self.assertIn(marker,read); self.assertIn('1 button: Change text',read)
                (root / 'web.json').write_text(json.dumps({'app':'Google Chrome','window':window,'tab':tab}))
                # Another scratch Chrome window becomes browser window 1; the mapped page must remain the action target.
                other = root / 'other.html'; other.write_text('<title>ATTICUS scratch cover</title><p>Second fixture window</p>')
                osa(f'tell application "Google Chrome"\nset w to make new window\nset URL of active tab of w to "{other.as_uri()}"\nend tell')
                time.sleep(.5)
                self.assertNotEqual(int(osa('tell application "Google Chrome" to get id of window 1')),window)
                # A separate native fixture, rather than a personal app, takes OS focus.
                source = root / 'cover.swift'
                source.write_text('''import Cocoa
let app=NSApplication.shared; app.setActivationPolicy(.regular)
let previous=NSWorkspace.shared.frontmostApplication
let w=NSWindow(contentRect:NSRect(x:40,y:40,width:800,height:600),styleMask:[.titled],backing:.buffered,defer:false)
w.title="Atticus scratch foreground"; w.orderFrontRegardless()
DispatchQueue.main.asyncAfter(deadline:.now()+0.2) { app.activate(ignoringOtherApps:true); w.makeKeyAndOrderFront(nil) }
DispatchQueue.main.asyncAfter(deadline:.now()+0.6) { print(ProcessInfo.processInfo.processIdentifier); fflush(stdout) }
signal(SIGTERM) { _ in DispatchQueue.main.async { previous?.activate(); exit(0) } }; app.run()
''')
                cover = root / 'ForegroundFixture.app/Contents/MacOS/cover'; cover.parent.mkdir(parents=True)
                with open(cover.parent.parent / 'Info.plist','wb') as f:
                    plistlib.dump({'CFBundleIdentifier':'com.atticus.foreground.fixture','CFBundleExecutable':'cover','CFBundleName':'Foreground Fixture','CFBundlePackageType':'APPL'},f)
                subprocess.run(['swiftc',str(source),'-o',str(cover)],env=env,check=True,capture_output=True)
                foreground = subprocess.Popen([str(cover)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                pid = int(foreground.stdout.readline()); time.sleep(.4)
                front = lambda: int(osa('tell application "System Events" to get unix id of first application process whose frontmost is true'))
                self.assertEqual(front(),pid)
                osa(tell + json.dumps('document.title'))
                before = front(); self.assertNotEqual(before,process.pid)
                with patch.object(atticus,'DIR',tmp), patch.dict(os.environ,{'ATTICUS_FRONT':'Google Chrome','ATTICUS_DIR':tmp},clear=False):
                    self.assertIn('Filled',atticus.web('fill','2','Background name'))
                    self.assertIn('Clicked 1',atticus.web('click','1'))
                self.assertEqual(front(),before,'JavaScript must leave the current foreground app focused')
                result = osa(tell + json.dumps('document.getElementById("result").textContent + "/" + document.querySelector("input").value'))
                self.assertEqual(result,'Clicked in background/Background name')
                print('CHROME BACKGROUND: JS fill/click changed the pinned non-front window; another app retained focus',flush=True)
                # System Events already has AX access; probe only the scratch PID/window, without changing permissions.
                osa(tell + json.dumps('document.getElementById("result").textContent="AX pending"'))
                script = f'''tell application "System Events" to tell (first application process whose unix id is {process.pid})
try
set value of attribute "AXManualAccessibility" to true
end try
set w to first window whose name contains "{marker}"
repeat with e in entire contents of w
try
if role of e is "AXButton" and (name of e is "Change text" or description of e is "Change text") then
perform action "AXPress" of e
return "AXPress"
end if
end try
end repeat
return "No background AX button"
end tell'''
                ax = subprocess.run(['osascript','-e',script],env=env,capture_output=True,text=True,timeout=10)
                if ax.returncode == 0 and ax.stdout.strip() == 'AXPress':
                    self.assertEqual(osa(tell + json.dumps('document.getElementById("result").textContent')),'Clicked in background')
                    print('CHROME AX: AXPress on the background page button worked without activating Chrome (System Events)',flush=True)
                else:
                    print('CHROME AX: not verified: ' + (ax.stderr.strip() or ax.stdout.strip()),flush=True)
                self.assertEqual(front(),before)
            finally:
                if foreground:
                    foreground.terminate(); foreground.communicate(timeout=5)
                if process.poll() is None: os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL); process.wait()
                log.close()


if __name__ == '__main__': unittest.main()
