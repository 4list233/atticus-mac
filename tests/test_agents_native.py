"""Deferred native/WebKit checks. Do not run on the user's Mac; VM only when authorized."""
import os
import plistlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


def native_fixture(body, real_mini=False):
    """The real agent panels with stubbed app, prefs and worker, then a fixture `body`."""
    source=(ROOT/'Atticus.swift').read_text()
    panels=source[source.index('// MARK: - Agent panels'):source.index('// First-run window')].replace('NSWorkspace.shared.accessibilityDisplayShouldReduceMotion','fixtureReduced')
    show=source[source.index('    func show()',source.index('final class App')):source.index('    func hide(after',source.index('final class App'))]
    lease=source[source.index('    static func screenLease()'):source.index('    static func fingerprint',source.index('    static func screenLease()'))]
    defaults=source[source.index('    func defaultKey('):source.index('    func gone()',source.index('    func defaultKey('))]
    keys=source[source.index('var specs = [EventTypeSpec'):source.index('var ref: EventHotKeyRef?',source.index('var specs = [EventTypeSpec'))]
    mini=source[source.index('final class PassView'):source.index('\n}\n',source.index('final class MiniScreen'))+3]
    harness='''import Cocoa
import Carbon
import WebKit
import AVFoundation
import EventKit
let dir=CommandLine.arguments[1]
var fixtureReduced=false
enum Prefs {
 static let path=ProcessInfo.processInfo.environment["ATTICUS_DIR"]!+"/settings.json"
 static var d:[String:Any]=[:]
 static func set(_ k:String,_ v:Any){d[k]=v}
 static func bool(_ k:String)->Bool{d[k] as? Bool ?? false}
 static func str(_ k:String)->String{d[k] as? String ?? ""}
}
final class KeyPanel:NSPanel{override var canBecomeKey:Bool{true}}
final class MiniScreen {
 let clip=NSView();var forcePreview=false;var target:[String:Any]?,timer:Timer?,publish:(([String:Any])->Void)?,screen:(()->NSScreen?)?
 func reset(){target=nil};func receive(_ value:[String:Any]){target=value};func stopCapture(clear:Bool){};func finish(){};func open(){};func layout(_ value:[String:Any]){}
}
enum MeetingFrame {
 static func key(_ s:NSScreen)->String{"fixture"}
 static func clamp(_ f:NSRect,_ b:NSRect)->NSRect{var f=f;f.size.width=min(f.width,b.width);f.size.height=min(f.height,b.height);f.origin.x=min(max(f.minX,b.minX),b.maxX-f.width);f.origin.y=min(max(f.minY,b.minY),b.maxY-f.height);return f}
}
func axAttribute(_ e:AXUIElement,_ key:String)->CFTypeRef?{nil}
func currentMeetingEvent(_ store:EKEventStore)->[String:Any]?{nil}
let calendar=EKEventStore()
final class MeetingStub {var running=false;func open(){};func acceptAgent(_ row:[String:Any]){};func showAgentPrep(_ row:[String:Any]){}}
let meeting=MeetingStub()
final class OnboardingStub{var allowed:[String]=[];func allow(_ need:String,app:String){allowed.append(need)}}   // setup asks: what Allow opened
let onboarding=OnboardingStub()
enum Onboarding{static var on=false;static func granted(_ need:String,app:String)->Bool{on}}
final class EngineStub{var isRunning=false}
final class AppStub{
 let web=WKWebView()
 let panel=KeyPanel(contentRect:NSRect(x:40,y:40,width:340,height:120),styleMask:[.nonactivatingPanel,.borderless],backing:.buffered,defer:false)
 let mini=MiniScreen(),engine=EngineStub();var busy=false,composing=false,pinned=false,gen=0,escRef:EventHotKeyRef?,agentCard=""
 var calls:[String]=[],defaultKeys:[EventHotKeyRef]=[]
 var defaultWanted=false,defaultObservers:[NSObjectProtocol]=[]
 func js(_ call:String,_ data:Any?=nil){calls.append(call)}
 func modelChoices()->[String:Any]{[:]}
 func frame(_ f:NSRect){panel.setFrame(f,display:true)}
 func cue(_ name:String){}
 func hide(after:Double){}
 func escape(){};func shortcut(_ pressed:Bool){}
''' + show + defaults + '''
}
let app=AppStub()
''' + keys + '''
func py(_ args:[String])->Process{let p=Process();p.executableURL=URL(fileURLWithPath:"/usr/bin/env");p.arguments=["python3",dir+"/tests/native_agent_worker.py"];return p}
enum Hands {
 struct Failure:Error{init(_ text:String){}}
 static let env=ProcessInfo.processInfo.environment
 static func checkStopped()throws{}
''' + lease + '''
}
''' + panels + body
    if real_mini:   # the real live-window stream, for screenshots that show it
        stub=harness[harness.index('final class MiniScreen {'):harness.index('\n}\n',harness.index('final class MiniScreen {'))+3]
        harness=harness.replace(stub,mini+'func axWindowNumber(_ e:AXUIElement)->NSNumber?{nil}\n').replace('import Cocoa\n','import Cocoa\nimport ScreenCaptureKit\nimport CoreMedia\n',1)
    return harness


