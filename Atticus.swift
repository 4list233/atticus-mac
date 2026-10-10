// Atticus overlay: hold Control+Option+Space to talk, let go to run.
// A box in the top left (ui/ui.html, with the thinking-orbs orb) shows your words live (Apple on-device speech),
// then Atticus's notes and steps while `atticus.py` works, then the answer. It never takes focus.
// Build: ./build.sh. Kept running by ~/Library/LaunchAgents/com.atticus.app.plist
import Cocoa
import Carbon
import AVFoundation
import Speech
import WebKit
import ScreenCaptureKit

let dir = Bundle.main.resourcePath!   // build.sh copies atticus.py and ui/ in here

// atticus.py with whatever python3 this Mac has (Homebrew's first, else Apple's).
func py(_ args: [String], _ extra: [String: String] = [:]) -> Process {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = ["python3", dir + "/atticus.py"] + args
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
    env["ATTICUS_APP"] = Bundle.main.executablePath
    env.merge(extra) { $1 }
    p.environment = env
    return p
}

// Sees the mouse over the box without taking clicks or focus (clicks go to the monitors in App).
final class Tracker: NSView {
    var onMove: ((NSPoint) -> Void)?, onExit: (() -> Void)?
    override func hitTest(_ p: NSPoint) -> NSView? { nil }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: .zero, options: [.activeAlways, .mouseMoved, .mouseEnteredAndExited, .inVisibleRect],
                                       owner: self))
    }
    override func mouseMoved(with e: NSEvent) { onMove?(convert(e.locationInWindow, from: nil)) }
    override func mouseEntered(with e: NSEvent) { onMove?(convert(e.locationInWindow, from: nil)) }
    override func mouseExited(with e: NSEvent) { onExit?() }
}

