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
    methods = source[source.index('    func cancelFast()'):source.index('    func stop()', source.index('    func cancelFast()'))]
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
    var consumed = 0, fastBusy = false, liveRevised = false, consumedText = "", targetScreen = "Fixture display", mine = "Notes", text = "open Spotify"
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
    def test_typing_requires_a_zero_length_caret(self):
        source = (ROOT / 'Atticus.swift').read_text()
        caret = source[source.index('    var caret: Bool'):source.index('    static func selectedRange',source.index('    var caret: Bool'))]
        swift = '''import Foundation
struct Harness { let text:String, range:CFRange?
''' + caret + '''
}
precondition(Harness(text:"",range:CFRange(location:2,length:0)).caret)
precondition(!Harness(text:" ",range:CFRange(location:2,length:1)).caret)
precondition(!Harness(text:"selected",range:CFRange(location:2,length:8)).caret)
print("caret safety ok")
'''
        with tempfile.TemporaryDirectory() as path:
            file = Path(path) / 'caret.swift'; file.write_text(swift)
            binary = Path(path) / 'caret'
            compiled = subprocess.run(['swiftc',str(file),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(compiled.returncode,0,compiled.stderr)
            result = subprocess.run([str(binary)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_final_words_after_live_action_preserve_raw_remainder(self):
        source = (ROOT / 'Atticus.swift').read_text()
        finish = source[source.index('    func finish()', source.index('    func typed(')):source.index('    // atticus.py --wait, started')]
        swift = '''import Foundation
final class Engine { var isRunning = false }
final class Panel { func makeKey() {}; func makeFirstResponder(_ v: Int) {} }
final class Harness {
    var busy = true, selectionPending = false, fastBusy = false, liveRevised = false, pinned = false
    var offerText: String?, text = "Fixture left, Other right", ahead = "", answer = ""
    var did = ["Fixture: left."], consumed = 2, stops = 0, web = 0
    var proc: Process?, aheadItem: DispatchWorkItem?, commit = "", lastDone = Date()
    let panel = Panel(), engine = Engine()
    var request = "", remaining: String?, delivered = ""
    func rest() -> String { text.split(separator:" ").dropFirst(consumed).joined(separator:" ") }
    func run(_ t: String, remaining: String? = nil) { request = t; self.remaining = remaining }
    func dropAhead() { ahead = "" }
    func unwarm() {}; func hush() {}; func cancel() {}; func hide(after: Double) {}
    func js(_ name: String, _ value: Any = "") {}; func cue(_ name: String) {}
    func linger(_ n: Double) -> Double { n }
    func deliver(_ text: String) { delivered = text }
''' + finish + '''}
func transcriptKey(_ t: String) -> String { t.lowercased() }
let partial = Harness(); partial.finish()
precondition(partial.request == partial.text && partial.remaining == "Other right")
let complete = Harness(); complete.consumed = 4; complete.finish()
precondition(complete.request.isEmpty && complete.delivered == "Fixture: left.")
let fresh = Harness(); fresh.did = []; fresh.consumed = 0; fresh.finish()
precondition(fresh.request == fresh.text && fresh.remaining == nil)
print("confirmed remainder regressions ok")
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); file = root / 'finish.swift'; file.write_text(swift)
            binary = root / 'finish'
            compiled = subprocess.run(['swiftc', str(file), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn('confirmed remainder regressions ok', result.stdout)

    def test_replace_checks_original_text_range_and_app(self):
        source = (ROOT / 'Atticus.swift').read_text()
        method = source[source.index('    var value: String?'):source.index('// Sees the mouse')]
        # Reuse the production method with fake AX writes; never address a real app.
        swift = """import Foundation
import CoreFoundation
let kAXSelectedTextAttribute = "selected"
let kAXValueAttribute = "value"
enum Status { case success, failure }
var currentText = "old", currentRange: CFRange? = CFRange(location: 3, length: 3), writes = 0
func axAttribute(_ element: Int, _ name: String) -> String? { currentText }
func AXUIElementSetAttributeValue(_ e: Int, _ name: CFString, _ text: CFString) -> Status { writes += 1; return .success }
final class FakeApp { var isTerminated = false }
struct Harness {
 let element = 1, app = FakeApp(), text = "old", range: CFRange? = CFRange(location: 3, length: 3)
 static func selectedRange(_ element: Int) -> CFRange? { currentRange }
""" + method + """
let h = Harness()
currentText = "abcdraftgh"
precondition(h.landed("draft","abcdefgh"))
currentText = "xxdraftxx"
precondition(!h.landed("draft","abcdefgh"))
currentText = "old"
precondition(h.replace("draft") && writes == 1)
currentText = "new"; precondition(!h.replace("draft") && writes == 1)
currentText = "old"; currentRange = CFRange(location: 4, length: 3)
precondition(!h.replace("draft") && writes == 1)
currentRange = nil; precondition(!h.replace("draft") && writes == 1)
currentRange = CFRange(location: 3, length: 3); h.app.isTerminated = true
precondition(!h.replace("draft") && writes == 1)
print("selection regressions ok")
"""
        with tempfile.TemporaryDirectory() as path:
            file = Path(path) / 'selection.swift'; file.write_text(swift)
            binary = Path(path) / 'selection'
            compiled = subprocess.run(['swiftc', str(file), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn('selection regressions ok', result.stdout)

    def test_meeting_size_stays_on_small_and_secondary_displays(self):
        source = (ROOT / 'Atticus.swift').read_text()
        size = source[source.index('    func size(_ h: Double'):source.index('    func compact(', source.index('    func size(_ h: Double'))]
        swift = """import Cocoa
final class NSScreen {
 static var main: NSScreen? = NSScreen()
 var visibleFrame = NSRect(x: -800, y: 100, width: 600, height: 400)
}
final class NSWorkspace { static let shared = NSWorkspace(); var accessibilityDisplayShouldReduceMotion = true }
final class NSAnimationContext {
 var duration: Double = 0
 static var done: (() -> Void)?
 static func runAnimationGroup(_ changes: (NSAnimationContext) -> Void, completionHandler: @escaping () -> Void) { changes(NSAnimationContext()); done = completionHandler }
}
final class Panel {
 var screen = NSScreen.main, frame = NSRect(x: -552, y: 258, width: 340, height: 230)
 func setFrame(_ f: NSRect, display: Bool, animate: Bool) { frame = f }
 func setFrame(_ f: NSRect, display: Bool) { frame = f }
 func animator() -> Panel { self }
}
final class Harness { var panel: Panel? = Panel(); var running = true, generation = 0; var expanded = NSSize(width: 340, height: 650), minimized = false, resizing = false
 var reveals: [Int] = []; func js(_ name: String, _ value: Int) { if name == "settled" { reveals.append(value) } }
""" + size + """}
let h = Harness(), p = h.panel!
let top = p.frame.maxY
h.size(200); precondition(p.frame.maxY == top && p.frame.height == 200, "top edge moved")
h.size(200, reveal: 1); precondition(h.reveals == [1], "unchanged frame lost reveal")
h.size(210, reveal: 2); precondition(h.reveals == [1, 2], "Reduce Motion lost reveal")
NSWorkspace.shared.accessibilityDisplayShouldReduceMotion = false
h.size(220, reveal: 3); precondition(h.reveals == [1, 2], "text revealed during expansion")
NSAnimationContext.done!(); precondition(h.reveals == [1, 2, 3], "completion lost reveal")
h.size(230, reveal: 4); h.generation += 1; NSAnimationContext.done!(); precondition(h.reveals == [1, 2, 3], "cancelled animation revealed a new meeting")
NSWorkspace.shared.accessibilityDisplayShouldReduceMotion = true
h.size(0); precondition(p.frame.height == 376 && p.screen!.visibleFrame.contains(p.frame), "secondary display overflow")
h.resize(440, 500); precondition(p.frame.width == 440 && p.frame.height == 376, "resize ignored display limits")
h.minimized = true; h.size(44); precondition(p.frame.width == 116 && p.frame.height == 44, "compact size wrong")
h.minimized = false; h.size(0); precondition(p.frame.width == 440 && p.frame.height == 376, "restore forgot expanded size")
p.screen!.visibleFrame = NSRect(x: 0, y: 0, width: 300, height: 190)
h.size(200); precondition(p.frame.width == 276 && p.frame.height == 166 && p.screen!.visibleFrame.contains(p.frame), "small display overflow")
print("meeting size ok")
"""
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'size.swift'; file.write_text(swift)
            binary = Path(root) / 'size'
            compiled = subprocess.run(['swiftc', str(file), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_calendar_dismissal_and_updates_preserve_open_meeting(self):
        source = (ROOT / 'Atticus.swift').read_text()
        timer = source[source.index('    let g = meeting.generation', source.index('func checkCalendar()')):source.index('\n}', source.index('    let g = meeting.generation', source.index('func checkCalendar()')))]
        context = next(line for line in source.splitlines() if 'let context = [e.title' in line)
        idle = source[source.index('    static func restartWhenIdle()'):source.index('    static func schedule()')]
        swift = """import Foundation
final class Queue {
 var pending: [() -> Void] = []
 func asyncAfter(deadline: DispatchTime, execute work: @escaping () -> Void) { pending.append(work) }
}
enum DispatchQueue { static let main = Queue() }
final class Meeting {
 var generation = 1, running = false, nudge: Int? = 1, panel: Int? = 1, discards = 0
 func discard() { generation += 1; nudge = nil; panel = nil; discards += 1 }
}
let meeting = Meeting()
func scheduleNudge() {
""" + timer + """
}
struct Event { var title: String? = "Call", location: String? = "Room 2", url: URL? = URL(string: "https://meet.google.com/abc"), notes: String? }
let e = Event(), people = ["Dana"]
""" + context + """
precondition(context.contains("Room 2") && context.contains("https://meet.google.com/abc"), "calendar link/location lost")
scheduleNudge(); meeting.discard(); meeting.generation += 1; meeting.nudge = 2; meeting.panel = 2
DispatchQueue.main.pending.removeFirst()(); precondition(meeting.panel == 2, "old timer dismissed new nudge")
scheduleNudge(); DispatchQueue.main.pending.removeFirst()(); precondition(meeting.panel == nil, "current nudge did not dismiss")
final class Engine { var isRunning = false }
final class Voice { var isRunning = false }
final class App { var busy = false, engine = Engine(), voice: Voice? }
final class Window { var isKeyWindow = false }
final class Settings { var win: Window? }
let app = App(), settings = Settings(), onboarding = Settings()
var restarts = 0
func relaunchSoon() { restarts += 1 }
enum Updater {
""" + idle + """}
meeting.panel = 1; Updater.restartWhenIdle(); precondition(restarts == 0, "update interrupted unsaved meeting")
meeting.panel = nil; DispatchQueue.main.pending.removeFirst()(); precondition(restarts == 1, "update never resumed")
print("calendar/update safety ok")
"""
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / 'calendar.swift'; file.write_text(swift)
            binary = Path(root) / 'calendar'
            compiled = subprocess.run(['swiftc', str(file), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shortcut_states_and_recording_limit(self):
        source = (ROOT / 'Atticus.swift').read_text()
        methods = source[source.index('    func shortcut('):source.index('    func review(')]
        methods += source[source.index('    func recordingLimit('):source.index('    func captureSelection()')]
        methods += source[source.index('    func stopOfferInput()'):source.index('    func dismiss()')]
        methods += source[source.index('    func escape()'):source.index('    func hush()')]
        at = source.index('    func stop()', source.index('final class App'))   # the App's stop, not the Mic class's
        stop = source[at:source.index('        engine.stop(); feed?.finish()', at)]
        swift = """import Foundation
struct Prefs { static var paused = false, toggle = false; static func bool(_ k: String) -> Bool { paused } }
final class MeetingStub { var running = false, focuses = 0; func focus() { focuses += 1 } }
let meeting = MeetingStub()
final class Node { var removed=false; func removeTap(onBus: Int) {removed=true} }
final class Engine { var isRunning = false, stopped = false; func stop() {isRunning=false; stopped=true} }
final class Input { var cancelled=false; func finish() {cancelled=true}; func cancel() {cancelled=true} }
final class Analyzer { func cancelAndFinishNow() async {} }
final class QuietAudio { func end() {} }
final class Panel { var isVisible = true; func makeKey() {}; func makeFirstResponder(_ value: Int) {} }
final class Harness {
    let engine = Engine(), panel = Panel(), quietAudio = QuietAudio()
    var retryOffer=false,offerText:String?,askFile="fixture-answer",listenID=0,stops=0
    var feed:Input?,reading:Input?,quiet:Input?,analyzer:Analyzer?
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
meeting.running = true
let meetingShortcut = Harness(); meetingShortcut.shortcut(true); meetingShortcut.shortcut(false)
precondition(!meetingShortcut.engine.isRunning && meeting.focuses == 1 && meetingShortcut.events.isEmpty, "meeting shortcut started the microphone")
meeting.running = false
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
let reply=Harness();reply.retryOffer=true;reply.busy=true;reply.offerText="Original task";reply.text="retry";reply.engine.isRunning=true;reply.feed=Input();reply.reading=Input()
reply.stopOfferInput()
precondition(reply.engine.stopped, "reply mic stayed open")
let failedStart=Harness();failedStart.retryOffer=true;failedStart.busy=true;failedStart.stopOfferInput();precondition(failedStart.engine.stopped && failedStart.busy && failedStart.askFile=="fixture-answer", "failed engine start left its tap or lost its worker")
precondition(!reply.engine.isRunning && reply.text=="Original task" && reply.offerText==nil && reply.busy && reply.retryOffer && reply.askFile=="fixture-answer", "stopping reply input lost the waiting task")
precondition(reply.feed!.cancelled && reply.reading!.cancelled, "reply input stayed alive")
reply.shortcut(true);precondition(reply.events==["start"], "retry shortcut cancelled its worker")
reply.offerText="Original task";reply.text="partial retry";reply.recordingLimit()
precondition(reply.busy && reply.askFile=="fixture-answer" && reply.text=="Original task" && !reply.events.contains("send"), "reply recording limit sent or cancelled the request")
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
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('regressions ok', result.stdout)


if __name__ == '__main__':
    if '--benchmark' in sys.argv:
        with tempfile.TemporaryDirectory() as path:
            subprocess.run(harness(Path(path)) + ['--benchmark'], check=True, timeout=30)
    else:
        unittest.main()