def run_fixture(test,body,marker,real_mini=False):
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder); fixture=root/'agents.swift';fixture.write_text(native_fixture(body,real_mini))
        # No real Claude, connector, audio, or app actions; all native UI stays in the VM.
        resources=root/'resources';resources.mkdir();(resources/'ui').symlink_to(ROOT/'ui')
        (resources/'tests').mkdir();(resources/'tests'/'native_agent_worker.py').write_text('import sys\nfor line in sys.stdin: pass\n')
        binary=root/'agents';env={**os.environ,'ATTICUS_DIR':str(root),'ATTICUS_ASK':str(root)}
        build=subprocess.run(['swiftc','-O',str(fixture),'-o',str(binary)],env=env,capture_output=True,text=True)
        test.assertEqual(build.returncode,0,build.stderr)
        result=subprocess.run([str(binary),str(resources)],env=env,capture_output=True,text=True,timeout=120)
        test.assertEqual(result.returncode,0,result.stdout+result.stderr)
        test.assertIn(marker,result.stdout)
        print(result.stdout.strip(),flush=True)


@unittest.skipUnless(sys.platform=='darwin','macOS native agent panels')
class AgentNativeTests(unittest.TestCase):
    def test_badge_counts_only_working_agents_but_restart_guard_keeps_live_work(self):
        run_fixture(self,'''let agentsUI=AgentPanels()
let now=Date().timeIntervalSince1970
let statuses=["running","needs-you","waiting-screen","queued","done","failed","idle"]
let rows=statuses.enumerated().map{index,status in ["id":String(repeating:String(index+1),count:32),"title":status,"status":status,"created":now] as [String:Any]}
agentsUI.receive(["event":"list","agents":rows])
guard agentsUI.count==1 else{print("Badge counted non-working agents");exit(1)}
guard agentsUI.hasLive else{print("Restart guard lost blocked or queued work");exit(1)}
agentsUI.receive(["event":"list","agents":Array(rows.suffix(3))])
guard agentsUI.count==0,!agentsUI.hasLive else{print("Terminal and idle agents stayed live");exit(1)}
agentsUI.process?.terminate();print("working agent count checks ok")''','working agent count checks ok')

    def test_card_flight_placement_and_reduced_motion(self):
        run_fixture(self,'''let nsapp=NSApplication.shared;nsapp.setActivationPolicy(.accessory)
let agentsUI=AgentPanels()
let id="11111111111111111111111111111111"
agentsUI.receive(["event":"state","agent":["id":id,"title":"Flight","status":"running","created":Date().timeIntervalSince1970]])
app.panel.contentView=app.web;app.panel.setFrame(NSRect(x:80,y:80,width:340,height:180),display:true);app.web.frame=app.panel.contentView!.bounds
app.web.loadHTMLString("<body style='background:#222;color:white'><h1>Research task</h1><p>Reading a source</p></body>",baseURL:nil);app.panel.orderFrontRegardless()
var stage=0,tries=0,at=Date(),flight:NSWindow?,origin=NSRect.zero
func fail(_ text:String)->Never{print(text);fflush(stdout);agentsUI.process?.terminate();exit(1)}
Timer.scheduledTimer(withTimeInterval:0.05,repeats:true){_ in
 tries+=1;if tries>150{fail("Flight fixture timed out at stage \\(stage)")}
 switch stage{
 case 0:
  guard tries>10,agentsUI.ready.contains("shelf") else{return}
  for bounds in [NSRect(x:0,y:0,width:1512,height:945),NSRect(x:-1920,y:0,width:1920,height:1055)]{
   for saved in [nil,[9000.0,-9000.0],[Double.nan,0]] as [[Double]?]{let f=AgentPanels.shelfFrame(NSRect(x:9000,y:9000,width:260,height:0),bounds,saved);guard bounds.contains(f),f.height==52 else{fail("Two-display saved geometry escaped its display")}}
  }
  for screen in NSScreen.screens{agentsUI.placeShelf(screen);guard screen.visibleFrame.contains(agentsUI.shelf!.frame),agentsUI.shelf!.frame.height>=52 else{fail("Shelf placement escaped display bounds")}}
  guard agentsUI.shelf!.collectionBehavior.contains(.canJoinAllSpaces),agentsUI.shelf!.collectionBehavior.contains(.fullScreenAuxiliary) else{fail("Shelf cannot join full-screen Spaces")}
  print("Placement checked on \\(NSScreen.screens.count) VM display(s)");stage=1;at=Date();agentsUI.js("shelf","arrive",id);agentsUI.fly(id,from:[10,10,30,30])
 case 1:
  // One transient panel; the orb swims a curved path on Core Animation (nothing per frame in the app), then the panel goes.
  guard let panel=agentsUI.flight,panel.isVisible else{fail("No orb flight")}
  guard !panel.canBecomeKey,panel.ignoresMouseEvents else{fail("Flight took input")}
  let groups=(panel.contentView?.layer?.sublayers ?? []).compactMap{$0.animation(forKey:"swim") as? CAAnimationGroup}
  guard groups.count==6,let swim=groups.last?.animations?.first as? CAKeyframeAnimation,swim.path != nil,abs(groups.last!.duration-0.62)<0.01 else{fail("Orb does not swim a curved path")}
  flight=panel;stage=2;at=Date()
 case 2:
  guard Date().timeIntervalSince(at)>0.95 else{return}
  guard flight?.isVisible==false,agentsUI.flight==nil else{fail("Flight panel stayed on screen")}
  agentsUI.shelfWeb?.evaluateJavaScript("document.querySelector('.chip.waiting')===null&&document.querySelectorAll('.chip').length===1"){v,_ in guard v as? Bool==true else{fail("The chip did not land")}}
  fixtureReduced=true;stage=3;at=Date();agentsUI.fly(id,from:[10,10,30,30])
 case 3:
  guard agentsUI.flight==nil else{fail("Reduce Motion still swam")}
  agentsUI.process?.terminate();print("flight and placement checks ok");exit(0)
 default:break
 }
}
nsapp.run()''','flight and placement checks ok')

    def test_real_return_is_captured_only_for_an_actionable_card(self):
        run_fixture(self,'''let nsapp=NSApplication.shared;nsapp.setActivationPolicy(.accessory)
let agentsUI=AgentPanels()
NSWorkspace.shared.runningApplications.first{$0.bundleIdentifier=="com.apple.finder"}?.activate(options:[])
var front:pid_t?
app.panel.orderFrontRegardless();app.defaultKey(true)
print("Return registrations: \\(app.defaultKeys.count)");fflush(stdout)
func require(_ value:Bool,_ message:String){if !value{print(message);fflush(stdout);agentsUI.process?.terminate();exit(1)}}
require(app.defaultKeys.count==2,"Return hotkeys failed to register")
func press(){DispatchQueue.global().async{for down in [true,false]{CGEvent(keyboardEventSource:CGEventSource(stateID:.hidSystemState),virtualKey:CGKeyCode(kVK_Return),keyDown:down)?.post(tap:.cghidEventTap);usleep(50_000)}}}
var stage=0,tries=0,other:KeyPanel?
Timer.scheduledTimer(withTimeInterval:0.15,repeats:true){_ in
 tries+=1;require(tries<40,"Real Return did not reach the native card default")
 if stage==0{front=NSWorkspace.shared.frontmostApplication?.processIdentifier;require(front != ProcessInfo.processInfo.processIdentifier,"A separate front app is required");stage=1;press()}
 else if stage==1,app.calls.contains("agentDefaultAction"){
  require(app.defaultKeys.isEmpty,"Return stayed captured after use")
  require(NSWorkspace.shared.frontmostApplication?.processIdentifier==front,"Return default stole foreground app focus")
  stage=2;press()
 }else if stage>=2{
  stage+=1
  if stage==5{require(app.calls.filter{$0=="agentDefaultAction"}.count==1,"No-card Return triggered an agent action");app.defaultKey(true);other=KeyPanel(contentRect:NSRect(x:300,y:300,width:180,height:100),styleMask:[.nonactivatingPanel,.titled],backing:.buffered,defer:false);other?.makeKeyAndOrderFront(nil)}
  if stage==7{require(app.defaultKeys.isEmpty,"Main card captured Return from another Atticus window");other?.orderOut(nil)}
  if stage==9{require(app.defaultKeys.count==2,"Main card default did not recover after closing another window");app.defaultKey(false);agentsUI.process?.terminate();print("native Return checks ok");exit(0)}
 }
}
nsapp.run()''','native Return checks ok')

    def test_complete_app_compiles_and_native_selftest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);binary=root/'Atticus'
            env={**os.environ,'ATTICUS_DIR':str(root),'ATTICUS_RESOURCES_DIR':str(ROOT)}
            build=subprocess.run(['swiftc','-O',str(ROOT/'Atticus.swift'),'-o',str(binary)],env=env,capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            result=subprocess.run([str(binary),'--selftest'],env=env,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            # Export a VM-built, scratch-state preview; never install or launch it on the host.
            preview=ROOT/'test-artifacts'/'Atticus Agents Preview.app';contents=preview/'Contents'
            (contents/'MacOS').mkdir(parents=True,exist_ok=True);(contents/'Resources').mkdir(exist_ok=True)
            shutil.copy2(binary,contents/'MacOS'/'Atticus');shutil.copy2(ROOT/'atticus.py',contents/'Resources'/'atticus.py')
            shutil.copytree(ROOT/'ui',contents/'Resources'/'ui',dirs_exist_ok=True)
            launcher=contents/'MacOS'/'launch'
            launcher.write_text('#!/bin/zsh\nexport ATTICUS_DIR=$(mktemp -d)\nexport ATTICUS_RESOURCES_DIR="$(cd "$(dirname "$0")/../../../.." && pwd)"\nprintf \'%s\\n\' \'{"input":"type","safe":true}\' > "$ATTICUS_DIR/settings.json"\nexec "$(dirname "$0")/Atticus" -onboarded YES\n');launcher.chmod(0o755)
            with (contents/'Info.plist').open('wb') as f:
                plistlib.dump({'CFBundleIdentifier':'com.atticus.agents-preview','CFBundleName':'Atticus Agents Preview',
                              'CFBundleExecutable':'launch','CFBundlePackageType':'APPL','CFBundleVersion':'1','LSUIElement':True,
                              'NSMicrophoneUsageDescription':'Listen when you use the microphone.',
                              'NSAppleEventsUsageDescription':'Perform the actions you request.',
                              'NSCalendarsFullAccessUsageDescription':'Prepare your selected meeting.',
                              'NSSpeechRecognitionUsageDescription':'Transcribe your requested speech.'},f)
            signed=subprocess.run(['codesign','--force','--sign','-',str(preview)],env=env,capture_output=True,text=True)
            self.assertEqual(signed.returncode,0,signed.stderr)

    def test_shelf_hover_fade_focus_safe_ping_and_screen_lease(self):
        run_fixture(self,'''let nsapp=NSApplication.shared;nsapp.setActivationPolicy(.accessory)
let agentsUI=AgentPanels()
let id="11111111111111111111111111111111"
var row:[String:Any]=["id":id,"title":"Prepare report","brief":"Read the supplied material.","status":"offered","step":"Reading source material","created":Date().timeIntervalSince1970,"steps":["Checking the brief","Reading source material"],"timeline":[]]
let original=NSWorkspace.shared.frontmostApplication?.processIdentifier
var stage=0,tries=0,nearAt=Date.distantPast
func fail(_ text:String)->Never{print(text);agentsUI.process?.terminate();exit(1)}
func evaluate(_ script:String,_ done:@escaping(Any?)->Void){agentsUI.shelfWeb?.evaluateJavaScript(script){value,error in if let error{fail(error.localizedDescription)};done(value)}}
agentsUI.receive(["event":"state","agent":row])
agentsUI.ensureShelf(preload:true)
let startAt=Date();agentsUI.accept(id)
guard agentsUI.shelf?.isVisible == true,Date().timeIntervalSince(startAt)<0.1 else{fail("Starting feedback exceeded 100 ms")}
print("Starting chip visible in \\(Int(Date().timeIntervalSince(startAt)*1000)) ms")
Timer.scheduledTimer(withTimeInterval:0.1,repeats:true){_ in
 tries+=1;if tries>150{fail("Agent native test timed out at stage \\(stage)")}
 switch stage {
 case 0:
  guard agentsUI.ready.contains("shelf"),let shelf=agentsUI.shelf,shelf.isVisible else{return}
  guard !shelf.canBecomeKey,!shelf.isKeyWindow else{fail("Shelf took keyboard focus")}
  evaluate("document.querySelector('.chip .title').textContent"){value in
   guard value as? String=="Prepare report" else{fail("Shelf chip missing")}
   stage=1;evaluate("document.querySelector('.chip').dispatchEvent(new MouseEvent('mouseenter'))"){_ in}
  }
 case 1:
  evaluate("document.querySelector('.chip').classList.contains('expanded')"){value in
   guard value as? Bool==true else{return};stage=2
   let shelf=agentsUI.shelf!,screen=shelf.screen!,display=screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as! CGDirectDisplayID
   let b=CGDisplayBounds(display);CGWarpMouseCursorPosition(CGPoint(x:shelf.frame.minX-20,y:b.maxY-shelf.frame.midY));nearAt=Date()
  }
 case 2:
  guard Date().timeIntervalSince(nearAt)>0.8 else{return}   // the fade now eases over 0.2 s
  guard agentsUI.shelf?.ignoresMouseEvents==true,abs((agentsUI.shelf?.alphaValue ?? 1)-0.25)<0.01 else{fail("Shelf did not yield near the mouse")}
  stage=3;app.busy=true;row["status"]="done";row["summary"]="The report is ready.";row["ended"]=Date().timeIntervalSince1970
  agentsUI.receive(["event":"done","agent":row]);agentsUI.receive(["event":"ping","agent":row])
 case 3:
  guard !app.calls.contains("agentDone") else{fail("Done ping interrupted a busy answer")};app.busy=false;agentsUI.lastKey=Date().addingTimeInterval(-2);stage=4
 case 4:
  guard app.calls.contains("agentDone") else{return}
  guard !app.panel.isKeyWindow,NSWorkspace.shared.frontmostApplication?.processIdentifier==original else{fail("Done ping took focus")}
  stage=5;row["status"]="running";row["step"]="Reading source material"
  row["timeline"]=[["tool":"WebFetch","text":"Reading source material"]]
  row["sources"]=[["title":"Research source","url":"https://fixture.example/source","domain":"fixture.example"]]
  agentsUI.receive(["event":"state","agent":row]);agentsUI.openView(id)
 case 5:
  guard agentsUI.ready.contains("view") else{return};stage=6
  agentsUI.viewWeb?.evaluateJavaScript("document.querySelector('.now .current')?.textContent==='Reading source material' && document.querySelector('#timeline .entry')?.textContent.includes('WebFetch') && document.querySelector('.now .source')?.textContent.includes('Research source') && !document.querySelector('#body .mini')"){value,error in
   guard error==nil,value as? Bool==true else{fail("Non-window Agent view lost its live step, source or timeline")}
  DispatchQueue.global().async{
   do{
    let first=try Hands.screenLease(),ready=DispatchSemaphore(value:0),done=DispatchSemaphore(value:0)
    DispatchQueue.global().async{ready.signal();do{let second=try Hands.screenLease();flock(second,LOCK_UN);Darwin.close(second);done.signal()}catch{fail("Second screen lease failed")}}
    ready.wait();usleep(100_000)
    guard done.wait(timeout:.now()) == .timedOut else{fail("Screen leases overlapped")}
    flock(first,LOCK_UN);Darwin.close(first)
    guard done.wait(timeout:.now()+2) == .success else{fail("Screen lease did not resume")}
    DispatchQueue.main.async{agentsUI.process?.terminate();print("agent native checks ok");exit(0)}
   }catch{fail("Screen lease failed")}
  }
  }
 default:break
 }
}
nsapp.run()''','agent native checks ok')

    def test_real_click_on_chip_opens_agent_view_in_front(self):
        # A real HID click, as the user does it: the app is inactive and the shelf never becomes key.
        run_fixture(self,'''let nsapp=NSApplication.shared;nsapp.setActivationPolicy(.accessory)
let agentsUI=AgentPanels()
let id="11111111111111111111111111111111"
let row:[String:Any]=["id":id,"title":"Cheapest e-bikes","brief":"Find the five cheapest e-bikes.","status":"running","step":"Reading amazon.com","created":Date().timeIntervalSince1970-40,"started":Date().timeIntervalSince1970-40,"steps":["Searching the web"],"timeline":[["tool":"WebSearch","text":"Searching the web"]],"sources":[["title":"Amazon","url":"https://www.amazon.com/s?k=e-bike","domain":"www.amazon.com"]]]
var stage=0,tries=0
func fail(_ text:String)->Never{print(text);agentsUI.process?.terminate();exit(1)}
func click(_ p:CGPoint){for type in [CGEventType.mouseMoved,.leftMouseDown,.leftMouseUp]{let e=CGEvent(mouseEventSource:CGEventSource(stateID:.hidSystemState),mouseType:type,mouseCursorPosition:p,mouseButton:.left);e?.setIntegerValueField(.mouseEventClickState,value:1);e?.post(tap:.cghidEventTap);usleep(type == .mouseMoved ? 900_000 : 100_000)}}
agentsUI.receive(["event":"state","agent":row])
Timer.scheduledTimer(withTimeInterval:0.1,repeats:true){_ in
 tries+=1;if tries>120{fail("Real click test timed out at stage \\(stage)")}
 switch stage {
 case 0:
  guard agentsUI.ready.contains("shelf"),let shelf=agentsUI.shelf,shelf.isVisible else{return};stage=1
  agentsUI.shelfWeb?.evaluateJavaScript("(r=>[r.x,r.y,r.width,r.height])(document.querySelector('.chip .head').getBoundingClientRect())"){value,_ in
   guard let r=value as? [Double],r.count==4 else{fail("Chip missing")}
   let f=shelf.frame,top=NSScreen.screens[0].frame.maxY
   let point=CGPoint(x:f.minX+r[0]+r[2]/2,y:top-(f.maxY-r[1]-r[3]/2));stage=2
   DispatchQueue.global().async{click(point)}
  }
 case 2,3:
  stage+=1
 default:
  guard let view=agentsUI.view,view.isVisible,agentsUI.ready.contains("view") else{if tries>60{fail("Clicking the chip did not open the Agent view")};return}
  let windows=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
  let front=windows.first{($0[kCGWindowLayer as String] as? Int)==0}?[kCGWindowNumber as String] as? Int
  guard front==view.windowNumber else{fail("Agent view opened behind other windows")}
  stage = -100
  agentsUI.viewWeb?.evaluateJavaScript("document.querySelector('.item.selected')?.dataset.id"){value,_ in
   guard value as? String==id else{fail("Agent view did not focus the clicked agent")}
   agentsUI.shelfWeb?.evaluateJavaScript("!!document.querySelector('.chip[data-id=\\"\\(id)\\"]')"){value,_ in
    guard value as? Bool==true,agentsUI.shelf?.isVisible==true else{fail("The chip left the shelf after a click")}
    agentsUI.process?.terminate();print("real click checks ok");exit(0)
   }
  }
 }
}
nsapp.run()''','real click checks ok')

    def test_hover_expand_is_smooth_and_resizes_once_per_change(self):
        # Real mouse hovers across chips: frames are timed in WebKit, native panel resizes are counted.
        run_fixture(self,'''let nsapp=NSApplication.shared;nsapp.setActivationPolicy(.accessory)
Prefs.set("agentStack","open")
let agentsUI=AgentPanels()
let now=Date().timeIntervalSince1970
var rows:[[String:Any]]=[]
for i in 0..<3{
 var row:[String:Any]=["id":String(repeating:String(i+1),count:32),"title":"Task \\(i+1)","brief":"b","status":"running","step":"Reading source \\(i+1)"]
 row["created"]=now-Double(i*10);row["started"]=now-Double(i*10);row["steps"]=["Searching the web","Reading a.example","Reading source \\(i+1)"];row["timeline"]=[] as [Any]
 row["sources"]=[["title":"A","url":"https://a.example/x","domain":"a.example"],["title":"B","url":"https://b.example/y","domain":"b.example"]];rows.append(row)
}
agentsUI.receive(["event":"list","agents":rows]);agentsUI.ensureShelf()
CGWarpMouseCursorPosition(CGPoint(x:60,y:400))
var resizes=0,stage=0,tries=0,heads:[CGPoint]=[]
NotificationCenter.default.addObserver(forName:NSWindow.didResizeNotification,object:nil,queue:.main){n in if (n.object as? NSWindow)===agentsUI.shelf{resizes+=1}}
func fail(_ text:String)->Never{print(text);fflush(stdout);agentsUI.process?.terminate();exit(1)}
func move(_ p:CGPoint){CGEvent(mouseEventSource:CGEventSource(stateID:.hidSystemState),mouseType:.mouseMoved,mouseCursorPosition:p,mouseButton:.left)?.post(tap:.cghidEventTap)}
Timer.scheduledTimer(withTimeInterval:0.1,repeats:true){_ in
 tries+=1;if tries>200{fail("Frame fixture timed out at stage \\(stage)")}
 guard stage==0,tries>25,agentsUI.ready.contains("shelf"),let shelf=agentsUI.shelf,shelf.isVisible else{return};stage=1
 agentsUI.shelfWeb?.evaluateJavaScript("JSON.stringify([...document.querySelectorAll('.chip .head')].map(h=>{const r=h.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}))"){v,_ in
  guard let text=v as? String,let points=try? JSONSerialization.jsonObject(with:Data(text.utf8)) as? [[Double]],points.count==3 else{fail("Chips missing")}
  let top=NSScreen.screens[0].frame.maxY,f=shelf.frame
  heads=points.map{CGPoint(x:f.minX+$0[0],y:top-(f.maxY-$0[1]))}
  agentsUI.shelfWeb?.evaluateJavaScript("window.__f=[];window.__x=new Set();let last=performance.now();(function tick(t){__f.push(t-last);last=t;const e=document.querySelector('.chip.expanded');if(e)__x.add(e.dataset.id);requestAnimationFrame(tick)})(last)")
  resizes=0
  // Each hover: 90 ms intent, expand, then the next chip (read live: chips move as others open); finally leave.
  func finish(){
   do{
    agentsUI.shelfWeb?.evaluateJavaScript("JSON.stringify(__f.slice(5))"){v,_ in
     let frames=((try? JSONSerialization.jsonObject(with:Data((v as? String ?? "[]").utf8))) as? [Double] ?? []).sorted()
     guard frames.count>60 else{fail("Too few frames: \\(frames.count)")}
     let median=frames[frames.count/2],p95=frames[Int(Double(frames.count)*0.95)],long=frames.filter{$0>25}.count
     print(String(format:"frames %d, median %.1f ms, p95 %.1f ms, over 25 ms %d (%.1f%%), panel resizes %d for 7 hover changes",frames.count,median,p95,long,Double(long)*100/Double(frames.count),resizes))
     guard resizes<=8 else{fail("Panel resized per frame: \\(resizes)")}
     guard median<=20,Double(long)/Double(frames.count)<0.1 else{fail("Hover motion is not smooth")}
     agentsUI.shelfWeb?.evaluateJavaScript("__x.size"){v,_ in
      print("chips expanded by hover: \\(v ?? 0)");do{guard (v as? Int ?? 0)>=3 else{fail("Hover did not expand the chips")}
      agentsUI.process?.terminate();print("frame checks ok");exit(0)}}
    }
   }
  }
  let plan=[0,1,2,0,-1,1,-1]
  func step(_ i:Int){
   guard i<plan.count else{finish();return}
   if plan[i]<0{move(CGPoint(x:60,y:400));DispatchQueue.main.asyncAfter(deadline:.now()+0.8){step(i+1)};return}
   agentsUI.shelfWeb?.evaluateJavaScript("(r=>[r.x+r.width/2,r.y+r.height/2])(document.querySelectorAll('.chip .head')[\\(plan[i])].getBoundingClientRect())"){v,_ in
    let r=v as? [Double] ?? [0,0],f=shelf.frame;move(CGPoint(x:f.minX+r[0],y:top-(f.maxY-r[1])));DispatchQueue.main.asyncAfter(deadline:.now()+0.8){step(i+1)}}
  }
  step(0)
 }
}
nsapp.run()''','frame checks ok')
