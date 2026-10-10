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

// Settings, shared with atticus.py (model, screen, openOn, instant): ~/.config/atticus/settings.json
enum Prefs {
    static let path = NSString(string: "~/.config/atticus/settings.json").expandingTildeInPath
    static let defaults: [String: Any] = ["hotkeyCode": 49, "hotkeyMods": controlKey | optionKey, "hotkeyLabel": "⌃⌥Space", "login": true, "paused": false, "corner": "top-left", "linger": "normal",
        "soundStart": "Pop", "soundDone": "Glass", "soundStop": "Tink", "volume": 0.16, "model": "haiku", "instant": true,
        "ahead": true, "aheadDelay": 0.7, "screen": "auto", "openOn": "here",
        "speak": false, "autoCopy": false, "hideWhenSharing": true, "menuIcon": true, "autoUpdate": true,
        "learn": true, "pointer": false, "learnMe": true, "input": "voice", "boxW": 460, "boxH": 360]
    static var d: [String: Any] = defaults.merging(
        (try? JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: path)))) as? [String: Any] ?? [:]) { $1 }
    static func set(_ k: String, _ v: Any) {
        d[k] = v
        try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        if let j = try? JSONSerialization.data(withJSONObject: d, options: [.prettyPrinted, .sortedKeys]) { try? j.write(to: URL(fileURLWithPath: path)) }
    }
    static func bool(_ k: String) -> Bool { d[k] as? Bool ?? false }
    static func str(_ k: String) -> String { d[k] as? String ?? "" }
    static func num(_ k: String) -> Double { (d[k] as? NSNumber)?.doubleValue ?? 0 }
}

// The talk shortcut, any key plus modifiers, recorded in Settings: Carbon modifiers to register, and the
// modifier keys whose release ends listening (none for a lone F-key: its own key-up ends it).
func hotkey() -> (code: Int, mods: Int, flags: NSEvent.ModifierFlags) {
    let m = Int(Prefs.num("hotkeyMods"))
    var f: NSEvent.ModifierFlags = []
    if m & cmdKey != 0 { f.insert(.command) }; if m & shiftKey != 0 { f.insert(.shift) }
    if m & optionKey != 0 { f.insert(.option) }; if m & controlKey != 0 { f.insert(.control) }
    return (Int(Prefs.num("hotkeyCode")), m, f)
}

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

// The box can take typing (canBecomeKey) without pulling Atticus forward (nonactivating): the user's app stays in front.
final class KeyPanel: NSPanel { override var canBecomeKey: Bool { true } }

