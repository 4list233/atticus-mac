"""Real AX integration, confined to an accessory fixture and temporary Atticus binary."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import signal
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'macOS AX fixture')
class NativeHandsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.binary = cls.root / 'Atticus'
        cls.env = {**os.environ, 'ATTICUS_DIR': str(cls.root), 'ATTICUS_RESOURCES_DIR': str(ROOT),
                   'ATTICUS_AX_ONLY': '1', 'ATTICUS_HANDS_TIMING':'1', 'ATTICUS_HANDS_STATE': str(cls.root / 'hands.json')}
        compiled = subprocess.run(['swiftc', '-O', str(ROOT / 'Atticus.swift'), '-o', str(cls.binary)], capture_output=True, text=True)
        if compiled.returncode:
            raise AssertionError(compiled.stderr)
        fixture = cls.root / 'HandsFixture.app/Contents/MacOS/fixture'
        fixture.parent.mkdir(parents=True)
        (fixture.parent.parent / 'Info.plist').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>com.atticus.hands.fixture</string><key>CFBundleExecutable</key><string>fixture</string><key>CFBundleName</key><string>Hands Fixture</string><key>CFBundlePackageType</key><string>APPL</string><key>LSUIElement</key><true/><key>NSPrincipalClass</key><string>NSApplication</string><key>CFBundleVersion</key><string>1</string><key>NSHighResolutionCapable</key><true/></dict></plist>''')
        compiled = subprocess.run(['swiftc', str(ROOT / 'tests/hands_fixture.swift'), '-o', str(fixture)], capture_output=True, text=True)
        if compiled.returncode:
            raise AssertionError(compiled.stderr)

    def call(self, *args, receipt=None, ok=True):
        start = time.perf_counter()
        result = subprocess.run([str(self.binary), '--hands', *args], input=json.dumps(receipt) if receipt else None,
                                env={**self.env, 'ATTICUS_HANDS_RECEIPT': '1' if receipt else '0'},
                                capture_output=True, text=True, timeout=10)
        self.elapsed = (time.perf_counter() - start) * 1000
        match = re.search(r"HANDS NET ([\d.]+) ms",result.stderr)
        self.net_elapsed = float(match[1]) if match else None
        if ok:
            self.assertIsNotNone(self.net_elapsed, 'Missing native timing')
            if result.returncode:
                print(f'HANDS TIMINGS (failed {args[0]}): wall {self.elapsed:.1f} ms, net {self.net_elapsed:.2f} ms', flush=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else: self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def action(self, *args):
        receipt = json.loads(self.call('label', *args))
        return self.call(*args, receipt=receipt)

    def test_native_selftest(self):
        result = subprocess.run([str(self.binary), '--selftest'], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('native selftest ok', result.stdout)

    def test_fixture_actions_and_timings(self):
        # Permission probe deliberately names an impossible PID; never inspect a user window.
        probe = subprocess.run([str(self.binary), '--hands', 'see', '2147483647'], env=self.env, capture_output=True, text=True)
        if 'Allow Atticus' in probe.stdout:
            print('HANDS TIMINGS (fixture): unavailable — temporary binary lacks Accessibility permission', flush=True)
            self.skipTest('Temporary binary lacks Accessibility permission; no permission changes made')
        fixture = self.root / 'HandsFixture.app/Contents/MacOS/fixture'
        ready = self.root / 'fixture-ready'
        ready.unlink(missing_ok=True)
        process = subprocess.Popen(['open','-W','-g','-n',str(fixture.parents[2]),'--args',str(self.root)],
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=self.env)
        pid = None
        def stop():
            if pid:
                try: os.kill(int(pid),signal.SIGTERM)
                except ProcessLookupError: pass
            try: process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(ready.exists(),'Fixture did not become ready')
        pid = ready.read_text().strip()
        def command(text):
            path = self.root / 'fixture-command'
            path.write_text(text)
            end = time.monotonic() + 2
            while path.exists() and time.monotonic() < end: time.sleep(.01)
            self.assertFalse(path.exists())
        timing, net = {}, {}
        screen = self.call('see', pid); timing['see'] = self.elapsed; net['see'] = self.net_elapsed
        for label in ('Export', 'Send', 'Delete draft', 'Password', 'Name', 'Enabled', 'File'):
            self.assertIn(label, screen)
        self.assertNotIn('never-print-me', screen)
        self.assertNotIn('4111', screen)
        self.assertNotIn('never-print-me', (self.root / 'hands.json').read_text())
        self.assertIn('[SECRET]', screen)
        data = json.loads((self.root / 'hands.json').read_text())
        export = next(n for n in data['nodes'] if n['label'] == 'Export')
        result = self.call('click', str(export['id'])); timing['click id'] = self.elapsed; net['click id'] = self.net_elapsed
        self.assertIn('a sheet opened', result)
        self.assertIn('Export Your Document', result)
        command('close-sheet'); self.call('see', pid)
        self.assertIn('a sheet opened', self.call('click', 'Export')); timing['click label'] = self.elapsed; net['click label'] = self.net_elapsed
        command('close-sheet'); self.call('see', pid)
        self.call('click', 'File > Export To > PDF…'); timing['menu path'] = self.elapsed; net['menu path'] = self.net_elapsed
        self.assertEqual((self.root / 'fixture-result').read_text(), 'pdf')
        (self.root / 'hands.json').unlink()   # targeted menus must not depend on today's front-app map
        self.call('menu', pid, 'File > Export To > PDF…'); timing['targeted menu'] = self.elapsed; net['targeted menu'] = self.net_elapsed
        self.assertEqual((self.root / 'fixture-result').read_text(), 'pdf')
        self.call('see', pid)
        result = self.call('scroll', 'down'); timing['scroll'] = self.elapsed; net['scroll'] = self.net_elapsed
        self.assertIn('Scrolled down', result); self.assertRegex(result, r'now \d+%')
        self.assertRegex(self.call('scroll-to', 'Row 150'), r'(Found|Revealed) "Row 150"')
        self.assertIn('Row 150', self.call('see', pid))
        self.call('scroll', 'up', 'all')
        self.assertIn('Already at the end', self.call('scroll', 'up'))
        self.assertIn('Window settled', self.call('wait', '2'))
        self.assertIn('Typed into', self.action('type', 'Name', 'Sam', 'submit'))
        data = json.loads((self.root / 'hands.json').read_text())
        self.assertEqual(next(n['value'] for n in data['nodes'] if n['label'] == 'Name'), 'Sam')
        self.action('type', 'Name', 'append') # Text "append" is not the append option.
        self.action('type', 'Name', '!', 'append')
        self.assertIn('append!', self.call('see', pid))
        began = time.perf_counter()
        self.assertIn('Clicked "Enabled"', self.action('click', 'Enabled'))
        timing['verified switch'] = (time.perf_counter() - began) * 1000
        self.assertRegex(self.call('see', pid), r'CheckBox "Enabled" = "(?:1|true)"')
        self.call('press', 'cmd+shift+a') # Posted only to the fixture PID, never global.
        self.assertIn('Window settled', self.call('wait', '.5'))
        if os.environ.get('ATTICUS_LIVE_MOUSE') == '1':
            saved = self.env['ATTICUS_AX_ONLY']; self.env['ATTICUS_AX_ONLY'] = '0'
            try:
                self.call('click', 'Enabled', 'double')
                self.call('click', 'Enabled', 'right')
                self.action('drag', 'Enabled', 'Name')
            finally: self.env['ATTICUS_AX_ONLY'] = saved
        self.call('see', pid)
        command('stale')
        self.assertIn('The screen changed. Run see again.', self.call('click', str(export['id']), ok=False))
        self.assertIn('a sheet opened', self.call('click', 'Export'))
        command('close-sheet'); command('duplicate'); self.call('see', pid)
        self.assertIn('More than one', self.call('click', 'Export', ok=False))
        self.assertIn('a sheet opened', self.call('click', 'Export', 'near', 'Name'))
        command('close-sheet'); self.call('see', pid)
        self.assertIn('Use atticus.py', self.call('click', 'Send', ok=False))
        self.assertIn('Use atticus.py', self.call('press', 'cmd+q', ok=False))
        self.assertIn('Use atticus.py', self.call('type', 'Password', 'secret', ok=False))
        self.action('click', 'Send')
        self.assertEqual((self.root / 'fixture-result').read_text(), 'Send')
        (self.root / 'settings.json').write_text('{"safe":false}')
        self.call('click', 'Delete draft')
        self.assertEqual((self.root / 'fixture-result').read_text(), 'Delete draft')
        command('long-text')
        document = json.loads(self.call('read', pid))
        self.assertIn('DOCUMENT END', document['text'])
        self.assertGreater(len(document['text']), 12000)
        command('rich-table'); rich = self.call('see', pid)
        self.assertIn('Explain the observed relationship:', rich)
        began = time.perf_counter()
        self.assertIn('Typed after', self.action('type-after', 'Explain the observed relationship:', ' Answer.'))
        timing['type after'] = (time.perf_counter() - began) * 1000
        document = json.loads(self.call('read', pid))
        self.assertIn('Explain the observed relationship: Answer.', document['text'])
        self.assertIn('A second prompt must remain.', document['text'])
        print('HANDS TIMINGS (fixture): ' + ', '.join(f'{name} {value:.1f} ms' for name,value in timing.items()), flush=True)
        print('HANDS NET TIMINGS (fixture): ' + ', '.join(f'{name} {value:.2f} ms' for name,value in net.items() if value is not None), flush=True)


    def test_quartz_event_spies_restore_cursor_and_clear_held_modifiers(self):
        source = (ROOT / 'Atticus.swift').read_text()
        methods = source[source.index('    static func normalized('):source.index('    static func attribute(')]
        methods += source[source.index('    static func sendKeys('):source.index('    static func raise(')]
        methods += source[source.index('    static func mouse('):source.index('    static func fingerprint(')]
        methods += source[source.index('    static func wheel('):source.index('    static func contentMoved(')]
        methods = methods.replace('NSRunningApplication','TargetApp').replace('CGEventSource(', 'EventSource(').replace('CGEvent(', 'Event(').replace('CGWarpMouseCursorPosition(', 'warp(')
        swift = '''import Cocoa
enum Prefs { static var path:String { CommandLine.arguments[1] + "/settings.json" } }
struct TargetApp { var processIdentifier: Int32 = 123 }
var cursor = CGPoint(x:900,y:600), traces:[Event] = [], warped:[CGPoint] = []
var cancelDrag=false, restored=0
func warp(_ p: CGPoint) { cursor = p; warped.append(p) }
final class EventSource {
 init?(stateID: CGEventSourceStateID) { precondition(stateID == .privateState) }
}
final class Event {
 var location = cursor, flags:CGEventFlags = .maskAlternate, type:CGEventType = .null
 var count:Int64 = 0, pid:Int32 = 0, code:CGKeyCode = 0
 init?(source:EventSource) {}
 init?(keyboardEventSource:EventSource,virtualKey:CGKeyCode,keyDown:Bool) { code = virtualKey; type = keyDown ? .keyDown : .keyUp }
 init?(mouseEventSource:EventSource,mouseType:CGEventType,mouseCursorPosition:CGPoint,mouseButton:CGMouseButton) { type = mouseType; location = mouseCursorPosition }
 init?(scrollWheelEvent2Source:EventSource,units:CGScrollEventUnit,wheelCount:UInt32,wheel1:Int32,wheel2:Int32,wheel3:Int32) { type = .scrollWheel; count = Int64(wheel1) }
 func setIntegerValueField(_ field:CGEventField,value:Int64) { count = value }
 func postToPid(_ target:Int32) { pid = target; traces.append(self) }
 func post(tap:CGEventTapLocation) { traces.append(self); if cancelDrag && type == .leftMouseDragged { Hands.stopped=1 } }
}
enum Hands {
 static let env:[String:String] = [:]
 struct Failure:Error { let message:String; init(_ m:String) { message=m } }
 static func matches(_ s:String,_ p:String)->Bool { s.range(of:p,options:.regularExpression) != nil }
 static var stopped:Int32=0
 static func checkStopped()throws { if stopped != 0 { throw Failure("Cancelled") } }
 static func raise(_ a:TargetApp) throws {}
 static func restoreFocus(_ a:TargetApp)->()->Void { { restored += 1 } }
''' + methods + '''
}
let a = TargetApp(), saved = cursor
try Hands.mouse(CGPoint(x:10,y:20),a,double:true)
precondition(traces.map(\\.type) == [.leftMouseDown,.leftMouseUp,.leftMouseDown,.leftMouseUp])
precondition(traces.map(\\.count) == [1,1,2,2] && traces.allSatisfy { $0.flags.isEmpty })
precondition(cursor == saved)
traces = []; try Hands.mouse(CGPoint(x:10,y:20),a,right:true)
precondition(traces.map(\\.type) == [.rightMouseDown,.rightMouseUp] && cursor == saved)
traces = []; try Hands.mouse(CGPoint(x:10,y:20),a,to:CGPoint(x:50,y:60))
precondition(traces.count == 22 && traces.first!.type == .leftMouseDown && traces.last!.type == .leftMouseUp)
precondition(traces.filter { $0.type == .leftMouseDragged }.count == 20 && cursor == saved)
traces = []; try Hands.sendKeys("cmd+shift+4",pid:123,times:2)
precondition(traces.count == 4 && traces.allSatisfy { $0.pid == 123 && $0.code == 21 && $0.flags == [.maskCommand,.maskShift] })
traces = []; try Hands.wheel("down",CGPoint(x:5,y:6),a,fraction:0.25)
precondition(traces.count == 1 && traces[0].type == .scrollWheel && traces[0].count == -125 && traces[0].flags.isEmpty && cursor == saved)
traces=[]; cancelDrag=true
do { try Hands.mouse(CGPoint(x:10,y:20),a,to:CGPoint(x:50,y:60)); preconditionFailure("Cancelled drag continued") } catch {}
precondition(traces.last!.type == .leftMouseUp && cursor == saved && restored == 5)
print("hands Quartz spies ok")
'''
        path = self.root / 'event-spies.swift'; path.write_text(swift)
        binary = self.root / 'event-spies'
        compiled = subprocess.run(['swiftc',str(path),'-o',str(binary)],capture_output=True,text=True)
        self.assertEqual(compiled.returncode,0,compiled.stderr)
        result = subprocess.run([str(binary),str(self.root)],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('hands Quartz spies ok',result.stdout)

    def test_ax_algorithms_with_in_memory_window(self):
        source = (ROOT / 'Atticus.swift').read_text()
        section = source[source.index('enum Hands {'):source.index('// MARK: - End Hands')]
        header = section[:section.index('    static func attribute(')]
        header = header.replace('AXUIElement','TestElement')
        header = header.replace('ProcessInfo.processInfo.environment', '["ATTICUS_DIR":CommandLine.arguments[1], "ATTICUS_AX_ONLY":"1"]')
        names = ['documentIdentity','identify','read','children','array','name','capture','save','load','at','point','visibleFrame','resolve','menuItem','menu','screenLease',
                 'pixelsChanged','contentMoved','pageKeysAllowed','scrollArea','scroll','findText','scrollTo','stringValue','selectedRange','insertionTarget','insertAfter','typeText',
                 'settle','validate','clickOptions','targets','freshIDs','checked','selftest','windowElement','root']
        matches = list(re.finditer(r'^    static func (\w+)\(', section, re.M))
        methods = ''.join(section[m.start():matches[i+1].start() if i+1<len(matches) else section.rfind('\n}')]
                          for i,m in enumerate(matches) if m[1] in names)
        methods = re.sub(r'\bAXUIElement\b','TestElement',methods)
        methods = methods.replace('NSRunningApplication','TestApp').replace('NSPasteboard.general','Clipboard.general')
        swift = '''import Cocoa
enum Prefs { static var path:String { CommandLine.arguments[1] + "/settings.json" } }
final class TestElement:NSObject {
 var attrs:[String:AnyObject] = [:], rect:CGRect, kids:[TestElement] = [], parent:TestElement?, supported:[String] = []
 var pid:Int32=123, action:((String)->Void)?
 init(_ role:String,_ title:String="",_ r:CGRect=CGRect(x:0,y:0,width:80,height:24)) {
  rect=r; super.init(); attrs[kAXRoleAttribute]=role as NSString; attrs[kAXTitleAttribute]=title as NSString
 }
 func add(_ e:TestElement) { kids.append(e); e.parent=self }
}
func axWindowNumber(_ e:TestElement) -> NSNumber? { e.attrs["AXWindowNumber"] as? NSNumber }
struct TestApp { let processIdentifier:Int32 = 123; let localizedName:String? = "Fixture"; var isTerminated=false; let bundleURL:URL?=nil }
let application = TestElement("AXApplication"), window = TestElement("AXWindow","Fixture",CGRect(x:0,y:0,width:400,height:400))
window.attrs["AXWindowNumber"]=NSNumber(value:42)
let menuBar=TestElement("AXMenuBar"), file=TestElement("AXMenuBarItem","File"), fileMenu=TestElement("AXMenu"), exportTo=TestElement("AXMenuItem","Export To"), formats=TestElement("AXMenu"), pdf=TestElement("AXMenuItem","PDF…")
application.add(window); application.attrs[kAXWindowsAttribute]=[window] as NSArray; application.attrs[kAXMenuBarAttribute]=menuBar
menuBar.add(file); file.add(fileMenu); fileMenu.add(exportTo); exportTo.add(formats); formats.add(pdf)
file.supported=["AXPress"]; exportTo.supported=["AXPress"]; pdf.supported=["AXPress"]
let export=TestElement("AXButton","Export",CGRect(x:10,y:10,width:80,height:24)), send=TestElement("AXButton","Send",CGRect(x:100,y:10,width:80,height:24))
window.add(export); window.add(send); export.supported=["AXPress"]
let row=TestElement("AXRow","",CGRect(x:10,y:80,width:180,height:24)), rowText=TestElement("AXStaticText","Destination",CGRect(x:20,y:80,width:120,height:24))
row.add(rowText); row.supported=["AXPress"]; window.add(row)
let name=TestElement("AXTextField","Name",CGRect(x:10,y:50,width:150,height:24)), secure=TestElement("AXSecureTextField","Password",CGRect(x:200,y:50,width:150,height:24))
name.attrs[kAXValueAttribute]="4111 1111 1111 1111" as NSString; secure.attrs[kAXValueAttribute]="never-print-me" as NSString
window.add(name); window.add(secure); application.attrs[kAXFocusedUIElementAttribute]=name
var caret=CFRange(location:0,length:0); name.attrs[kAXSelectedTextRangeAttribute]=AXValueCreate(.cfRange,&caret)!
let area=TestElement("AXScrollArea","Rows",CGRect(x:10,y:100,width:370,height:200)), document=TestElement("AXGroup","",CGRect(x:10,y:100,width:370,height:4800)), bar=TestElement("AXScrollBar","Scroll",CGRect(x:370,y:100,width:10,height:200))
window.add(area); area.add(document); area.add(bar); area.attrs[kAXVerticalScrollBarAttribute]=bar; bar.attrs[kAXValueAttribute]=NSNumber(value:0)
final class Clipboard {
 static let general=Clipboard(); var text="Original clipboard", changeCount=0
 var pasteboardItems:[NSPasteboardItem]? { let i=NSPasteboardItem(); i.setString(text,forType:.string); return [i] }
 func clearContents() { text=""; changeCount += 1 }
 func setString(_ s:String,forType:NSPasteboard.PasteboardType)->Bool { text=s; changeCount += 1; return true }
 func writeObjects(_ items:[NSPasteboardItem]) { text=items.first?.string(forType:.string) ?? ""; changeCount += 1 }
}
var refuseValueWrite=false, offset=0.0, pagePushes=0, menuPressed=false, enhancedWrites=0, lazyAccessibility=false, keysSent:[String]=[]
func move(_ value:Double) {
 offset=max(0,min(4600,value)); bar.attrs[kAXValueAttribute]=NSNumber(value:offset/4600)
 for (i,e) in document.kids.enumerated() { e.rect.origin.y=100 + Double(i)*24-offset }
}
for i in 1...200 {
 let row=TestElement("AXStaticText","Row \\(i)",CGRect(x:20,y:100+Double(i-1)*24,width:300,height:24))
 row.supported=["AXScrollToVisible"]; row.action={ _ in move(Double(i-1)*24) }; document.add(row)
}
area.supported=["AXScrollDownByPage","AXScrollUpByPage"]; area.action={ action in pagePushes += 1; move(offset + (action.contains("Down") ? 200 : -200)) }
pdf.action={ _ in menuPressed=true; window.attrs[kAXTitleAttribute]="PDF" as NSString; pdf.attrs[kAXTitleAttribute]="Changed after press" as NSString }
func AXUIElementCreateApplication(_ pid:Int32)->TestElement { precondition(pid==123); return application }
func AXUIElementSetMessagingTimeout(_ e:TestElement,_ timeout:Float)->AXError { .success }
func AXUIElementIsAttributeSettable(_ e:TestElement,_ key:CFString,_ result:UnsafeMutablePointer<DarwinBoolean>)->AXError { result.pointee=DarwinBoolean(e === bar); return .success }
func AXUIElementSetAttributeValue(_ e:TestElement,_ key:CFString,_ value:CFTypeRef)->AXError {
 let k=key as String
 if e === application && k == "AXEnhancedUserInterface" { enhancedWrites += 1 }
 if refuseValueWrite && e === name && k == kAXValueAttribute { return .cannotComplete }
 if e === bar, k == kAXValueAttribute { move((value as! NSNumber).doubleValue*4600) }
 else if k == kAXFocusedAttribute { application.attrs[kAXFocusedUIElementAttribute]=e }
 else if k == kAXSelectedTextAttribute, let old=e.attrs[kAXValueAttribute] as? String, let range=Hands.selectedRange(e) {
  e.attrs[kAXValueAttribute]=(old as NSString).replacingCharacters(in:NSRange(location:range.location,length:range.length),with:value as! String) as NSString
 }
 else { e.attrs[k]=value }
 return .success
}
var rootHit:TestElement?
var cgWindows:[[String:Any]] = []
func CGWindowListCopyWindowInfo(_ options:CGWindowListOption,_ relative:CGWindowID)->CFArray? { cgWindows as CFArray }
func AXUIElementCreateSystemWide()->TestElement { application }
func AXUIElementCopyElementAtPosition(_ e:TestElement,_ x:Float,_ y:Float,_ found:UnsafeMutablePointer<TestElement?>)->AXError { found.pointee=rootHit; return rootHit == nil ? .noValue : .success }
func AXUIElementGetPid(_ e:TestElement,_ pid:UnsafeMutablePointer<pid_t>)->AXError { pid.pointee=e.pid; return .success }
''' + header + '''
 static func attribute(_ e:TestElement,_ key:String)->CFTypeRef? {
  if key == kAXChildrenAttribute {
   if lazyAccessibility && e === window && !CFEqual(application.attrs["AXEnhancedUserInterface"]!,kCFBooleanTrue) { return [] as NSArray }
   return e.kids as NSArray
  }
  if key == kAXParentAttribute { return e.parent }
  return e.attrs[key]
 }
 static func element(_ e:TestElement,_ key:String)->TestElement? { attribute(e,key) as? TestElement }
 static func frame(_ e:TestElement)->CGRect { e.rect }
 static func perform(_ e:TestElement,_ names:[String])->Bool {
  guard let action=names.first(where:e.supported.contains) else { return false }; e.action?(action); return true
 }
 static func fingerprint(_ m:Map)->[UInt8] { [] }
 static func wheel(_ d:String,_ p:CGPoint,_ a:TestApp,fraction:Double=1)throws { preconditionFailure("Unexpected real mouse fallback") }
 static func raise(_ a:TestApp)throws { preconditionFailure("Unexpected app activation") }
 static func restoreFocus(_ a:TestApp)->()->Void { {} }   // menu presses tell the focus guard; nothing to restore in memory
 static func sendKeys(_ text:String,pid:Int32,times:Int=1)throws { precondition(pid==123); keysSent.append(text); if text == "cmd+v", let field=application.attrs[kAXFocusedUIElementAttribute] as? TestElement { field.attrs[kAXValueAttribute]=Clipboard.general.text as NSString } }
''' + methods + '''
}
Hands.selftest()
precondition(!Hands.riskyLabel("Fixture > Start meeting") && Hands.riskyLabel("File > Delete") && Hands.riskyLabel("OK"))
precondition(!Hands.windowElement(application,app:application) && Hands.windowElement(window,app:application))
let app=TestApp()
application.attrs["AXEnhancedUserInterface"]=kCFBooleanFalse
var writes=enhancedWrites; _=try! Hands.root(app)
precondition(CFEqual(application.attrs["AXEnhancedUserInterface"]!,kCFBooleanTrue) && enhancedWrites == writes + 1)
Hands.restoreAccessibility()
precondition(CFEqual(application.attrs["AXEnhancedUserInterface"]!,kCFBooleanFalse) && enhancedWrites == writes + 2)
lazyAccessibility=true
writes=enhancedWrites
do {
 defer { Hands.restoreAccessibility() }
 let mapped=try Hands.capture(app)
 precondition(mapped.nodes.contains { $0.label == "Export" })
 let document=try Hands.read(app,head:true)
 precondition((document["text"] as? String)?.contains("Export") == true)
 _=try Hands.capture(app) // Repeated roots must not restore the flag before verification.
 precondition(enhancedWrites == writes + 1)
}
precondition(CFEqual(application.attrs["AXEnhancedUserInterface"]!,kCFBooleanFalse) && enhancedWrites == writes + 2)
precondition((Hands.attribute(window,kAXChildrenAttribute) as? [TestElement])?.isEmpty == true)
lazyAccessibility=false
application.attrs["AXEnhancedUserInterface"]=kCFBooleanTrue; writes=enhancedWrites; _=try! Hands.root(app)
Hands.restoreAccessibility()
precondition(CFEqual(application.attrs["AXEnhancedUserInterface"]!,kCFBooleanTrue) && enhancedWrites == writes)
application.attrs["AXEnhancedUserInterface"]=kCFBooleanFalse
let stableIdentity=Hands.documentIdentity(app,window), stableFrame=window.rect
window.rect.origin.x += 100; window.attrs[kAXTitleAttribute]="Fixture — Edited" as NSString
precondition(Hands.documentIdentity(app,window)==stableIdentity)
window.attrs["AXWindowNumber"]=NSNumber(value:43); precondition(Hands.documentIdentity(app,window) != stableIdentity)
window.attrs.removeValue(forKey:"AXWindowNumber"); window.attrs[kAXTitleAttribute]="Untitled — Edited" as NSString
let fallbackIdentity=Hands.documentIdentity(app,window)
window.attrs[kAXTitleAttribute]="Untitled — Modified" as NSString; precondition(Hands.documentIdentity(app,window)==fallbackIdentity)
window.attrs[kAXTitleAttribute]="Another document" as NSString; precondition(Hands.documentIdentity(app,window) != fallbackIdentity)
window.attrs["AXWindowNumber"]=NSNumber(value:42); window.attrs[kAXTitleAttribute]="Fixture" as NSString; window.rect=stableFrame
application.attrs[kAXFocusedWindowAttribute]=application; application.attrs[kAXMainWindowAttribute]=application
precondition(try! Hands.root(app).1 === window) // Invalid focused/main references must fall back to AXWindows.
application.attrs[kAXWindowsAttribute]=[application] as NSArray
cgWindows=[[kCGWindowOwnerPID as String:123,kCGWindowLayer as String:0,kCGWindowBounds as String:window.rect.dictionaryRepresentation]]
rootHit=export
precondition(try! Hands.root(app).1 === window) // CG hit-test climbs from a control to its actual window.
rootHit=application
do { _=try Hands.root(app); preconditionFailure("Application accepted as a window") } catch {}
let foreign=TestElement("AXWindow"); foreign.pid=999; rootHit=foreign
do { _=try Hands.root(app); preconditionFailure("Another app's window accepted") } catch {}
rootHit=nil; cgWindows=[]; application.attrs[kAXWindowsAttribute]=[window] as NSArray
let nestedText=String(repeating:"Deep document words. ",count:100)
let level1=TestElement("AXGroup"), level2=TestElement("AXGroup"), level3=TestElement("AXGroup"), deepEditor=TestElement("AXTextArea","Deep document")
deepEditor.attrs[kAXValueAttribute]=nestedText as NSString; level1.add(level2); level2.add(level3); level3.add(deepEditor)
let shallowToolbar=TestElement("AXToolbar","Shallow tools")
for i in 1...300 { shallowToolbar.add(TestElement("AXStaticText","Shallow tool \\(i)")) }
let savedChildren=window.kids; window.kids=[]; window.add(shallowToolbar); window.add(level1)
precondition((try! Hands.read(app,head:true)["text"] as? String)?.contains("Deep document words")==true)
window.kids=savedChildren; for child in savedChildren { child.parent=window }
let longText=String(repeating:"Document paragraph with unfamiliar words. ",count:300) + "DOCUMENT END"
let editor=TestElement("AXTextArea","Document",CGRect(x:10,y:5000,width:380,height:300))
editor.attrs[kAXValueAttribute]=longText as NSString; window.add(editor)
let toolbar=TestElement("AXToolbar","Tools",CGRect(x:0,y:0,width:400,height:80))
for i in 1...300 { toolbar.add(TestElement("AXStaticText","Tool \\(i)")) }; window.kids.insert(toolbar,at:0)
let headRead=try Hands.read(app,head:true)
precondition((headRead["text"] as? String)?.contains("Document paragraph")==true && (headRead["text"] as! String).count<=1510)
window.kids.removeFirst()
let documentRead=try Hands.read(app)
precondition((documentRead["text"] as? String)?.contains(longText)==true)
precondition(!(documentRead["text"] as! String).contains("never-print-me"))
precondition((documentRead["text"] as! String).contains("Row 200"))
let tail=TestElement("AXStaticText",String(repeating:"Tail paragraph. ",count:3000)); window.add(tail)
let finalText=TestElement("AXStaticText","NATIVE FINAL"); window.add(finalText)
let firstChunk=try Hands.read(app); precondition(firstChunk["incomplete"] as? Bool == true)
let readState=try! JSONSerialization.jsonObject(with:Data(contentsOf:Hands.state.deletingLastPathComponent().appendingPathComponent("document-read.json"))) as! [String:Any]
precondition((readState["guards"] as! [[String:Any]]).count<=64)
let nextChunk=try Hands.read(app,more:true); precondition((nextChunk["text"] as! String).contains("NATIVE FINAL"))
_=try Hands.read(app); name.attrs[kAXValueAttribute]="Changed document" as NSString
do { _=try Hands.read(app,more:true); preconditionFailure("Mixed document states accepted") } catch {}
name.attrs[kAXValueAttribute]="4111 1111 1111 1111" as NSString
window.kids.removeLast(2)
let secretAlias=TestElement("AXTextField"); secretAlias.attrs["AXLabel"]="Client Secret" as NSString; secretAlias.attrs[kAXValueAttribute]="never-expose-alias" as NSString
window.add(secretAlias)
precondition(!(try! Hands.read(app)["text"] as! String).contains("never-expose-alias"))
window.kids.removeLast()
let secretStatic=TestElement("AXStaticText","Recovery phrase"); secretStatic.attrs[kAXValueAttribute]="alpha beta gamma delta" as NSString
let maskedStatic=TestElement("AXStaticText","Reference"); maskedStatic.attrs[kAXValueAttribute]="654321" as NSString
window.add(secretStatic); window.add(maskedStatic)
let privateRead=try! Hands.read(app)["text"] as! String
precondition(!privateRead.contains("alpha beta") && !privateRead.contains("654321"))
window.kids.removeLast(2)
window.attrs[kAXTitleAttribute]="Password policy worksheet" as NSString
precondition((try! Hands.read(app)["text"] as! String).contains("DOCUMENT END"))
window.attrs[kAXTitleAttribute]="Fixture" as NSString

var initial=try Hands.capture(app)
precondition(initial.nodes.contains { $0.label == "Export" } && initial.nodes.contains { $0.label == "File" })
precondition(initial.nodes.contains { $0.role == "AXRow" && $0.label == "Destination" })
precondition(initial.texts.contains("Row 1") && !initial.texts.contains("Row 150"))
precondition(!initial.description.contains("never-print-me") && !initial.description.contains("4111") && initial.description.contains("[SECRET]"))
try Hands.save(initial); let saved=try Hands.load(); precondition(saved.signature==initial.signature)
let stored=try String(contentsOf:Hands.state,encoding:.utf8); precondition(stored.contains("signature") && !stored.contains("never-print-me"))
let id=initial.nodes.first { $0.label=="Export" }!.id
precondition(try! Hands.resolve(String(id),map:initial,a:app).0.label == "Export")
precondition(try! Hands.resolve("export",map:initial,a:app).0.id == id)
precondition(try! Hands.resolve("110,20",map:initial,a:app).0.label == "Send")
let risk=try Hands.checked(["click","110,20"],initial,app); precondition(risk["risky"] as? Bool == true)
let unknown=try Hands.checked(["click","390,390"],initial,app); precondition(unknown["risky"] as? Bool == true)
do { _=try Hands.resolve("500,500",map:initial,a:app); preconditionFailure("Point outside mapped app accepted") } catch {}
let cancel=TestElement("AXButton","Cancel",CGRect(x:200,y:80,width:80,height:20)), ok=TestElement("AXButton","OK",CGRect(x:290,y:80,width:80,height:20))
window.add(cancel); window.add(ok)
var dialog=try Hands.capture(app)
precondition((try! Hands.checked(["click","Cancel"],dialog,app))["risky"] as? Bool == false)
precondition((try! Hands.checked(["click","OK"],dialog,app))["risky"] as? Bool == false)
let warning=TestElement("AXStaticText","Delete 3 items?",CGRect(x:10,y:80,width:160,height:20)); window.add(warning)
dialog=try Hands.capture(app)
precondition((try! Hands.checked(["click","Cancel"],dialog,app))["risky"] as? Bool == false)
precondition((try! Hands.checked(["click","OK"],dialog,app))["risky"] as? Bool == true)
window.attrs[kAXTitleAttribute]="Changed" as NSString
let fresh=try Hands.capture(app)
do { try Hands.freshIDs(["click",String(id)],initial,fresh); preconditionFailure("Stale ID accepted") } catch {}
try Hands.freshIDs(["click","Export"],initial,fresh)
let duplicate=TestElement("AXButton","Export",CGRect(x:200,y:10,width:80,height:24)), anchor=TestElement("AXStaticText","Format",CGRect(x:200,y:40,width:80,height:24))
window.add(duplicate); window.add(anchor); let duplicates=try Hands.capture(app)
do { _=try Hands.resolve("Export",map:duplicates,a:app); preconditionFailure("Ambiguous control accepted") } catch {}
precondition(try! Hands.resolve("Export",map:duplicates,a:app,near:"Format").1 === duplicate)
precondition(try! Hands.menu("File > Export To > PDF…",app,act:false)=="PDF…" && !menuPressed)
precondition(try! Hands.menu("File > Export To > PDF…",app,act:true)=="PDF…" && menuPressed)
precondition((try! Hands.scroll("down",pages:1,all:false,a:app)).contains("Scrolled down") && offset>0)
precondition((try! Hands.scrollTo("Row 150",app)).contains("Revealed"))
precondition((try! Hands.capture(app)).texts.contains("Row 150"))
_=try Hands.scroll("up",pages:1,all:true,a:app); precondition(offset==0)
precondition(try! Hands.scroll("up",pages:1,all:false,a:app)=="Already at the end")
let pushes=pagePushes; _=try Hands.scroll("down",pages:0.25,all:false,a:app); precondition(offset>0 && pagePushes==pushes)
let field=(try Hands.capture(app)).nodes.first { $0.label=="Name" }!
try Hands.typeText("Sam",field,name,app,append:false); try Hands.typeText("!",field,name,app,append:true)
precondition(name.attrs[kAXValueAttribute] as? String == "Sam!")
name.attrs[kAXValueAttribute]="Question remains:\\nNext line remains." as NSString; caret=CFRange(location:0,length:0); name.attrs[kAXSelectedTextRangeAttribute]=AXValueCreate(.cfRange,&caret)!
precondition((try! Hands.insertAfter("Question remains:"," Answer.",app)).contains("Typed after"))
precondition(name.attrs[kAXValueAttribute] as? String == "Question remains: Answer.\\nNext line remains.")
do { _=try Hands.checked(["press","backspace"],try Hands.capture(app),app); preconditionFailure("Destructive key accepted in editable text") } catch {}
refuseValueWrite=true; try Hands.typeText("Background paste",field,name,app,append:false)
precondition(name.attrs[kAXValueAttribute] as? String == "Background paste" && Clipboard.general.text == "Original clipboard")
precondition(keysSent.suffix(2) == ["cmd+a","cmd+v"]); refuseValueWrite=false
precondition(!Hands.pageKeysAllowed(app))
precondition(try! Hands.settle(app,seconds:0.5))
print("hands in-memory AX fixture ok")
'''
        swift = swift.replace('AXUIElementCreateApplication','testApplication').replace('AXUIElementCreateSystemWide','testSystemWide').replace('CGWindowListCopyWindowInfo','testCGWindows')
        path = self.root / 'ax-spies.swift'; path.write_text(swift)
        binary = self.root / 'ax-spies'
        compiled = subprocess.run(['swiftc',str(path),'-o',str(binary)],capture_output=True,text=True)
        self.assertEqual(compiled.returncode,0,compiled.stderr)
        result = subprocess.run([str(binary),str(self.root)],capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('hands in-memory AX fixture ok',result.stdout)


if __name__ == '__main__': unittest.main()
