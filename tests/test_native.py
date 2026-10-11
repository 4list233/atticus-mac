"""Run the actual live-worker methods headlessly, with fake actions and scratch state."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def harness(root):
    source = (ROOT / 'Atticus.swift').read_text()
    methods = source[source.index('    func cancelFast()'):source.index('    func stop()')]
    key = source[source.index('func transcriptKey('):source.index('// atticus.py with')]
    worker = root / 'worker.py'
    worker.write_text('''import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import atticus
if os.environ.get('BENCH') == '1':
    atticus.licensed = lambda: True
    atticus.ping = lambda: None
    atticus.start_claude = lambda: None
    atticus.look = lambda: []
    atticus.app_named = lambda _: 'Spotify'
    def action(*args, **kwargs):
        Path(os.environ['MARKER']).write_text(str(time.time()))
        return type('Result', (), {'returncode': 0})()
    atticus.subprocess.run = action
    sys.argv = ['atticus.py'] + sys.argv[2:]
    atticus.main()
else:
    time.sleep(float(os.environ.get('DELAY', '.1')))
    Path(os.environ['MARKER']).write_text('action')
    print('x' * int(os.environ.get('FLOOD', '0')))
    print('✓ ' + sys.argv[-1], flush=True)
''')
    swift = root / 'native.swift'
    swift.write_text('''import Foundation
var delay = "0.1", flood = "0", bench = false
let root = CommandLine.arguments[1], repo = CommandLine.arguments[2], python = CommandLine.arguments[3]
struct Prefs { static var live = true; static func bool(_ k: String) -> Bool { k != "live" || live } }
final class Engine { var isRunning = true }
func py(_ args: [String], _ extra: [String: String] = [:]) -> Process {
    let p = Process(); p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [root + "/worker.py", repo] + args
    p.environment = ProcessInfo.processInfo.environment.merging(extra) { $1 }
    p.environment?["ATTICUS_DIR"] = root
    p.environment?["DELAY"] = delay; p.environment?["FLOOD"] = flood
    p.environment?["BENCH"] = bench ? "1" : "0"; p.environment?["MARKER"] = root + "/marker"
    return p
}
''' + key + '''
final class Harness {
    var consumed = 0, fastBusy = false, liveRevised = false, consumedText = "", mine = "Notes", text = "open Spotify"
    var fastProc: Process?, proc: Process?, did: [String] = []
    var busy = false, ahead = ""
    let engine = Engine()
    func words() -> [Substring] { text.split(separator: " ") }
    func js(_ name: String, _ text: String) {}
''' + methods + '''}
func pump(_ seconds: Double) {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.005)) }
}
let marker = root + "/marker"
func clear() { try? FileManager.default.removeItem(atPath: marker) }
func waitAction() {
    let end = Date().addingTimeInterval(3)
    while !FileManager.default.fileExists(atPath: marker) && Date() < end { pump(0.005) }
    precondition(FileManager.default.fileExists(atPath: marker), "worker never acted")
}
if CommandLine.arguments.contains("--benchmark") {
    bench = true
    var on: [Double] = [], off: [Double] = []
    for _ in 0..<20 {
        clear(); Prefs.live = true
        let h = Harness(); h.tryFast()
        pump(0.25) // phrase recognized 250 ms before release; no speech finalization delay
        let release = Date().timeIntervalSince1970
        waitAction(); on.append((Double(try! String(contentsOfFile: marker, encoding: .utf8))! - release) * 1000)
        pump(0.05)
        clear(); Prefs.live = false
        let p = py(["--wait"]), input = Pipe(), output = Pipe()
        p.standardInput = input; p.standardOutput = output
        let commit = root + "/commit"; try? FileManager.default.removeItem(atPath: commit)
        p.environment?["ATTICUS_COMMIT"] = commit
        try! p.run(); pump(0.25)
        let released = Date().timeIntervalSince1970
        try! "go".write(toFile: commit, atomically: true, encoding: .utf8)
        try! input.fileHandleForWriting.write(contentsOf: Data("Notes\\topen Spotify\\n".utf8))
        try! input.fileHandleForWriting.close()
        waitAction(); off.append((Double(try! String(contentsOfFile: marker, encoding: .utf8))! - released) * 1000)
        _ = output.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
    }
    func report(_ name: String, _ values: [Double]) {
        let s = values.sorted(); print("\\(name): n=\\(s.count), median=\\(s[10]) ms, p95=\\(s[18]) ms")
    }
    report("live on", on); report("live off", off)
} else {
    clear(); delay = "0.4"
    let h = Harness(); h.tryFast(); precondition(h.fastBusy)
    h.cancelFast(); h.text = "new request"; delay = "0.05"; h.tryFast(); pump(0.6)
    precondition(h.did == ["new request"] && !h.fastBusy, "cancelled result contaminated new request")
    clear(); let cancelled = Harness(); cancelled.tryFast(); cancelled.cancelFast(); pump(0.2)
    precondition(!FileManager.default.fileExists(atPath: marker), "action survived cancellation")
    let revised = Harness(); revised.tryFast(); revised.text = "open Notes"; pump(0.2)
    precondition(revised.liveRevised, "revised transcript was consumed")
    let finalizing = Harness(); finalizing.busy = true; finalizing.ahead = "old words"; finalizing.engine.isRunning = false
    finalizing.dropAhead(); precondition(finalizing.busy && finalizing.ahead.isEmpty, "final transcript correction abandoned request")
    let disabled = Harness(); Prefs.live = false; disabled.tryFast(); precondition(disabled.fastProc == nil)
    Prefs.live = true; flood = "1000000"; let large = Harness(); large.tryFast(); pump(0.4)
    precondition(!large.fastBusy, "full stdout blocked worker")
    print("native live-worker regressions ok")
}
''')
    binary = root / 'native'
    subprocess.run(['swiftc', '-O', str(swift), '-o', str(binary)], check=True, capture_output=True, text=True)
    return [str(binary), str(root), str(ROOT), sys.executable]


@unittest.skipUnless(sys.platform == 'darwin', 'macOS Swift worker regression')
class NativeTests(unittest.TestCase):
    def test_shortcut_states_and_recording_limit(self):
        source = (ROOT / 'Atticus.swift').read_text()
        methods = source[source.index('    func shortcut('):source.index('    func review(')]
        methods += source[source.index('    func recordingLimit('):source.index('    func start()')]
        methods += source[source.index('    func escape()'):source.index('    func hush()')]
        stop = source[source.index('    func stop()'):source.index('        engine.stop(); engine.inputNode.removeTap', source.index('    func stop()'))]
        swift = """import Foundation