final class App: NSObject, WKScriptMessageHandler {
    let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 76),
                        styleMask: [.nonactivatingPanel, .borderless], backing: .buffered, defer: false)
    let web: WKWebView
    let engine = AVAudioEngine()
    // Speech: Apple's SpeechAnalyzer (macOS 26+), on-device and free; punctuates and capitalizes.
    var feed: AsyncStream<AnalyzerInput>.Continuation?, analyzer: SpeechAnalyzer?, reading: Task<Void, Never>?
    var speechFormat: AVAudioFormat?   // what the model wants (found once at launch)
    var text = "", answer = "", busy = false, gen = 0
    var proc: Process?, escRef: EventHotKeyRef?, warm: (p: Process, input: FileHandle)?
    var consumed = 0, fastBusy = false, did: [String] = [], pause: DispatchWorkItem?   // commands done while still talking
    var ahead = "", aheadItem: DispatchWorkItem?   // words the AI already started on while the user was still holding the keys
    var stops = 0   // each key release; tells a late "words landed" from a current one
    var hovering = false, xHot = false, pinned = false, stuck = false, lastDone = Date.distantPast   // pinned: the user clicked the box, so only Esc / click-out / ✕ close it; stuck: the user dragged it, so only ✕
    var mine = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""   // the app the user's in, never Atticus itself
    let recent = NSString(string: "~/.config/atticus/recent.jsonl").expandingTildeInPath
    func overX(_ p: NSPoint) -> Bool { p.x > panel.frame.width - 42 && p.y > panel.frame.height - 42 }
    // Words the recognizer should expect: the user's app names plus a few of the user's own (free, on-device).
    lazy var hints: [String] = Array((["Atticus", "Chrome", "VS Code", "Peekaboo", "split screen", "study mode"] +
        ["/Applications", "/System/Applications", "/System/Applications/Utilities"].flatMap {
            ((try? FileManager.default.contentsOfDirectory(atPath: $0)) ?? []).filter { $0.hasSuffix(".app") }.map { String($0.dropLast(4)) }
        }).prefix(100))
    func sound(_ name: String, _ v: Float) { let s = NSSound(named: name); s?.volume = v; s?.play() }

    override init() {
        let cfg = WKWebViewConfiguration()
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 460, height: 76), configuration: cfg)
        super.init()
        cfg.userContentController.add(self, name: "h")
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        let bg = NSVisualEffectView(frame: web.frame)
        bg.material = .hudWindow; bg.state = .active; bg.wantsLayer = true
        bg.layer?.cornerRadius = 18; bg.layer?.masksToBounds = true
        bg.addSubview(web)
        let tracker = Tracker(frame: web.frame)
        tracker.autoresizingMask = [.width, .height]
        tracker.onMove = { [weak self] p in
            guard let self else { return }
            hovering = true
            if overX(p) != xHot { xHot.toggle(); js("xhot", xHot); (xHot ? NSCursor.pointingHand : NSCursor.arrow).set() }
        }
        tracker.onExit = { [weak self] in
            guard let self else { return }
            hovering = false
            if xHot { xHot = false; js("xhot", false); NSCursor.arrow.set() }
            if !busy { hide(after: 3) }   // mouse gone for a few seconds = close
        }
        bg.addSubview(tracker)
        panel.acceptsMouseMovedEvents = true
        panel.contentView = bg
        panel.level = .statusBar; panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let html = (try? String(contentsOfFile: dir + "/ui/ui.html", encoding: .utf8)) ?? "missing ui.html"
        let orbs = (try? String(contentsOfFile: dir + "/ui/orbs.js", encoding: .utf8)) ?? ""
        web.loadHTMLString(html.replacingOccurrences(of: "/*ORBS*/", with: orbs), baseURL: nil)
        // Click anywhere else closes the box once the answer is up; on the box: ✕ closes, elsewhere copies the answer.
        NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            guard let self, panel.isVisible, !busy, !engine.isRunning, !stuck else { return }
            close(clear: false)
        }
        var grab: (mouse: NSPoint, origin: NSPoint)?, moved = false
        NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .leftMouseDragged, .leftMouseUp]) { [weak self] e in
            guard let self else { return e }
            switch e.type {
            case .leftMouseDown:
                guard e.window == panel else { return e }
                if overX(e.locationInWindow) { close(clear: true) } else { pinned = true; grab = (NSEvent.mouseLocation, panel.frame.origin); moved = false }
            case .leftMouseDragged:
                guard let g = grab else { return e }
                let m = NSEvent.mouseLocation
                if abs(m.x - g.mouse.x) + abs(m.y - g.mouse.y) > 3 { moved = true; stuck = true }
                panel.setFrameOrigin(NSPoint(x: g.origin.x + m.x - g.mouse.x, y: g.origin.y + m.y - g.mouse.y))
            default:
                guard grab != nil else { return e }
                grab = nil
                if !moved && !answer.isEmpty {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(String(answer.dropFirst(2)), forType: .string)
                    js("copied")
                }
            }
            return nil
        }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { [weak self] n in
            if let a = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication, a != .current { self?.mine = a.localizedName ?? "" }
        }
        SFSpeechRecognizer.requestAuthorization { _ in }
        Task { [weak self] in   // make sure the speech model is installed, and learn its audio format
            let tr = SpeechTranscriber(locale: Locale(identifier: "en-US"), preset: .progressiveTranscription)
            if let r = try? await AssetInventory.assetInstallationRequest(supporting: [tr]) { try? await r.downloadAndInstall() }
            let f = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [tr])
            await MainActor.run { self?.speechFormat = f }
        }
        AVCaptureDevice.requestAccess(for: .audio) { _ in }
    }

    // The page reports its height; grow the box down from the top-left corner.
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let h = (m.body as? NSNumber).map({ CGFloat($0.doubleValue) }), h > 0 else { return }
        var f = panel.frame
        f.origin.y = f.maxY - h; f.size.height = h
        panel.setFrame(f, display: true, animate: panel.isVisible)
    }

    func js(_ call: String, _ arg: Any? = nil) {
        var a = ""
        if let arg, let d = try? JSONSerialization.data(withJSONObject: [arg]), let s = String(data: d, encoding: .utf8) {
            a = "..." + s
        }
        web.evaluateJavaScript("ui.\(call)(\(a))")
    }

    func show() {
        gen += 1
        if panel.isVisible {   // still up: stay where the user dragged it
            NSAnimationContext.runAnimationGroup { $0.duration = 0.18; panel.animator().alphaValue = 1 }
            return
        }
        let vf = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        panel.setFrameOrigin(NSPoint(x: vf.minX + 14, y: vf.maxY - panel.frame.height - 10))
        panel.alphaValue = 0
        panel.orderFrontRegardless()
        if escRef == nil {
            RegisterEventHotKey(UInt32(kVK_Escape), 0, EventHotKeyID(signature: 0x41545443, id: 2), GetApplicationEventTarget(), 0, &escRef)
        }
        NSAnimationContext.runAnimationGroup { $0.duration = 0.18; panel.animator().alphaValue = 1 }
    }

    func hide(after s: Double) {
        let g = gen
        DispatchQueue.main.asyncAfter(deadline: .now() + s) { [self] in
            guard g == gen, !busy, !engine.isRunning, !hovering, !pinned else { return }
            NSAnimationContext.runAnimationGroup({ $0.duration = 0.3; panel.animator().alphaValue = 0 },
                                                 completionHandler: { [self] in if g == gen { gone() } })
        }
    }

    func gone() {
        panel.orderOut(nil); pinned = false; stuck = false
        if let r = escRef { UnregisterEventHotKey(r); escRef = nil }
    }

    // Esc: stop listening, stop the answer, or close the box.
    func escape() {
        if engine.isRunning {
            engine.stop(); engine.inputNode.removeTap(onBus: 0); feed?.finish(); reading?.cancel(); pause?.cancel(); unwarm()
            aheadItem?.cancel(); dropAhead()
            let an = analyzer; Task { await an?.cancelAndFinishNow() }
            sound("Tink", 0.12); js("stopped", "Stopped listening"); hide(after: 1.5)
        } else if busy {
            cancel()
        } else if panel.isVisible && !stuck {
            close(clear: false)
        }
    }

    func cancel() {
        proc?.terminate(); proc = nil; busy = false
        sound("Tink", 0.12); js("stopped", "Answer stopped"); hide(after: 1.5)
    }

    // ✕ also ends the conversation; clicking away or timing out keeps it for 5 minutes.
    func close(clear: Bool) {
        if clear { try? FileManager.default.removeItem(atPath: recent); lastDone = .distantPast }
        gen += 1
        let g = gen
        NSAnimationContext.runAnimationGroup({ $0.duration = 0.2; panel.animator().alphaValue = 0 },
                                             completionHandler: { [self] in if g == gen { gone() } })
    }

    func start() {
        if busy {   // pressed again while it works = cancel
            return cancel()
        }
        guard !engine.isRunning else { return }
        text = ""; consumed = 0; did = []; ahead = ""
        if !panel.isVisible && Date().timeIntervalSince(lastDone) > 30 { js("reset"); try? FileManager.default.removeItem(atPath: recent) }
        sound("Pop", 0.18)
        unwarm(); warm = spawn()
        onboarding.tried()
        js("listen")
        show()
        let tr = SpeechTranscriber(locale: Locale(identifier: "en-US"), preset: .progressiveTranscription)
        let (stream, cont) = AsyncStream<AnalyzerInput>.makeStream()
        let an = SpeechAnalyzer(modules: [tr])
        feed = cont; analyzer = an
        let ctx = AnalysisContext(); ctx.contextualStrings[.general] = hints
        Task { try? await an.setContext(ctx); try? await an.start(inputSequence: stream) }
        reading = Task { [weak self] in   // finished pieces add up; the live (volatile) piece is shown after them
            var done = ""
            do {
                for try await r in tr.results {
                    let piece = String(r.text.characters)
                    if r.isFinal { done += piece }
                    let now = r.isFinal ? done : done + piece
                    await MainActor.run { self?.heard(now) }
                }
            } catch {}
        }
        let input = engine.inputNode, natural = input.outputFormat(forBus: 0)
        let want = speechFormat ?? natural
        let conv = want == natural ? nil : AVAudioConverter(from: natural, to: want)
        input.removeTap(onBus: 0)
        var n = 0
        input.installTap(onBus: 0, bufferSize: 1024, format: natural) { [weak self] buf, _ in
            if let conv, let out = AVAudioPCMBuffer(pcmFormat: want, frameCapacity: AVAudioFrameCount(Double(buf.frameLength) * want.sampleRate / natural.sampleRate) + 1) {
                var given = false
                conv.convert(to: out, error: nil) { _, status in
                    if given { status.pointee = .noDataNow; return nil }
                    given = true; status.pointee = .haveData; return buf
                }
                cont.yield(AnalyzerInput(buffer: out))
            } else {
                cont.yield(AnalyzerInput(buffer: buf))
            }
            n += 1
            guard n % 2 == 0, let ch = buf.floatChannelData?[0], buf.frameLength > 0 else { return }
            var sum: Float = 0
            for i in 0..<Int(buf.frameLength) { sum += ch[i] * ch[i] }
            let v = Double(min(1, sqrt(sum / Float(buf.frameLength)) * 14))
            DispatchQueue.main.async { self?.js("level", v) }
        }
        engine.prepare()
        do { try engine.start() } catch { js("msg", "Mic error: \(error.localizedDescription)"); hide(after: 3); return }
        let began = Date()
        Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] t in
            guard let self, engine.isRunning else { return t.invalidate() }
            if NSEvent.modifierFlags.isDisjoint(with: [.control, .option]) || Date().timeIntervalSince(began) > 60 { t.invalidate(); stop() }
        }
    }

    func heard(_ now: String) {
        guard engine.isRunning || busy else { return }
        text = now
        js("words", text)
        tryFast()   // every new word: "can you open Spotify and then…" opens Spotify mid-sentence
        pause?.cancel()   // a short pause in speech = try what was said so far as an instant command
        let item = DispatchWorkItem { [weak self] in self?.tryFast() }
        pause = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.55, execute: item)
        if !ahead.isEmpty && key(rest()) != key(ahead) { dropAhead() }   // the user kept talking: that start was for half a sentence
        aheadItem?.cancel()   // a longer pause = the user is probably done: start the AI now, keys still held
        let go = DispatchWorkItem { [weak self] in self?.startAhead() }
        aheadItem = go
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7, execute: go)
    }

    func rest() -> String { let w = words(); return w.count > consumed ? w[consumed...].joined(separator: " ") : "" }
    func key(_ s: String) -> String { s.lowercased().filter { $0.isLetter || $0.isNumber } }   // ignores the punctuation speech adds later

    func startAhead() {
        let r = rest()
        guard engine.isRunning, !fastBusy, !r.isEmpty, key(r) != key(ahead) else { return }
        dropAhead()
        ahead = r
        run(r)
        warm = spawn()   // ready again in case the user keeps talking
    }

    func dropAhead() { if !ahead.isEmpty { proc?.terminate(); proc = nil; ahead = "" } }

    func stop() {
        guard engine.isRunning else { return }
        engine.stop(); engine.inputNode.removeTap(onBus: 0); feed?.finish()
        busy = true
        js("think")   // keys are up: stop saying Listening now, not when the last words land
        let an = analyzer, rd = reading
        stops += 1
        let g = stops
        let once = { [weak self] in   // whichever comes first (words landed, or 2 s) goes on; the other does nothing
            guard let self, g == stops else { return }
            stops += 1; pause?.cancel(); finish()
        }
        Task {   // let the model finish the last words, then act
            try? await an?.finalizeAndFinishThroughEndOfInput()
            await rd?.value
            await MainActor.run { once() }
        }
        // finalizing can hang (seen Oct 9): after 2 s go with what was heard
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self, g == stops else { return }
            rd?.cancel(); Task { await an?.cancelAndFinishNow() }
            once()
        }
    }

    func words() -> [Substring] { text.split(separator: " ") }

    // Runs `atticus.py --fast` on the words not yet used; exit 0 means it was a simple command and it is done.
    func tryFast() {
        let w = words()
        guard engine.isRunning, !fastBusy, w.count > consumed else { return }
        let seg = w[consumed...].joined(separator: " "), n = w.count, front = mine
        fastBusy = true
        DispatchQueue.global().async { [weak self] in
            let p = py(["--fast", seg], ["ATTICUS_FRONT": front]), out = Pipe()
            p.standardOutput = out
            try? p.run(); p.waitUntilExit()
            let line = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                fastBusy = false
                if p.terminationStatus == 0 && line.hasPrefix("✓") {
                    consumed = n; did.append(String(line.dropFirst(2)))
                    js("line", "▸ " + line.dropFirst(2))
                }
            }
        }
    }

    func finish() {
        guard busy else { return }   // cancelled while the last words landed
        if fastBusy { DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [self] in finish() }; return }
        aheadItem?.cancel()
        let rest = rest()
        if !rest.isEmpty && key(rest) == key(ahead) {   // already started on exactly this while the user talked
            ahead = ""; unwarm()
            if proc == nil { busy = false; hide(after: max(6, min(30, Double(answer.count) / 12))) }   // and already done
            return
        }
        dropAhead()
        if !rest.isEmpty { return run(rest) }
        busy = false; unwarm()   // nothing left for the AI
        if did.isEmpty { js("msg", "Didn't catch that."); return hide(after: 1.2) }
        answer = "✓ " + did.joined(separator: " ")
        js("line", answer); lastDone = Date(); sound("Glass", 0.12); hide(after: 5)
    }

    // atticus.py --wait, started at key-down so Claude is up by the time the user lets go; run() hands it the words.
    func spawn() -> (p: Process, input: FileHandle)? {
        let p = py(["--wait"], ["ATTICUS_UI": "1", "ATTICUS_FRONT": mine])
        let pipe = Pipe(), inp = Pipe(); p.standardOutput = pipe; p.standardInput = inp
        var buffer = ""
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            guard let s = String(data: h.availableData, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                buffer += s
                while let r = buffer.range(of: "\n") {
                    let line = String(buffer[..<r.lowerBound]); buffer.removeSubrange(..<r.upperBound)
                    if line == "! setup" { onboarding.open(); continue }   // Claude not installed, signed out or no plan
                    if line.hasPrefix("✓") { self?.answer = line }
                    self?.js("line", line)
                }
            }
        }
        p.terminationHandler = { [weak self] _ in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                guard let self, self.proc === p else { return }   // a cancelled run ending late must not touch the next one
                self.busy = false; self.proc = nil
                if !self.answer.isEmpty { self.lastDone = Date(); self.sound("Glass", 0.12) }
                self.hide(after: max(6, min(30, Double(self.answer.count) / 12)))   // longer answers stay longer
            }
        }
        do { try p.run() } catch { return nil }
        return (p, inp.fileHandleForWriting)
    }

    func unwarm() { warm?.p.terminate(); warm = nil }

    func run(_ t: String) {
        busy = true; answer = ""
        js("think")
        guard let w = warm ?? spawn() else { busy = false; js("msg", "Could not start atticus.py"); return hide(after: 3) }
        warm = nil; proc = w.p
        try? w.input.write(contentsOf: Data((mine + "\t" + t.replacingOccurrences(of: "\n", with: " ") + "\n").utf8)); try? w.input.close()
    }
}