final class App: NSObject, WKScriptMessageHandler {
    let panel = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: max(340, Prefs.num("boxW")), height: 76),
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
    var dragging = false, fromTop = false   // moving or resizing the box; fromTop: dragging the top edge, so the bottom stays put
    var began = Date()   // when the shortcut went down: a quick tap opens the box to type, a hold talks
    var fresh = false   // opened by a tap and nothing said, typed or asked yet: the shortcut again puts it away
    var composing = false, compH: CGFloat = 0, clickTalk = false, quiet: DispatchWorkItem?   // typing box focused / its height from the bottom; mic button = talk without keys
    var hovering = false, xHot = false, pinned = false, stuck = false, lastDone = Date.distantPast   // pinned: the user clicked the box, so only Esc / click-out / ✕ close it; stuck: the user dragged it, so only ✕
    var mine = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""   // the app the user's in, never Atticus itself
    let recent = NSString(string: "~/.config/atticus/recent.jsonl").expandingTildeInPath
    func frame(_ f: NSRect) { panel.setFrame(f, display: true); panel.invalidateShadow() }   // instant: animating a window around a web view jitters
    // Which edges the point is on (window coordinates): 6 px edges, 14 px corners.
    func edges(_ p: NSPoint) -> (l: Bool, r: Bool, t: Bool, b: Bool)? {
        let w = panel.frame.width, h = panel.frame.height, c: CGFloat = 14
        let l = p.x < 6 || (p.x < c && (p.y < c || p.y > h - c)), r = p.x > w - 6 || (p.x > w - c && (p.y < c || p.y > h - c))
        let b = p.y < 6 || (p.y < c && (p.x < c || p.x > w - c)), t = p.y > h - 6 || (p.y > h - c && (p.x < c || p.x > w - c))
        return l || r || t || b ? (l, r, t, b) : nil
    }
    func overX(_ p: NSPoint) -> Bool { p.x > panel.frame.width - 42 && p.y > panel.frame.height - 42 }
    // Words the recognizer should expect: the user's app names plus a few of the user's own (free, on-device).
    lazy var hints: [String] = Array((["Atticus", "Chrome", "VS Code", "Peekaboo", "split screen", "study mode"] +
        ["/Applications", "/System/Applications", "/System/Applications/Utilities"].flatMap {
            ((try? FileManager.default.contentsOfDirectory(atPath: $0)) ?? []).filter { $0.hasSuffix(".app") }.map { String($0.dropLast(4)) }
        }).prefix(100))
    func sound(_ name: String, _ v: Float) { let s = NSSound(named: name); s?.volume = v; s?.play() }
    func cue(_ k: String) { let n = Prefs.str(k); if n != "None" { sound(n, Float(Prefs.num("volume"))) } }   // soundStart / soundDone / soundStop
    var voice: Process?   // `say`: the Mac's system voice (a Siri voice if picked in Spoken Content); AVSpeechSynthesizer can't use Siri voices
    @discardableResult func shush() -> Bool { guard let v = voice, v.isRunning else { return false }; v.terminate(); return true }
    func deliver(_ a: String) {   // Settings: read answers aloud, copy them
        if Prefs.bool("autoCopy") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(a, forType: .string) }
        if Prefs.bool("speak") {
            shush(); let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/say"); p.arguments = ["--", a]; try? p.run(); voice = p
        }
    }
    func linger(_ s: Double) -> Double { s * (["short": 0.5, "long": 2][Prefs.str("linger")] ?? 1) }

    override init() {
        let cfg = WKWebViewConfiguration()
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: max(340, Prefs.num("boxW")), height: 76), configuration: cfg)
        super.init()
        cfg.userContentController.add(self, name: "h")
        cfg.userContentController.add(self, name: "c")
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        let bg = NSVisualEffectView(frame: web.frame)
        bg.material = .hudWindow; bg.state = .active; bg.wantsLayer = true; bg.autoresizingMask = [.width, .height]
        bg.appearance = NSAppearance(named: .darkAqua)   // stays dark over white pages, so the orb shows
        bg.layer?.cornerRadius = 18; bg.layer?.masksToBounds = true
        bg.maskImage = NSImage(size: NSSize(width: 37, height: 37), flipped: false) { NSColor.black.setFill(); NSBezierPath(roundedRect: $0, xRadius: 18, yRadius: 18).fill(); return true }
        bg.maskImage?.capInsets = NSEdgeInsets(top: 18, left: 18, bottom: 18, right: 18); bg.maskImage?.resizingMode = .stretch
        bg.addSubview(web)
        let tracker = Tracker(frame: web.frame)
        tracker.autoresizingMask = [.width, .height]
        tracker.onMove = { [weak self] p in
            guard let self else { return }
            if !hovering { js("hover", true) }
            hovering = true
            if (overX(p) && edges(p) == nil) != xHot { xHot.toggle(); js("xhot", xHot); (xHot ? NSCursor.pointingHand : NSCursor.arrow).set() }
        }
        tracker.onExit = { [weak self] in
            guard let self else { return }
            hovering = false; js("hover", false)
            if xHot { xHot = false; js("xhot", false); NSCursor.arrow.set() }
            if !busy { hide(after: 3) }   // mouse gone for a few seconds = close
        }
        bg.addSubview(tracker)
        panel.acceptsMouseMovedEvents = true
        panel.contentView = bg
        panel.level = .statusBar; panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.sharingType = Prefs.bool("hideWhenSharing") ? .none : .readOnly   // Settings: others on a screen share don't see it
        let html = (try? String(contentsOfFile: dir + "/ui/ui.html", encoding: .utf8)) ?? "missing ui.html"
        let orbs = (try? String(contentsOfFile: dir + "/ui/orbs.js", encoding: .utf8)) ?? ""
        web.loadHTMLString(html.replacingOccurrences(of: "/*ORBS*/", with: orbs), baseURL: nil)
        // Click anywhere else closes the box once the answer is up; on the box: ✕ closes, elsewhere copies the answer.
        NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            guard let self, panel.isVisible, !busy, !engine.isRunning, !stuck else { return }
            close(clear: false)
        }
        var grab: (mouse: NSPoint, origin: NSPoint)?, moved = false
        var sizing: (mouse: NSPoint, frame: NSRect, maxH: Double, e: (l: Bool, r: Bool, t: Bool, b: Bool))?   // dragging an edge or corner
        NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .leftMouseDragged, .leftMouseUp]) { [weak self] e in
            guard let self else { return e }
            switch e.type {
            case .leftMouseDown:
                guard e.window == panel else { return e }
                let p = e.locationInWindow
                if let ed = edges(p), !(overX(p) && !ed.r && !ed.t) { sizing = (NSEvent.mouseLocation, panel.frame, Prefs.num("boxH"), ed); fromTop = ed.t; pinned = true; dragging = true }
                else if overX(p) { close(clear: true) }
                else if p.y < compH { pinned = true; panel.makeKey(); panel.makeFirstResponder(web); return e }   // the typing box: a normal click
                else { pinned = true; grab = (NSEvent.mouseLocation, panel.frame.origin); moved = false; dragging = true }
            case .leftMouseDragged:
                if let z = sizing {   // width from the side edges; how tall the conversation may get from the top/bottom edges
                    let m = NSEvent.mouseLocation, dx = m.x - z.mouse.x, dy = m.y - z.mouse.y
                    let w = min(900, max(340, z.frame.width + (z.e.r ? dx : z.e.l ? -dx : 0)))
                    let h = min(900, max(120, z.maxH + (z.e.b ? -dy : z.e.t ? dy : 0)))
                    var f = panel.frame; f.size.width = w; f.origin.x = z.e.l ? z.frame.maxX - w : z.frame.minX; frame(f)
                    Prefs.d["boxW"] = w; Prefs.d["boxH"] = h
                    js("size", h)
                    return nil
                }
                guard let g = grab else { return e }
                let m = NSEvent.mouseLocation
                if abs(m.x - g.mouse.x) + abs(m.y - g.mouse.y) > 3 { moved = true; stuck = true }
                var f = panel.frame; f.origin = NSPoint(x: g.origin.x + m.x - g.mouse.x, y: g.origin.y + m.y - g.mouse.y); frame(f)
            default:
                dragging = false
                if sizing != nil { sizing = nil; DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in self?.fromTop = false }; Prefs.set("boxH", Prefs.num("boxH")); return nil }   // saves both
                guard grab != nil else { return e }
                grab = nil
                if !moved {   // a click, not a drag: the page decides (answer = copy, anywhere else = type)
                    panel.makeKey(); panel.makeFirstResponder(web)
                    js("tap", [e.locationInWindow.x, panel.frame.height - e.locationInWindow.y])
                }
            }
            return nil
        }
        NotificationCenter.default.addObserver(forName: NSWindow.didResignKeyNotification, object: panel, queue: .main) { [weak self] _ in
            guard let self else { return }
            composing = false   // the page may not get a blur; the box must still be able to fade
            if !hovering && !busy { hide(after: 3) }
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

    // The page reports its height; grow the box down from the top-left corner. "c": the typing box.
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        if m.name == "c", let b = m.body as? [String: Any] {
            if let h = b["comp"] as? NSNumber { compH = CGFloat(h.doubleValue) }
            if let f = b["focus"] as? Bool {
                composing = f
                if !f && !hovering && !busy { hide(after: 3) }
            }
            if b["type"] != nil { fresh = false; if engine.isRunning { hush() } }   // clicked or typed in the box while listening: stop the mic quietly
            if b["copy"] != nil && !answer.isEmpty {
                NSPasteboard.general.clearContents(); NSPasteboard.general.setString(String(answer.dropFirst(2)), forType: .string); js("copied")
            }
            if let t = b["send"] as? String { typed(t, keep: true) }
            if b["mic"] != nil { fresh = false }
            if b["mic"] != nil && !busy && !engine.isRunning { clickTalk = true; start(); if !engine.isRunning { clickTalk = false } }
            if b["ready"] != nil { js("size", Prefs.num("boxH")) }
            return
        }
        guard let h = (m.body as? NSNumber).map({ CGFloat($0.doubleValue) }), h > 0 else { return }
        var f = panel.frame
        if !fromTop { f.origin.y = f.maxY - h }   // grows down from the top, unless the user is dragging the top edge
        f.size.height = h
        guard f != panel.frame else { return }
        frame(f)
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
        let x = Prefs.str("corner") == "top-right" ? vf.maxX - panel.frame.width - 14 : vf.minX + 14
        panel.setFrameOrigin(NSPoint(x: x, y: vf.maxY - panel.frame.height - 10))
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
            guard g == gen, !busy, !engine.isRunning, !hovering, !pinned, !composing else { return }
            NSAnimationContext.runAnimationGroup({ $0.duration = 0.3; panel.animator().alphaValue = 0 },
                                                 completionHandler: { [self] in if g == gen { gone() } })
        }
    }

    func gone() {
        panel.orderOut(nil); fresh = false; pinned = false; stuck = false; composing = false; js("compose", false)
        if let r = escRef { UnregisterEventHotKey(r); escRef = nil }
    }

    // Esc: stop listening, stop the answer, or close the box.
    func escape() {
        if shush() { return }
        if engine.isRunning {
            hush(); cue("soundStop"); js("stopped", "Stopped listening"); hide(after: 1.5)
        } else if busy {
            cancel()
        } else if panel.isVisible && !stuck {
            close(clear: false)
        }
    }

    func hush() {   // stop listening, drop what was heard, say nothing
        engine.stop(); engine.inputNode.removeTap(onBus: 0); feed?.finish(); reading?.cancel(); pause?.cancel(); unwarm()
        clickTalk = false; quiet?.cancel(); stops += 1   // stops: a late "words landed" from this listen does nothing
        aheadItem?.cancel(); dropAhead()
        let an = analyzer; Task { await an?.cancelAndFinishNow() }
        busy = false   // an early start dropped above never reports back (its exit handler sees proc changed)
    }

    func dismiss() {   // the shortcut again before anything happened: say so, then go
        if engine.isRunning { hush() }
        cue("soundStop"); js("stopped", "Answer stopped")
        let g = gen; DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [self] in if g == gen { close(clear: false) } }
    }

    func cancel() {
        proc?.terminate(); proc = nil; busy = false
        cue("soundStop"); js("stopped", "Answer stopped"); hide(after: 1.5)
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
        guard !Prefs.bool("paused") else { return }
        shush()
        if busy {   // pressed again while it works = cancel
            return cancel()
        }
        if fresh && panel.isVisible { return dismiss() }
        guard !engine.isRunning else { return }
        text = ""; consumed = 0; did = []; ahead = ""
        if !panel.isVisible && Date().timeIntervalSince(lastDone) > 30 { js("reset"); try? FileManager.default.removeItem(atPath: recent) }
        if Prefs.str("input") == "type" && !clickTalk {   // Settings: the shortcut opens the box ready to type
            if composing && panel.isVisible { return dismiss() }   // pressed again = put it away
            onboarding.tried(); show()
            panel.makeKey(); panel.makeFirstResponder(web); js("compose", true)
            return
        }
        cue("soundStart")
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
        began = Date()   // after the mic is up: a slow mic start must not turn a tap into a hold
        Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] t in
            guard let self, engine.isRunning else { return t.invalidate() }
            let f = hotkey().flags, held = Date().timeIntervalSince(began)
            if clickTalk && text.isEmpty && held > 8 { t.invalidate(); hush(); js("stopped", ""); return hide(after: 3) }   // nothing said: stop the mic quietly
            if (!clickTalk && !f.isEmpty && NSEvent.modifierFlags.isDisjoint(with: f)) || held > 60 { t.invalidate(); stop() }
        }
    }

    func heard(_ now: String) {
        guard engine.isRunning || busy else { return }
        text = now; if !now.trimmingCharacters(in: .whitespaces).isEmpty { fresh = false }
        js("words", text)
        tryFast()   // every new word: "can you open Spotify and then…" opens Spotify mid-sentence
        pause?.cancel()   // a short pause in speech = try what was said so far as an instant command
        let item = DispatchWorkItem { [weak self] in self?.tryFast() }
        pause = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.55, execute: item)
        if clickTalk {   // no keys to let go of: a 1.3 s pause ends it
            quiet?.cancel(); let q = DispatchWorkItem { [weak self] in self?.stop() }; quiet = q
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.3, execute: q)
        }
        if !ahead.isEmpty && key(rest()) != key(ahead) { dropAhead() }   // the user kept talking: that start was for half a sentence
        aheadItem?.cancel()   // a longer pause = the user is probably done: start the AI now, keys still held
        guard Prefs.bool("ahead") else { return }
        let go = DispatchWorkItem { [weak self] in self?.startAhead() }
        aheadItem = go
        DispatchQueue.main.asyncAfter(deadline: .now() + Prefs.num("aheadDelay"), execute: go)
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
        guard engine.isRunning, !clickTalk || text.isEmpty == false else { return }
        if !clickTalk && text.isEmpty && Date().timeIntervalSince(began) < 0.4 {   // tapped, not held: like Spotlight
            clickTalk = true; fresh = true; panel.makeKey(); panel.makeFirstResponder(web); js("compose", true); return
        }
        engine.stop(); engine.inputNode.removeTap(onBus: 0); feed?.finish()
        clickTalk = false; quiet?.cancel()
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

    func typed(_ t: String, keep: Bool = false) {   // "Ask Atticus…" from the menu bar, or the typing box (keep: it stays open for the next one)
        guard !busy, !engine.isRunning else { return }
        fresh = false; js("listen", keep); show(); js("words", t); run(t)
    }

    func words() -> [Substring] { text.split(separator: " ") }

    // Runs `atticus.py --fast` on the words not yet used; exit 0 means it was a simple command and it is done.
    func tryFast() {
        let w = words()
        guard Prefs.bool("instant"), engine.isRunning, !fastBusy, w.count > consumed else { return }
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
            if proc == nil { busy = false; hide(after: linger(max(6, min(30, Double(answer.count) / 12)))) }   // and already done
            return
        }
        dropAhead()
        if !rest.isEmpty { return run(rest) }
        busy = false; unwarm()   // nothing left for the AI
        if did.isEmpty { js("msg", "Didn't catch that."); return hide(after: 1.2) }
        answer = "✓ " + did.joined(separator: " ")
        js("line", answer); lastDone = Date(); cue("soundDone"); hide(after: linger(5))
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
                    if line.hasPrefix("@ ") { pointAt(line); continue }   // experimental "show me where"
                    if line.hasPrefix("✓") { self?.answer = line; self?.deliver(String(line.dropFirst(2))) }
                    self?.js("line", line)
                }
            }
        }
        p.terminationHandler = { [weak self] _ in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                guard let self, self.proc === p else { return }   // a cancelled run ending late must not touch the next one
                self.busy = false; self.proc = nil
                if !self.answer.isEmpty { self.lastDone = Date(); self.cue("soundDone") }
                DispatchQueue.global().async { Apps.sync() }
                self.hide(after: self.linger(max(6, min(30, Double(self.answer.count) / 12))))   // longer answers stay longer
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
            let s = Onboarding.perms()
            DispatchQueue.main.async {
                guard let d = try? JSONSerialization.data(withJSONObject: s), let j = String(data: d, encoding: .utf8) else { return }
                self?.web?.evaluateJavaScript("ob.set(\(j))")
            }
        }
    }

    static func perms() -> [String: Bool] {
        ["mic": AVCaptureDevice.authorizationStatus(for: .audio) == .authorized, "speech": SFSpeechRecognizer.authorizationStatus() == .authorized,
         "ax": AXIsProcessTrusted(), "screen": CGPreflightScreenCaptureAccess(), "auto": Apps.check("com.apple.systemevents", ask: false) == noErr]
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
        grant(b["id"] ?? "")
    }

    // A permission's Allow button, from setup or Settings. One click shows one thing: macOS's own box the first time
    // (its button lands on the right list), System Settings once macOS won't ask again. Both at once confused people.
    func grant(_ id: String) {
        switch id {
        case "mic":
            if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined { AVCaptureDevice.requestAccess(for: .audio) { _ in } }
            else { settings("Microphone") }
        case "speech":
            if SFSpeechRecognizer.authorizationStatus() == .notDetermined { SFSpeechRecognizer.requestAuthorization { _ in } }
            else { settings("SpeechRecognition") }
        case "ax":
            if UserDefaults.standard.bool(forKey: "askedAX") { if !AXIsProcessTrusted() { settings("Accessibility") } }
            else { UserDefaults.standard.set(true, forKey: "askedAX"); _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary) }
        case "claude":
            fixPlan()
        case "screen":
            // First click: only macOS's own prompt (its button lands on the right list). Opening Settings at the
            // same time put people in the wrong section; later clicks, when macOS won't prompt again, open Settings.
            if UserDefaults.standard.bool(forKey: "askedScreen") { if !CGPreflightScreenCaptureAccess() { settings("ScreenCapture") } }
            else { UserDefaults.standard.set(true, forKey: "askedScreen"); _ = CGRequestScreenCaptureAccess() }
        case "auto":
            Atticus.settings.open(tab: "apps")
        default: break
        }
    }

    // Screen Recording only takes effect after a relaunch; launchd (KeepAlive) starts us again right away.
    func finish() {
        UserDefaults.standard.set(true, forKey: "onboarded")
        let relaunch = !CGPreflightScreenCaptureAccess()
        win?.close()
        if relaunch { relaunchSoon() }
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

// "Show me where": a soft pulsing ring at a spot on screen for a few seconds. "@ x y" in top-left screen points.
var ring: NSPanel?
func pointAt(_ line: String) {
    let p = line.split(separator: " ").dropFirst().compactMap { Double($0) }
    guard p.count == 2, let main = NSScreen.screens.first else { return }
    let size: CGFloat = 64, c = NSPoint(x: p[0], y: main.frame.height - p[1])   // flip to Cocoa's bottom-left origin
    ring?.orderOut(nil)
    let w = NSPanel(contentRect: NSRect(x: c.x - size / 2, y: c.y - size / 2, width: size, height: size),
                    styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    w.level = .screenSaver; w.isOpaque = false; w.backgroundColor = .clear; w.ignoresMouseEvents = true; w.hasShadow = false
    w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
    let v = NSView(frame: NSRect(x: 0, y: 0, width: size, height: size)); v.wantsLayer = true
    let l = CAShapeLayer()
    l.path = CGPath(ellipseIn: CGRect(x: 8, y: 8, width: size - 16, height: size - 16), transform: nil)
    l.fillColor = NSColor.systemBlue.withAlphaComponent(0.18).cgColor; l.strokeColor = NSColor.systemBlue.cgColor; l.lineWidth = 3
    l.frame = v.bounds; v.layer?.addSublayer(l)
    let pulse = CABasicAnimation(keyPath: "transform.scale"); pulse.fromValue = 0.7; pulse.toValue = 1.1
    pulse.duration = 0.6; pulse.autoreverses = true; pulse.repeatCount = 6
    l.anchorPoint = CGPoint(x: 0.5, y: 0.5); l.position = CGPoint(x: size / 2, y: size / 2); l.bounds = v.bounds; l.add(pulse, forKey: "pulse")
    w.contentView = v; w.orderFrontRegardless(); ring = w
    DispatchQueue.main.asyncAfter(deadline: .now() + 6) { if ring === w { w.orderOut(nil); ring = nil } }
}

func jsonString(_ s: String) -> String { String(data: try! JSONSerialization.data(withJSONObject: [s]), encoding: .utf8)!.dropFirst().dropLast().description }

// Restart Atticus (Screen Recording only takes effect in a new process); works under launchd or when opened by hand.
func relaunchSoon() {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/sh")
    p.arguments = ["-c", "sleep 1; open \"$0\"", Bundle.main.bundlePath]
    try? p.run()
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exit(0) }
}

// Updates: build.sh records the source folder (a git clone); Atticus pulls and rebuilds itself from it, then
// restarts once idle. Local edits or a failed build leave the running version alone.
enum Updater {
    static let src = Bundle.main.object(forInfoDictionaryKey: "AtticusSource") as? String ?? ""
    static var status = ""
    static func sh(_ script: String) -> (code: Int32, out: String) {
        let p = Process(), o = Pipe()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh"); p.arguments = ["-c", script, src]; p.standardOutput = o; p.standardError = o
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"; env["ATTICUS_NO_RESTART"] = "1"; p.environment = env
        try? p.run(); p.waitUntilExit()
        return (p.terminationStatus, String(data: o.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? "")
    }
    static func version() -> String {
        sh(#"cd "$0" 2>/dev/null && git log -1 --format='%h · %cd' --date=format:'%b %-d, %Y'"#).out.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    static func check(_ done: @escaping () -> Void = {}) {
        status = "Checking…"; done()
        DispatchQueue.global().async {
            let r = sh(#"""
                cd "$0" 2>/dev/null || exit 3
                git fetch -q 2>/dev/null || exit 4
                [ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] && exit 0
                git pull -q --ff-only 2>/dev/null || exit 5
                ./build.sh >/dev/null 2>&1 || exit 6
                exit 10
                """#)
            DispatchQueue.main.async {
                status = [0: "Up to date", 3: "Source folder not found (reinstall with build.sh)", 4: "Couldn’t reach GitHub",
                          5: "Skipped: the source folder has local changes", 6: "Update downloaded, but the build failed",
                          10: "Updated. Restarting when you’re not using it…"][r.code] ?? "Update failed"
                done()
                if r.code == 10 { restartWhenIdle() }
            }
        }
    }
    static func restartWhenIdle() {
        if app.busy || app.engine.isRunning || settings.win?.isKeyWindow == true {
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) { restartWhenIdle() }
        } else { relaunchSoon() }
    }
    static func schedule() {   // a minute after launch, then every 6 hours
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { if Prefs.bool("autoUpdate") { check() } }
        Timer.scheduledTimer(withTimeInterval: 6 * 3600, repeats: true) { _ in if Prefs.bool("autoUpdate") { check() } }
    }
}

// Opening Atticus again (Spotlight, Finder, Dock) while it runs: setup the first time, Settings after.
final class Delegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        UserDefaults.standard.bool(forKey: "onboarded") ? settings.open() : onboarding.open()
        return false
    }
}

// The menu bar icon: quick actions, today's usage.
final class MenuBar: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    override init() {
        super.init()
        item.button?.image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Atticus")
        let m = NSMenu(); m.delegate = self; item.menu = m
        item.isVisible = Prefs.bool("menuIcon")
        paint()
    }
    func paint() { item.button?.appearsDisabled = Prefs.bool("paused") }
    func menuNeedsUpdate(_ m: NSMenu) {
        m.removeAllItems()
        let keys = Prefs.str("hotkeyLabel")
        let head = NSMenuItem(title: Prefs.bool("paused") ? "Atticus is paused" : "Hold \(keys) and talk", action: nil, keyEquivalent: "")
        head.isEnabled = false; m.addItem(head)
        m.addItem(.separator())
        add(m, "Ask Atticus…", #selector(ask), "")
        add(m, Prefs.bool("paused") ? "Resume Atticus" : "Pause Atticus", #selector(pause), "")
        m.addItem(.separator())
        let u = NSMenuItem(title: today(), action: nil, keyEquivalent: ""); u.isEnabled = false; m.addItem(u)
        m.addItem(.separator())
        add(m, "Settings…", #selector(openSettings), ",")
        add(m, "Setup & Permissions…", #selector(openSetup), "")
        m.addItem(.separator())
        add(m, "Quit Atticus", #selector(quit), "q")
    }
    func add(_ m: NSMenu, _ t: String, _ a: Selector, _ k: String) { let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.target = self; m.addItem(i) }
    func today() -> String {
        let day = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; return f.string(from: Date()) }()
        let log = (try? String(contentsOfFile: NSString(string: "~/.config/atticus/log.jsonl").expandingTildeInPath, encoding: .utf8)) ?? ""
        let rows = log.split(separator: "\n").compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            .filter { ($0["at"] as? String ?? "").hasPrefix(day) }
        let usd = rows.reduce(0.0) { $0 + (($1["usd"] as? NSNumber)?.doubleValue ?? 0) }
        return "Today: \(rows.count) ask\(rows.count == 1 ? "" : "s") · " + (usd < 1 ? String(format: "%.2f¢", usd * 100) : String(format: "$%.2f", usd))
    }
    @objc func ask() {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert(); a.messageText = "Ask Atticus"; a.informativeText = "Type what you would say."
        let f = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24)); f.placeholderString = "What’s the weather tomorrow?"
        a.accessoryView = f; a.addButton(withTitle: "Ask"); a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = f
        if a.runModal() == .alertFirstButtonReturn, !f.stringValue.trimmingCharacters(in: .whitespaces).isEmpty { app.typed(f.stringValue) }
    }
    @objc func pause() { Prefs.set("paused", !Prefs.bool("paused")); paint(); settings.push() }
    @objc func openSettings() { settings.open() }
    @objc func openSetup() { onboarding.open() }
    @objc func quit() { NSApp.terminate(nil) }
}

// Settings window (ui/settings.html): every setting lives in Prefs; atticus.py reads the same file.
// Apps Atticus may control. macOS asks once per app, only while that app runs, and has no "allow all". So Settings > Apps
// lists every installed app and asks for all the ticked ones in one go: start each hidden, ask, quit it again.
// macOS only answers "allowed?" for running apps, so what it said is kept in UserDefaults ("allowedApps").
enum Apps {
    static let suggested: Set<String> = ["com.apple.systemevents", "com.apple.finder", "com.apple.Safari", "com.google.Chrome",
        "company.thebrowser.Browser", "com.brave.Browser", "com.apple.Notes", "com.apple.reminders", "com.apple.iCal", "com.apple.Music",
        "com.spotify.client", "com.apple.Terminal", "com.googlecode.iterm2", "com.apple.mail", "com.apple.MobileSMS", "com.apple.Preview",
        "com.apple.TextEdit", "com.apple.systempreferences", "com.microsoft.VSCode", "com.apple.shortcuts", "com.apple.AddressBook",
        "com.apple.Photos", "com.apple.iWork.Pages", "com.apple.iWork.Keynote", "com.apple.iWork.Numbers", "com.microsoft.Word",
        "com.microsoft.Excel", "com.microsoft.Powerpoint", "com.tinyspeck.slackmacgap", "us.zoom.xos", "com.hnc.Discord", "notion.id"]
    static var icons: [String: String] = [:]   // bundle id -> data URL, made once per launch
    static var stopped = false   // Settings' Stop button

    static var allowed: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: "allowedApps") ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: "allowedApps") }
    }
    static func check(_ id: String, ask: Bool) -> OSStatus {   // ask: show macOS's prompt if it never asked (blocks until answered)
        var d = AEAddressDesc()
        _ = id.withCString { AECreateDesc(typeApplicationBundleID, $0, id.utf8.count, &d) }
        defer { AEDisposeDesc(&d) }
        return AEDeterminePermissionToAutomateTarget(&d, typeWildCard, typeWildCard, ask)
    }
    // "allowed" | "blocked" | "new" per app, asked all at once: a busy or frozen app can stall macOS's answer forever,
    // so whatever isn't back in 1.5 s keeps what was saved before.
    @discardableResult static func statuses(_ ids: [String]) -> [String: String] {
        var codes = [String: OSStatus](); let lock = NSLock(), g = DispatchGroup()
        for id in ids {
            g.enter()
            DispatchQueue.global().async { let r = check(id, ask: false); lock.lock(); codes[id] = r; lock.unlock(); g.leave() }
        }
        _ = g.wait(timeout: .now() + 1.5)
        lock.lock(); let got = codes; lock.unlock()
        var a = allowed, out = [String: String]()
        for id in ids {
            if got[id] == noErr { a.insert(id) } else if got[id] == OSStatus(errAEEventNotPermitted) { a.remove(id) }
            out[id] = got[id] == OSStatus(errAEEventNotPermitted) ? "blocked" : a.contains(id) ? "allowed" : "new"
        }
        allowed = a
        return out
    }
    static func sync() {   // after an ask: an app allowed in the moment, at macOS's own prompt, joins the list
        statuses(NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }.compactMap(\.bundleIdentifier))
    }
    static func png(_ img: NSImage) -> String {
        guard let r = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 64, pixelsHigh: 64, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                       isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return "" }
        NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: r)
        img.draw(in: NSRect(x: 0, y: 0, width: 64, height: 64)); NSGraphicsContext.restoreGraphicsState()
        return "data:image/png;base64," + (r.representation(using: .png, properties: [:])?.base64EncodedString() ?? "")
    }
    static func list() -> [[String: Any]] {
        let fm = FileManager.default
        let dirs = ["/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities", NSHomeDirectory() + "/Applications"]
        let paths = dirs.flatMap { d in ((try? fm.contentsOfDirectory(atPath: d)) ?? []).filter { $0.hasSuffix(".app") }.map { d + "/" + $0 } }
            + ["/System/Library/CoreServices/Finder.app", "/System/Library/CoreServices/System Events.app"]
        var seen = Set<String>(), out: [[String: Any]] = []
        for p in paths {
            guard let id = Bundle(path: p)?.bundleIdentifier, id != Bundle.main.bundleIdentifier, seen.insert(id).inserted else { continue }
            if icons[id] == nil { icons[id] = png(NSWorkspace.shared.icon(forFile: p)) }
            var name = fm.displayName(atPath: p); if name.hasSuffix(".app") { name.removeLast(4) }
            out.append(["id": id, "name": name, "icon": icons[id]!, "suggested": suggested.contains(id)])
        }
        let st = statuses(out.map { $0["id"] as! String })
        for i in out.indices { out[i]["status"] = st[out[i]["id"] as! String] }
        return out.sorted { ($0["name"] as! String).localizedCaseInsensitiveCompare($1["name"] as! String) == .orderedAscending }
    }
    static func allow(_ ids: [String], _ progress: @escaping (String, String) -> Void) {   // off the main thread
        stopped = false
        for id in ids where !stopped {
            var started: NSRunningApplication?
            if NSRunningApplication.runningApplications(withBundleIdentifier: id).isEmpty, let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: id) {
                let c = NSWorkspace.OpenConfiguration(); c.activates = false; c.hides = true; c.addsToRecentItems = false
                let done = DispatchSemaphore(value: 0)
                NSWorkspace.shared.openApplication(at: url, configuration: c) { a, _ in started = a; done.signal() }
                _ = done.wait(timeout: .now() + 15)
            }
            progress(id, "asking")
            let answered = DispatchSemaphore(value: 0)   // some apps (Spotify) never answer: move on after 20 s
            DispatchQueue.global().async { _ = check(id, ask: true); answered.signal() }
            _ = answered.wait(timeout: .now() + 20)
            started?.terminate()   // only the ones Atticus started
            progress(id, statuses([id])[id]!)
        }
    }
}

final class SettingsWindow: NSObject, WKScriptMessageHandler, WKNavigationDelegate, NSWindowDelegate {
    var win: NSWindow?, web: WKWebView?

    var tab = "", timer: Timer?   // tab: shown once the page loads

    func open(tab t: String = "") {
        NSApp.activate(ignoringOtherApps: true)
        if let w = win { w.makeKeyAndOrderFront(nil); refresh(); if !t.isEmpty { js("st.tab('\(t)')") }; return }
        tab = t
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 560),
                         styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView], backing: .buffered, defer: false)
        w.titlebarAppearsTransparent = true; w.titleVisibility = .hidden; w.title = "Atticus Settings"
        w.isReleasedWhenClosed = false; w.delegate = self
        let bg = NSVisualEffectView(frame: w.contentRect(forFrameRect: w.frame))
        bg.material = .underWindowBackground; bg.blendingMode = .behindWindow; bg.state = .active
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "st")
        let v = WKWebView(frame: bg.bounds, configuration: cfg)
        v.autoresizingMask = [.width, .height]; v.setValue(false, forKey: "drawsBackground"); v.navigationDelegate = self
        v.loadHTMLString((try? String(contentsOfFile: dir + "/ui/settings.html", encoding: .utf8)) ?? "missing settings.html", baseURL: nil)
        bg.addSubview(v); w.contentView = bg
        win = w; web = v
        w.center(); w.makeKeyAndOrderFront(nil)
        timer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in   // System Settings changes them behind our back
            DispatchQueue.global().async { self?.send("perms", Onboarding.perms()) }
        }
    }

    func webView(_ v: WKWebView, didFinish n: WKNavigation!) { push(); refresh(); DispatchQueue.global().async { [weak self] in self?.send("perms", Onboarding.perms()) }; if !tab.isEmpty { js("st.tab('\(tab)')"); tab = "" } }

    func send(_ call: String, _ v: Any) {
        guard let d = try? JSONSerialization.data(withJSONObject: v), let j = String(data: d, encoding: .utf8) else { return }
        DispatchQueue.main.async { [weak self] in self?.js("st.\(call)(\(j))") }
    }

    func push() {   // the settings, and the app icon for the sidebar
        var d: [String: Any] = ["prefs": Prefs.d, "version": Updater.version(), "update": Updater.status]
        if let t = NSApp.applicationIconImage?.tiffRepresentation, let png = NSBitmapImageRep(data: t)?.representation(using: .png, properties: [:]) {
            d["icon"] = "data:image/png;base64," + png.base64EncodedString()
        }
        guard let j = try? JSONSerialization.data(withJSONObject: d), let s = String(data: j, encoding: .utf8) else { return }
        web?.evaluateJavaScript("st.load(\(s))")
    }

    func refresh() {   // usage, saved rules, Claude plan: atticus.py --state (it runs Claude Code, so off the main thread)
        DispatchQueue.global().async { [weak self] in
            let p = py(["--state"]), out = Pipe(); p.standardOutput = out
            try? p.run(); p.waitUntilExit()
            let s = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            guard s.hasPrefix("{") else { return }
            DispatchQueue.main.async { self?.web?.evaluateJavaScript("st.state(\(s))") }
        }
    }

    // "Change shortcut": the next key pressed with modifiers (or a lone F-key) becomes the talk shortcut. Esc cancels.
    var monitor: Any?
    func record() {
        if let r = ref { UnregisterEventHotKey(r); ref = nil }   // so pressing the old one doesn't start listening
        let fkeys = [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90]
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .flagsChanged]) { [weak self] e in
            guard let self else { return e }
            let f = e.modifierFlags.intersection([.command, .shift, .option, .control])
            let mods = (f.contains(.control) ? "⌃" : "") + (f.contains(.option) ? "⌥" : "") + (f.contains(.shift) ? "⇧" : "") + (f.contains(.command) ? "⌘" : "")
            if e.type == .flagsChanged { js("st.rec(\(jsonString(mods)))"); return nil }
            if e.keyCode == 53 && f.isEmpty { done(nil); return nil }   // Esc
            let fn = fkeys.firstIndex(of: Int(e.keyCode))
            guard !f.isEmpty || fn != nil else { js("st.rec(\(jsonString("Add ⌃, ⌥, ⇧ or ⌘")))"); return nil }
            let names: [UInt16: String] = [49: "Space", 36: "Return", 48: "Tab", 51: "Delete", 123: "←", 124: "→", 125: "↓", 126: "↑"]
            let key = fn.map { "F\($0 + 1)" } ?? names[e.keyCode] ?? (e.charactersIgnoringModifiers ?? "?").uppercased()
            var c = 0
            if f.contains(.command) { c |= cmdKey }; if f.contains(.shift) { c |= shiftKey }
            if f.contains(.option) { c |= optionKey }; if f.contains(.control) { c |= controlKey }
            done((Int(e.keyCode), c, mods + key))
            return nil
        }
    }

    func done(_ new: (code: Int, mods: Int, label: String)?) {
        if let m = monitor { NSEvent.removeMonitor(m); monitor = nil }
        let old = (Prefs.d["hotkeyCode"], Prefs.d["hotkeyMods"], Prefs.d["hotkeyLabel"])
        if let n = new { Prefs.set("hotkeyCode", n.code); Prefs.set("hotkeyMods", n.mods); Prefs.set("hotkeyLabel", n.label) }
        if !registerHotkey() {   // taken by another app: keep the old one
            Prefs.set("hotkeyCode", old.0!); Prefs.set("hotkeyMods", old.1!); Prefs.set("hotkeyLabel", old.2!)
            registerHotkey()
            js("st.taken()")
        }
        push()
    }

    func js(_ s: String) { web?.evaluateJavaScript(s) }

    func helper(_ args: [String]) { let p = py(args); try? p.run(); p.waitUntilExit() }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard let b = m.body as? [String: Any], let what = b["do"] as? String else { return }
        switch what {
        case "set":
            guard let k = b["k"] as? String, let v = b["v"] else { return }
            Prefs.set(k, v)
            if k == "hideWhenSharing" { app.panel.sharingType = v as? Bool == true ? .none : .readOnly }
            if k == "menuIcon" { bar.item.isVisible = v as? Bool == true }
            if k == "paused" { bar.paint() }
            if k == "login" {   // the LaunchAgent build.sh installed: enabled = starts at login
                let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
                p.arguments = [v as? Bool == true ? "enable" : "disable", "gui/\(getuid())/com.atticus.app"]; try? p.run()
            }
        case "record": record()
        case "unlearn":
            guard let say = b["say"] as? String else { return }
            DispatchQueue.global().async { [weak self] in self?.helper(["--unlearn", say]); DispatchQueue.main.async { self?.refresh() } }
        case "checkUpdate": Updater.check { [weak self] in self?.push() }
        case "github": NSWorkspace.shared.open(URL(string: "https://github.com/SaarthurR/atticus-mac")!)
        case "resetHotkey":
            Prefs.set("hotkeyCode", 49); Prefs.set("hotkeyMods", controlKey | optionKey); Prefs.set("hotkeyLabel", "⌃⌥Space")
            registerHotkey(); push()
        case "preview":
            if let n = b["sound"] as? String { app.sound(n, Float(Prefs.num("volume"))) }
        case "setup": onboarding.open()
        case "openLog": NSWorkspace.shared.open(URL(fileURLWithPath: NSString(string: "~/.config/atticus/log.jsonl").expandingTildeInPath))
        case "clearRecent": try? FileManager.default.removeItem(atPath: app.recent); app.lastDone = .distantPast
        case "me":   // About you: name / about / drop a fact
            guard let what = b["what"] as? String, let text = b["text"] as? String else { return }
            DispatchQueue.global().async { [weak self] in self?.helper(["--me", what, text]); DispatchQueue.main.async { self?.refresh() } }
        case "forget", "remember":
            guard let line = b["line"] as? String else { return }
            DispatchQueue.global().async { [weak self] in self?.helper(["--" + what, line]); DispatchQueue.main.async { self?.refresh() } }
        case "apps": DispatchQueue.global().async { [weak self] in self?.send("apps", Apps.list()) }
        case "allow":
            guard let ids = b["ids"] as? [String] else { return }
            DispatchQueue.global().async { [weak self] in
                Apps.allow(ids) { id, s in self?.send("appStatus", [id, s]) }
                self?.send("allowDone", 0)
            }
        case "grant": if let id = b["id"] as? String { onboarding.grant(id) }
        case "stopAllow": Apps.stopped = true
        case "automation": NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")!)
        case "quit": NSApp.terminate(nil)
        default: break
        }
    }

    func windowWillClose(_ n: Notification) {
        if monitor != nil { done(nil) }   // closed mid-recording: keep the old shortcut
        timer?.invalidate(); timer = nil
        web?.configuration.userContentController.removeScriptMessageHandler(forName: "st")
        web = nil; win = nil
    }
}

// One Atticus at a time: a lock the system drops when the process ends (opened by hand + started by launchd = two)
let lockFD = open(NSString(string: "~/.config/atticus/app.lock").expandingTildeInPath, O_CREAT | O_RDWR, 0o644)
if lockFD >= 0 && flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
    for _ in 0..<15 where flock(lockFD, LOCK_EX | LOCK_NB) != 0 { usleep(100_000) }   // a restart: the old one is exiting
    if flock(lockFD, LOCK_EX | LOCK_NB) != 0 { exit(0) }
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
@discardableResult func registerHotkey() -> Bool {   // false: another app already owns that shortcut
    if let r = ref { UnregisterEventHotKey(r); ref = nil }
    let h = hotkey()
    return RegisterEventHotKey(UInt32(h.code), UInt32(h.mods), EventHotKeyID(signature: 0x41545443, id: 1), GetApplicationEventTarget(), 0, &ref) == noErr
}
registerHotkey()
let settings = SettingsWindow()
let bar = MenuBar()
let delegate = Delegate()
NSApplication.shared.delegate = delegate
Updater.schedule()

// Test hook: `defaults write com.atticus.app demo -string "<request>"` then restart runs it once without the mic.
if let demo = UserDefaults.standard.string(forKey: "demo") {
    UserDefaults.standard.removeObject(forKey: "demo")
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { app.js("listen"); app.show(); app.js("words", demo); app.run(demo) }
}

NSApplication.shared.setActivationPolicy(.accessory)
NSApplication.shared.run()
