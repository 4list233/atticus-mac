"""Run mock-bridge HTML fixtures in a temporary WebKit host, never the installed app."""
import functools
import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'macOS WebKit fixtures')
class FixtureTests(unittest.TestCase):
    def test_mock_bridge_fixtures(self):
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(ROOT)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        swift = '''import Cocoa
import WebKit
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .idleDisplaySleepDisabled], reason: "Atticus isolated WebKit fixtures")
let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1000, height: 900))
let panel = NSPanel(contentRect: web.frame, styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
panel.contentView = web; panel.sharingType = .none
panel.ignoresMouseEvents = true   // nonactivating fixture windows exercise real compositor frames without changing the user's focus
panel.orderFrontRegardless()
web.load(URLRequest(url: URL(string: CommandLine.arguments[1])!))
var tries = 0
Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { timer in
 tries += 1
 web.evaluateJavaScript("({status:document.getElementById('results')?.className,text:document.getElementById('results')?.textContent})") { result, error in
  if let result = result as? [String: String], ["pass", "fail"].contains(result["status"] ?? "") {
   print(result["text"] ?? ""); exit(result["status"] == "pass" ? 0 : 1)
  }
  if tries > 200 { print("Fixture timed out"); exit(2) }
 }
}
app.run()
'''
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'fixture.swift'; source.write_text(swift)
            binary = Path(root) / 'fixture'
            compiled = subprocess.run(['swiftc', str(source), '-o', str(binary)], text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            for name in ['ui', 'settings', 'meeting', 'onboarding', 'web']:
                with self.subTest(fixture=name):
                    result = subprocess.run([str(binary), f'http://127.0.0.1:{server.server_port}/tests/{name}.html'], capture_output=True, text=True, timeout=25)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