// First-run window (ui/onboarding.html): welcome, then each macOS permission with an Allow button, then how to use it.
// Shows until finished once; to see it again: defaults delete com.atticus.app onboarded, then restart the app.
final class Onboarding: NSObject, WKScriptMessageHandler, NSWindowDelegate {
    var win: NSWindow?, web: WKWebView?, timer: Timer?
    let pane = "x-apple.systempreferences:com.apple.preference.security?Privacy_"

    func open() {
        guard win == nil else { win?.makeKeyAndOrderFront(nil); return }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 640),
                         styleMask: [.titled, .closable, .fullSizeContentView], backing: .buffered, defer: false)
        w.titlebarAppearsTransparent = true; w.titleVisibility = .hidden; w.isMovableByWindowBackground = true
        w.isReleasedWhenClosed = false; w.delegate = self
        let bg = NSVisualEffectView(frame: w.contentRect(forFrameRect: w.frame))
        bg.material = .underWindowBackground; bg.blendingMode = .behindWindow; bg.state = .active
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "ob")
        let v = WKWebView(frame: NSRect(x: 0, y: 0, width: 640, height: 612), configuration: cfg)   // top 28 pt stays draggable
        v.setValue(false, forKey: "drawsBackground")
        let html = (try? String(contentsOfFile: dir + "/ui/onboarding.html", encoding: .utf8)) ?? "missing onboarding.html"
        let orbs = (try? String(contentsOfFile: dir + "/ui/orbs.js", encoding: .utf8)) ?? ""
        v.loadHTMLString(html.replacingOccurrences(of: "/*ORBS*/", with: orbs), baseURL: nil)
        bg.addSubview(v); w.contentView = bg
        win = w; web = v
        w.center(); NSApp.activate(ignoringOtherApps: true); w.makeKeyAndOrderFront(nil)
        var tick = 0
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in   // keys 20×/s, permissions 1×/s
            guard let self else { return }
            let f = NSEvent.modifierFlags
            web?.evaluateJavaScript("ob.keys(\(f.contains(.control)), \(f.contains(.option)))")
            tick += 1
            if tick % 20 == 1 { push() }
            if tick % 100 == 1 { checkPlan() }   // every 5 s: it starts Claude Code, too slow for every second
        }
    }

    // Which permissions are on, sent to the page every second (System Settings changes them behind our back).
    func push() {
        DispatchQueue.global().async { [weak self] in
            var se = AEAddressDesc()
            let id = "com.apple.systemevents"
            _ = id.withCString { AECreateDesc(typeApplicationBundleID, $0, id.utf8.count, &se) }
            let auto = AEDeterminePermissionToAutomateTarget(&se, typeWildCard, typeWildCard, false) == noErr
            AEDisposeDesc(&se)
            let s: [String: Bool] = ["mic": AVCaptureDevice.authorizationStatus(for: .audio) == .authorized,
                                     "speech": SFSpeechRecognizer.authorizationStatus() == .authorized,
                                     "ax": AXIsProcessTrusted(), "screen": CGPreflightScreenCaptureAccess(), "auto": auto]
            DispatchQueue.main.async {
                guard let d = try? JSONSerialization.data(withJSONObject: s), let j = String(data: d, encoding: .utf8) else { return }
                self?.web?.evaluateJavaScript("ob.set(\(j))")
            }
        }
    }

    // Claude Code installed, signed in, on a paid plan? (atticus.py --check)
    static func plan(_ done: @escaping ([String: Any]) -> Void) {
        DispatchQueue.global().async {
            let p = py(["--check"]), out = Pipe(); p.standardOutput = out
            try? p.run(); p.waitUntilExit()
            let j = (try? JSONSerialization.jsonObject(with: out.fileHandleForReading.readDataToEndOfFile())) as? [String: Any]
            DispatchQueue.main.async { done(j ?? ["ok": false, "need": "install", "text": "Could not run Atticus’s helper (python3 missing?)."]) }
        }
    }

    func checkPlan() {
        Onboarding.plan { [weak self] c in
            guard let d = try? JSONSerialization.data(withJSONObject: c), let j = String(data: d, encoding: .utf8) else { return }
            self?.web?.evaluateJavaScript("ob.plan(\(j))")
        }
    }

    // The Claude row's button: install Claude Code, sign in, or upgrade, in a Terminal window the user can watch.
    func fixPlan() {
        Onboarding.plan { [weak self] c in
            let need = c["need"] as? String ?? ""
            if need == "upgrade" { NSWorkspace.shared.open(URL(string: "https://claude.ai/upgrade")!); return }
            let cmd = need == "install" ? "curl -fsSL https://claude.ai/install.sh | bash && ~/.local/bin/claude auth login" : "claude auth login"
            let f = NSTemporaryDirectory() + "atticus-claude.command"
            try? "#!/bin/zsh\nexport PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\"\necho 'Atticus: setting up Claude Code'\n\(cmd)\necho; echo 'Done. You can close this window.'\n".write(toFile: f, atomically: true, encoding: .utf8)
            chmod(f, 0o755)
            NSWorkspace.shared.open(URL(fileURLWithPath: f))
            self?.checkPlan()
        }
    }

    func tried() { web?.evaluateJavaScript("ob.tried()") }

    func settings(_ p: String) { NSWorkspace.shared.open(URL(string: pane + p)!) }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let b = m.body as? [String: String] else { return }
        if b["do"] == "done" { return finish() }
        switch b["id"] {
        case "mic":
            if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined { AVCaptureDevice.requestAccess(for: .audio) { _ in } }
            else { settings("Microphone") }
        case "speech":
            if SFSpeechRecognizer.authorizationStatus() == .notDetermined { SFSpeechRecognizer.requestAuthorization { _ in } }
            else { settings("SpeechRecognition") }
        case "ax":
            if !AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary) { settings("Accessibility") }
        case "claude":
            fixPlan()
        case "screen":
            if !CGRequestScreenCaptureAccess() { settings("ScreenCapture") }
        case "auto":
            DispatchQueue.global().async { [weak self] in   // asking blocks until the user answers the macOS prompt
                // macOS asks once per app it controls, and only about apps that are running. So ask for the usual
                // ones now, in one go: System Events (started if needed), then whichever of the others are open.
                let se = "com.apple.systemevents"
                if NSRunningApplication.runningApplications(withBundleIdentifier: se).isEmpty {
                    let c = NSWorkspace.OpenConfiguration(); c.activates = false
                    NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/System/Library/CoreServices/System Events.app"), configuration: c) { _, _ in }
                    for _ in 0..<30 where NSRunningApplication.runningApplications(withBundleIdentifier: se).isEmpty { usleep(100_000) }
                }
                var r = noErr
                for id in [se, "com.apple.finder", "com.google.Chrome", "com.apple.Safari", "com.apple.Music", "com.spotify.client",
                           "com.apple.Notes", "com.apple.reminders", "com.apple.Terminal"]
                where id == se || !NSRunningApplication.runningApplications(withBundleIdentifier: id).isEmpty {
                    var d = AEAddressDesc()
                    _ = id.withCString { AECreateDesc(typeApplicationBundleID, $0, id.utf8.count, &d) }
                    let x = AEDeterminePermissionToAutomateTarget(&d, typeWildCard, typeWildCard, true)
                    AEDisposeDesc(&d)
                    if id == se { r = x }
                }
                if r != noErr { DispatchQueue.main.async { self?.settings("Automation") } }
            }
        default: break
        }
    }

    // Screen Recording only takes effect after a relaunch; launchd (KeepAlive) starts us again right away.
    func finish() {
        UserDefaults.standard.set(true, forKey: "onboarded")
        let relaunch = !CGPreflightScreenCaptureAccess()
        win?.close()
        if relaunch { DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exit(0) } }
    }

    func windowWillClose(_ n: Notification) {
        timer?.invalidate(); timer = nil
        web?.configuration.userContentController.removeScriptMessageHandler(forName: "ob")
        web = nil; win = nil
    }
}

