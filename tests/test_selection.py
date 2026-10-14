"""Selection lifecycle with fake AX, focus, events and clipboard; no real apps."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'macOS Swift regression')
class SelectionTests(unittest.TestCase):
    def run_swift(self, source):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'test.swift'; path.write_text(source)
            binary = Path(root) / 'test'
            result = subprocess.run(['swiftc', str(path), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

    def test_paste_rechecks_target_cancellation_and_clipboard_ownership(self):
        source = (ROOT / 'Atticus.swift').read_text()
        apply = source[source.index('    func apply(_ draft:'):source.index('    func replace(_ draft:')]
        swift = '''import Foundation
import CoreFoundation
let kAXFocusedUIElementAttribute = "focus", kAXSelectedTextAttribute = "text", kAXValueAttribute = "value"
var keys: [Int] = [], field = "old", deaf = false, hidEvents = 0
var focused = 1, selected = "old", currentRange = CFRange(location: 0, length: 3), events = 0, directWrites = 0, directOK = true
func AXUIElementCreateApplication(_ pid: Int) -> Int { pid }
func AXUIElementSetMessagingTimeout(_ element: Int, _ timeout: Double) {}
func axAttribute(_ e: Any, _ name: String) -> CFTypeRef? { name == "focus" ? NSNumber(value: focused) : name == "value" ? field as CFString : selected as CFString }
final class FakeApp: Equatable {
 var isTerminated = false, processIdentifier = 42
 func activate() { NSWorkspace.shared.frontmostApplication = self }
 static func == (a: FakeApp, b: FakeApp) -> Bool { a === b }
}
final class NSWorkspace { static let shared = NSWorkspace(); var frontmostApplication: FakeApp? }
enum PasteType: Hashable { case string, rich }
final class NSPasteboardItem {
 var data: [PasteType: Data] = [:]
 var types: [PasteType] { Array(data.keys) }
 func data(forType type: PasteType) -> Data? { data[type] }
 func setData(_ value: Data, forType type: PasteType) { data[type] = value }
}
final class NSPasteboard {
 static let general = NSPasteboard()
 var pasteboardItems: [NSPasteboardItem]? = [], changeCount = 0, failWrite = false
 func clearContents() { changeCount += 1; pasteboardItems = [] }
 func setString(_ text: String, forType type: PasteType) -> Bool {
  if failWrite { return false }
  let item = NSPasteboardItem(); item.setData(Data(text.utf8), forType: type)
  pasteboardItems = [item]; changeCount += 1; return true
 }
 func writeObjects(_ items: [NSPasteboardItem]) { pasteboardItems = items; changeCount += 1 }
}
enum CGEventTapLocation { case cghidEventTap }
struct Flags: OptionSet { let rawValue: Int; static let maskCommand = Flags(rawValue: 1) }
final class CGEvent {
 var flags: Flags = []
 let key: Int
 init?(keyboardEventSource: Int?, virtualKey: Int, keyDown: Bool) { key = virtualKey }
 func postToPid(_ pid: Int) { events += 1; keys.append(key); precondition(pid == 42 && flags == (key == 51 ? [] : .maskCommand)); if !deaf { field = key == 51 ? "" : "draft" } }
 func post(tap: CGEventTapLocation) { hidEvents += 1 }
}
struct Harness {
 let element: CFTypeRef = NSNumber(value: 1), app = FakeApp(), text = "old", range: CFRange? = CFRange(location: 0, length: 3)
 var editable = false, pastable = true
 static func selectedRange(_ e: CFTypeRef) -> CFRange? { currentRange }
 func replace(_ text: String) -> Bool { if directOK { directWrites += 1; field = text }; return directOK }
''' + apply + '''}
func pump(_ seconds: Double) {
 let end = Date().addingTimeInterval(seconds)
 while Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.005)) }
}
let h = Harness(), pb = NSPasteboard.general
let old = NSPasteboardItem(); old.setData(Data("rich original".utf8), forType: .rich)
func reset() { focused = 1; selected = "old"; field = "old"; deaf = false; currentRange = CFRange(location: 0, length: 3); h.app.isTerminated = false; pb.failWrite = false; pb.writeObjects([old]) }
for scenario in 0..<6 {
 reset(); let before = events; var result: Bool?, allowed = true
 h.apply(scenario == 4 ? "" : "draft", valid: { allowed }) { result = $0 }
 precondition(result == nil, "reported success before delivery")
 switch scenario {
 case 0: NSWorkspace.shared.frontmostApplication = FakeApp()
 case 1: focused = 2
 case 2: currentRange.location = 4
 case 3: selected = "different"
 case 4: h.app.isTerminated = true
 default: allowed = false
 }
 pump(0.2); precondition(result == false && events == before, "stale/cancelled target received keys")
 precondition(pb.pasteboardItems?.first?.data(forType: .rich) == Data("rich original".utf8), "aborted paste touched clipboard")
}
reset(); var ok = false
h.apply("draft", valid: { true }) { ok = $0 }; pump(0.5); precondition(ok)
pb.clearContents(); _ = pb.setString("new copy", forType: .string); pump(0.45)
precondition(pb.pasteboardItems?.first?.data(forType: .string) == Data("new copy".utf8), "new clipboard copy clobbered")
reset(); h.apply("draft", valid: { true }) { ok = $0 }; pump(0.6)
precondition(ok && pb.pasteboardItems?.first?.data(forType: .rich) == Data("rich original".utf8), "rich clipboard not restored")
reset(); let count = pb.changeCount; h.apply("", valid: { true }) { ok = $0 }; pump(0.5)
precondition(ok && pb.changeCount == count && Array(keys.suffix(2)) == [51, 51], "delete key/clipboard regressed")
reset(); pb.failWrite = true; h.apply("draft", valid: { true }) { ok = $0 }; pump(0.2)
precondition(!ok && pb.pasteboardItems?.first?.data(forType: .rich) != nil, "failed write lost clipboard")
reset(); deaf = true; ok = true; h.apply("draft", valid: { true }) { ok = $0 }; pump(0.9)
precondition(!ok && hidEvents == 2 && pb.pasteboardItems?.first?.data(forType: .rich) != nil, "ignored paste reported success or kept the draft on the clipboard")
var readonly = h; readonly.pastable = false; let before = events
readonly.apply("", valid: { true }) { ok = $0 }; pump(0.2); precondition(!ok && events == before, "read-only delete posted")
var editable = h; editable.editable = true; editable.pastable = false
editable.apply("direct", valid: { true }) { ok = $0 }
precondition(ok && directWrites == 1 && events == before, "editable custom role rejected")
reset(); editable.pastable = true; directOK = false
editable.apply("fallback", valid: { true }) { ok = $0 }; pump(0.6)
precondition(ok && events == before + 2, "failed AX write did not fall back to paste")
print("paste safety ok")
'''
        self.assertIn('paste safety ok', self.run_swift(swift))

    def test_typing_capture_accepts_empty_fields_but_never_secure_fields(self):
        source = (ROOT / 'Atticus.swift').read_text()
        capture = source[source.index('    static func capture('):source.index('    static func selectedRange(')]
        typing_guard = next(line for line in source.splitlines() if 'guard let selected = TextSelection.capture(enabled: true' in line)
        swift = '''import Foundation
import CoreFoundation
let kAXFocusedUIElementAttribute = "focus", kAXSubroleAttribute = "subrole", kAXSelectedTextAttribute = "text", kAXSecureTextFieldSubrole = "secure"
var selectedText = "", subrole = "field", canEdit = true
struct Prefs { static func bool(_ key: String) -> Bool { true } }
final class NSRunningApplication: Equatable {
 static let current = NSRunningApplication()
 let processIdentifier = 1
 static func == (a: NSRunningApplication, b: NSRunningApplication) -> Bool { a === b }
}
final class NSWorkspace { static let shared = NSWorkspace(); var frontmostApplication: NSRunningApplication? = NSRunningApplication() }
typealias AXUIElement = Int
func AXIsProcessTrusted() -> Bool { true }
func AXUIElementCreateApplication(_ pid: Int) -> Int { pid }
func AXUIElementSetMessagingTimeout(_ e: Int, _ timeout: Double) {}
func axAttribute(_ e: Int, _ name: String) -> Any? { name == "focus" ? 1 : name == "subrole" ? subrole : selectedText }
struct TextSelection {
 let element: Int, app: NSRunningApplication, text: String, range: CFRange?
 var editable: Bool { canEdit }; var pastable: Bool { false }
 static func selectedRange(_ e: Int) -> CFRange? { CFRange(location: 0, length: selectedText.count) }
''' + capture + '''}
func acceptsTyping() -> Bool {
''' + typing_guard + ''' return false }; return true
}
precondition(acceptsTyping(), "typing rejected an editable custom role")
canEdit = false; precondition(!acceptsTyping(), "typing accepted a read-only custom role"); canEdit = true
precondition(TextSelection.capture()?.caret == true, "a cursor in a text box was not kept for drafts")
canEdit = false; precondition(TextSelection.capture() == nil, "an empty read-only spot became a draft target"); canEdit = true
precondition(TextSelection.capture(enabled: true, allowEmpty: true)?.text == "", "typing rejected an empty field")
selectedText = "old"; precondition(TextSelection.capture()?.text == "old")
precondition(TextSelection.capture(enabled: false, allowEmpty: true) == nil)
subrole = "secure"; precondition(TextSelection.capture(enabled: true, allowEmpty: true) == nil, "password field captured")
subrole = "field"; selectedText = String(repeating: "x", count: 6001)
precondition(TextSelection.capture() == nil, "AI selection budget exceeded")
precondition(TextSelection.capture(enabled: true, allowEmpty: true)?.text.count == 6001, "typing truncated selected text")
print("typing capture ok")
'''
        self.assertIn('typing capture ok', self.run_swift(swift))

    def test_capture_is_async_and_cancelled_or_stale_results_are_ignored(self):
        source = (ROOT / 'Atticus.swift').read_text().split('final class App: NSObject, WKScriptMessageHandler {',1)[1]
        capture = source[source.index('    func captureSelection()'):source.index('    func start()')]
        finish = source[source.index('    func finish()'):source.index('        aheadItem?.cancel()', source.index('    func finish()'))] + '        finished += 1\n    }\n'
        swift = '''import Foundation
struct Prefs { static func bool(_ key: String) -> Bool { true } }
final class NSWorkspace { static let shared = NSWorkspace(); var frontmostApplication: String? = "Original" }
struct TextSelection {
 let text: String
 var caret: Bool { text.isEmpty }
 static func capture(_ app: String?, enabled: Bool) -> TextSelection? { Thread.sleep(forTimeInterval: 0.15); return TextSelection(text: app ?? "") }
}
final class Harness {
 var selectionID = 0, selectionPending = false, selection: TextSelection?, messages: [String] = []
 var warm: Int? = 1, warmSel = "", respawns = 0
 var busy = true, fastBusy = false, stops = 0, finished = 0
 var offerText: String?, text = "Original request"
 func unwarm() { warm = nil }
 func spawn() -> Int? { respawns += 1; warmSel = selection?.text ?? ""; return 1 }
 func js(_ call: String, _ text: String) { messages.append(text) }
''' + capture + finish + '''}
func pump(_ seconds: Double) {
 let end = Date().addingTimeInterval(seconds)
 while Date() < end { RunLoop.current.run(until: Date().addingTimeInterval(0.005)) }
}
let h = Harness(), start = Date()
h.captureSelection(); h.finish(); precondition(h.finished == 0, "short key-up submitted before capture")
precondition(Date().timeIntervalSince(start) < 0.05 && h.selectionPending, "AX capture blocked shortcut")
NSWorkspace.shared.frontmostApplication = "Moved"; pump(0.2)
precondition(h.selection?.text == "Original" && !h.selectionPending, "captured wrong app")
pump(0.1); precondition(h.finished == 1, "pending key-up did not resume")
precondition(h.respawns == 1 && h.warmSel == "Original", "warm worker did not refresh before request")
h.captureSelection(); h.selectionID += 1; h.selectionPending = false; h.selection = nil; pump(0.2)
precondition(h.selection == nil && h.messages == ["Original"], "cancelled capture returned")
h.captureSelection(); NSWorkspace.shared.frontmostApplication = "New"; h.captureSelection(); pump(0.2)
precondition(h.selection?.text == "New" && h.messages == ["Original", "New"], "stale capture contaminated new request")
let reply = Harness(); reply.offerText = "Original request"; reply.text = "retry"; reply.finish()
precondition(reply.text == "Original request" && reply.offerText == nil && reply.messages == ["retry"] && reply.finished == 0, "card reply became a new task or lost its original request")
print("capture safety ok")
'''
        self.assertIn('capture safety ok', self.run_swift(swift))
