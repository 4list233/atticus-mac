"""Opt-in Chrome integration: only uniquely titled pages in a fresh --user-data-dir."""
import json
import os
import plistlib
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import functools
import http.server
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import atticus

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')


@unittest.skipUnless(sys.platform == 'darwin' and CHROME.exists(), 'Isolated Chrome profile requires macOS Chrome (run in VM)')
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
                for _ in range(40):
                    time.sleep(.3)
                    try: probe = subprocess.run(['osascript','-e',selector],env=env,capture_output=True,text=True,timeout=3)
                    except subprocess.TimeoutExpired: self.skipTest('VM does not allow Apple events to its scratch Chrome')
                    ids = probe.stdout.strip() if not probe.returncode else ''
                    if ids: break
                if not ids: self.skipTest('Scratch Chrome did not expose its uniquely titled fixture')
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
NSWorkspace.shared.notificationCenter.addObserver(forName:NSWorkspace.didActivateApplicationNotification,object:nil,queue:.main) { n in
 if let a=n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication, a.processIdentifier != ProcessInfo.processInfo.processIdentifier { print("FOCUS LOST: \\(a.processIdentifier)"); fflush(stdout) }
}
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
                # Exercise actual URL creation, navigation, parallel reads and --window continuations.
                handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
                origin = f'http://127.0.0.1:{server.server_port}'
                browser_before = osa('tell application "Google Chrome" to return {id of window 1, id of active tab of window 1, count windows}')
                scratch = root / 'agent'; scratch.mkdir()
                (scratch / 'settings.json').write_text(json.dumps({'workIn':'front'}))
                # Keep production window-target/AX workers confined to a fake executable.
                worker = root / 'fake-app'
                worker.write_text('#!/bin/sh\nif [ "$1" = "--target-app" ]; then echo "Google Chrome"; fi\n')
                worker.chmod(0o700)
                agent_env = {**env, 'ATTICUS_DIR':str(scratch), 'ATTICUS_ASK':str(scratch), 'ATTICUS_AGENT':'fixture', 'ATTICUS_FRONT':'Google Chrome', 'ATTICUS_APP':str(worker)}
                # URL selects the explicitly named browser; the CLI normally resolves the default handler.
                code = 'import atticus; print(atticus.web("read", __import__("sys").argv[1], app="Google Chrome"))'
                readers = [subprocess.Popen([sys.executable, '-c', code, origin+'/page.html?read='+str(i)], env=agent_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(3)]
                for reader in readers:
                    output, error = reader.communicate(timeout=30)
                    self.assertEqual(reader.returncode,0,error); self.assertIn(marker,output,output)
                shown = subprocess.run([sys.executable, '-c', 'import atticus,sys;print(atticus.show(sys.argv[1]))', origin+'/page.html?show'],
                                       env=agent_env, capture_output=True, text=True, timeout=30)
                self.assertEqual(shown.returncode,0,shown.stderr); self.assertIn(marker,shown.stdout,shown.stdout)
                (scratch/'window-summary.json').write_text(json.dumps({'app':'Google Chrome','web':True}))
                window = subprocess.run([sys.executable, '-c', 'import atticus;print(atticus.window_text("Google Chrome"))'],
                                        env=agent_env, capture_output=True, text=True, timeout=30)
                self.assertEqual(window.returncode,0,window.stderr); self.assertIn(marker,window.stdout,window.stdout)
                self.assertEqual(osa('tell application "Google Chrome" to return {id of window 1, id of active tab of window 1, count windows}'), browser_before, 'Background reads selected a tab/window or created a window')
                self.assertEqual(front(),before)
                foreground.terminate(); focus_events, _ = foreground.communicate(timeout=5); foreground = None
                self.assertNotIn('FOCUS LOST',focus_events,'Even transient focus theft is a failure')
                print('CHROME BACKGROUND: three actual URL reads retained foreground, selected tab and window order',flush=True)
            finally:
                if foreground:
                    foreground.terminate(); foreground.communicate(timeout=5)
                if process.poll() is None: os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL); process.wait()
                log.close()


if __name__ == '__main__': unittest.main()