// `Atticus --look <screen #> <out.jpg>` (atticus.py --look): the screen without Atticus's own box on it, 1280 px wide.
if CommandLine.arguments.count == 4, CommandLine.arguments[1] == "--look" {
    let a = CommandLine.arguments, done = DispatchSemaphore(value: 0)
    let scr = NSScreen.screens[max(0, min((Int(a[2]) ?? 1) - 1, NSScreen.screens.count - 1))]
    let id = scr.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID ?? CGMainDisplayID()
    Task {
        defer { done.signal() }
        guard let all = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true),
              let d = all.displays.first(where: { $0.displayID == id }) ?? all.displays.first else { return }
        let me = all.applications.filter { $0.bundleIdentifier == Bundle.main.bundleIdentifier }
        let cfg = SCStreamConfiguration()
        cfg.width = 1280; cfg.height = Int(1280 * Double(d.height) / Double(d.width))
        guard let img = try? await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(display: d, excludingApplications: me, exceptingWindows: []), configuration: cfg),
              let jpg = NSBitmapImageRep(cgImage: img).representation(using: .jpeg, properties: [.compressionFactor: 0.7]) else { return }
        try? jpg.write(to: URL(fileURLWithPath: a[3]))
    }
    done.wait()
    exit(FileManager.default.fileExists(atPath: a[3]) ? 0 : 1)
}