struct Prefs { static var paused = false, toggle = false; static func bool(_ k: String) -> Bool { paused } }
final class Engine { var isRunning = false }
final class Panel { var isVisible = true; func makeKey() {}; func makeFirstResponder(_ value: Int) {} }
final class Harness {
    let engine = Engine(), panel = Panel()
    var shortcutDown = false, toggleTalk = false, clickTalk = false, busy = false, speaking = false
    var events: [String] = [], text = "hello", began = Date(), wasUp = false, fresh = false, web = 0
    func start() { events.append("start"); engine.isRunning = true; toggleTalk = Prefs.toggle }
    func cancel() { events.append("cancel"); hush(); shush() }
    func dismiss() { events.append("dismiss"); hush() }
    func shush() -> Bool { let was = speaking; speaking = false; if was { events.append("quiet") }; return was }
    func hush() { events.append("hush"); engine.isRunning = false; busy = false; toggleTalk = false }
    func close(clear: Bool) { events.append("close"); hush() }
    func review(_ text: String) { events.append("review"); hush() }
    func cue(_ text: String) {}
    func js(_ f: String, _ text: Any) {}
    func hide(after: Double) {}
""" + methods + stop + '        events.append("send"); engine.isRunning = false; busy = true; toggleTalk = false\n    }\n' + """}
Prefs.toggle = true
let toggle = Harness(); toggle.shortcut(true); toggle.shortcut(true)
precondition(toggle.events == ["start"], "key repeat toggled twice")
toggle.shortcut(false); precondition(toggle.engine.isRunning && toggle.events == ["start"], "release sent a toggle request")
toggle.shortcut(true); precondition(toggle.events == ["start", "send"], "second press did not send")
toggle.shortcut(false); toggle.shortcut(true); precondition(toggle.events.contains("cancel"), "working shortcut did not cancel")
let empty = Harness(); empty.text = ""; empty.shortcut(true); empty.shortcut(false); empty.shortcut(true); precondition(empty.events == ["start", "send"] && !empty.clickTalk && !empty.engine.isRunning, "empty toggle failed to finalize late speech or reopened typing")
Prefs.toggle = false
let tap = Harness(); tap.text = ""; tap.began = Date(); tap.shortcut(true); tap.shortcut(false); precondition(tap.events == ["start", "send"] && !tap.clickTalk, "quick voice tap opened typing")
let hold = Harness(); hold.shortcut(true); hold.shortcut(false); precondition(hold.events == ["start", "send"], "hold release regressed")
let voice = Harness(); voice.speaking = true; voice.busy = true; voice.shortcut(true); precondition(voice.events == ["quiet"] && !voice.engine.isRunning && voice.busy, "stopping voice started the mic")
let click = Harness(); click.engine.isRunning = true; click.clickTalk = true; click.shortcut(false); precondition(click.events.isEmpty, "release sent click-to-talk")
Prefs.paused = true; let paused = Harness(); paused.shortcut(true); paused.shortcut(false); precondition(paused.events.isEmpty)
Prefs.paused = false; Prefs.toggle = true
let escape = Harness(); escape.shortcut(true); escape.escape(); precondition(!escape.events.contains("send") && !escape.engine.isRunning, "Esc submitted instead of cancelling")
let limit = Harness(); limit.shortcut(true); limit.recordingLimit(); precondition(limit.events.contains("review") && !limit.events.contains("send"), "timeout submitted toggle request")
print("shortcut regressions ok")
"""
        with tempfile.TemporaryDirectory() as path:
            file = Path(path) / 'shortcut.swift'; file.write_text(swift)
            binary = Path(path) / 'shortcut'
            compiled = subprocess.run(['swiftc', '-O', str(file), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn('shortcut regressions ok', result.stdout)

    def test_live_worker_cancellation_revision_and_pipe_drain(self):
        with tempfile.TemporaryDirectory() as path:
            command = harness(Path(path))
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
            self.assertIn('regressions ok', result.stdout)


if __name__ == '__main__':
    if '--benchmark' in sys.argv:
        with tempfile.TemporaryDirectory() as path:
            subprocess.run(harness(Path(path)) + ['--benchmark'], check=True, timeout=30)
    else:
        unittest.main()