let app = App()
let onboarding = Onboarding()
if !UserDefaults.standard.bool(forKey: "onboarded") { DispatchQueue.main.async { onboarding.open() } }
else { Onboarding.plan { c in if c["ok"] as? Bool != true { onboarding.open() } } }   // set up once, but Claude signed out since
var specs = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
             EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
InstallEventHandler(GetApplicationEventTarget(), { _, ev, _ in
    let pressed = GetEventKind(ev) == UInt32(kEventHotKeyPressed)
    var hk = EventHotKeyID()
    GetEventParameter(ev, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
    DispatchQueue.main.async { hk.id == 2 ? (pressed ? app.escape() : ()) : pressed ? app.start() : app.stop() }
    return noErr
}, 2, &specs, nil, nil)
var ref: EventHotKeyRef?
RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey),
                    EventHotKeyID(signature: 0x41545443, id: 1), GetApplicationEventTarget(), 0, &ref)

// Test hook: `defaults write com.atticus.app demo -string "<request>"` then restart runs it once without the mic.
if let demo = UserDefaults.standard.string(forKey: "demo") {
    UserDefaults.standard.removeObject(forKey: "demo")
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { app.js("listen"); app.show(); app.js("words", demo); app.run(demo) }
}

NSApplication.shared.setActivationPolicy(.accessory)
NSApplication.shared.run()
