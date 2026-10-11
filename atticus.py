#!/usr/bin/env python3
"""Atticus: a small Mac helper run by Claude Haiku through the Claude Code CLI (your Claude plan). Idea from Caleb Writes Code's "Atticus".

Hold Control+Option+Space (Atticus.app) -> talk -> let go. It answers questions (web, files, screen) and does
tasks on the Mac. Output lines for the overlay: "· note", "▸ step", "✓ answer".
  atticus.py "what's on my screen?"      run one request (no args = a typed pop-up, used by the SwiftBar menu)
  atticus.py --place <proc> <left|right|full|center> <laptop|monitor|current> [title]
  atticus.py --look                       screenshot of the screen the user is using -> prints the image path
  atticus.py --front <app> [tab words]   bring that app (and that browser tab) forward, then --look
  atticus.py --show <url|path|app>       open it on the user's other screen and give focus back to what the user was using
  atticus.py --remember "<line>"  |  --forget "<words>"   the user's saved rules, one line each, sent with every request
  atticus.py --shortcut "<words>" "<shell command>"  the user's own command: those words then run it instantly, no AI
  atticus.py --fast "<words>"            run it instantly with no AI if it is a simple command (exit 1 if not)
  atticus.py --play "<playlist name>"     play one of the user's Spotify playlists, no clicks, focus stays put
  atticus.py --search <query>  |  --read <url>   free web search (DuckDuckGo) / one page as plain text
  atticus.py --watch <app> "<title words>" <gone|shows> "<phrase>" ["<request>"]   watch a window in the background, no AI, then notify (and run the request)
  atticus.py --pick "<question>" <option>...   the user picks one in the box (files get a preview) -> prints it
  atticus.py --set "<setting name>" on|off   flip one of Atticus's own switches (not safe mode or pause), no clicks
  atticus.py --unwatch                    stop every watch  |  --budget   is real use still under the cost and speed budget?
Completed requests go to ~/.config/atticus/log.jsonl with their cost (live actions are marked).
"""
import atexit, base64, html, json, shutil, threading, os, re, signal, subprocess, sys, tempfile, time, urllib.parse, urllib.request

HOME = os.path.expanduser('~')
DIR = os.environ.get('ATTICUS_DIR') or os.path.join(HOME, '.config/atticus')   # ATTICUS_DIR: tests keep off the user's chat and rules
ME = os.path.abspath(__file__)   # inside Atticus.app once built, so the AI's helper commands point here
PLACE, LOOK, SEARCH, READ, SHOW, REMEMBER, FORGET, TYPE, PLAY, ABOUT, SHORTCUT, FRONT, WATCH, PICK, SET = (ME + f' --{c}' for c in ('place', 'look', 'search', 'read', 'show', 'remember', 'forget', 'type', 'play', 'me add', 'shortcut', 'front', 'watch', 'pick', 'set'))
MEMORY, RECENT = os.path.join(DIR, 'memory.md'), os.path.join(DIR, 'recent.jsonl')
PREFS = os.path.join(DIR, 'settings.json')   # written by the Settings window
# $5 once, sold on Gumroad. shortcut: the source is public, so a coder can delete this check; fine for a $5 app.
GUMROAD_PRODUCT = '4o1C13Q132kPu-TW7b-sAA=='   # Gumroad product ID; empty = free
BUY = 'https://3370759087892.gumroad.com/l/atticus'
KEY_FILE = os.path.join(DIR, 'license')
# Daily-user count: once a day, a random ID (no name, no asks) goes to PostHog. Off in Settings → Safety. Empty key = off.
POSTHOG_KEY = os.environ.get('ATTICUS_POSTHOG_KEY', '')   # optional; no embedded API keys


# About the user: name, one line they wrote, up to 8 short facts Atticus picked up. Capped at 300 characters
# (~75 tokens, ~0.008¢ an ask) so knowing them never gets expensive. One writer: this file's --me commands.
ME_FILE = os.path.join(DIR, 'me.json')


def me():
    try:
        return {'name': '', 'about': '', 'facts': [], **json.load(open(ME_FILE))}
    except (OSError, ValueError):
        return {'name': '', 'about': '', 'facts': []}


def me_set(what, text):
    m, text = me(), ' '.join(text.split())
    if what in ('name', 'about'):
        m[what] = text[:40 if what == 'name' else 200]
    elif what == 'add' and text and text.lower() not in map(str.lower, m['facts']):
        m['facts'] = (m['facts'] + [text[:60]])[-8:]   # newest 8
    elif what == 'drop':
        m['facts'] = [f for f in m['facts'] if f != text]
    os.makedirs(DIR, exist_ok=True)
    json.dump(m, open(ME_FILE, 'w'), indent=1)


def me_line():
    m = me()
    parts = ([f"Name: {m['name']}."] if m['name'] else []) + ([m['about'].rstrip('.') + '.'] if m['about'] else []) + m['facts']
    return ' '.join(parts)[:300]


def pref(key, default):
    try:
        return json.load(open(PREFS)).get(key, default)
    except (OSError, ValueError):
        return default   # recent: Atticus.app deletes it on ✕
SHOT = '/tmp/atticus-screen'   # + -<n>.jpg, one per display
os.environ['PATH'] = f"/opt/homebrew/bin:/usr/local/bin:{HOME}/.local/bin:{HOME}/.claude/local:/usr/bin:/bin:/usr/sbin:" + os.environ.get('PATH', '')
APP = os.environ.get('ATTICUS_APP') or os.path.join(HOME, 'Applications/Atticus.app/Contents/MacOS/Atticus')   # set by Atticus.app

SYSTEM = f"""You are Atticus, a Mac helper. The user holds a hotkey, talks, lets go; your reply shows in a small box on the user's screen.
Requests are questions (find the answer and give it), tasks (do it on the Mac), or both ("find this author's bio on Google and open it" = look at the screen, search, open the best page).
Speed matters; every extra round costs the user a second. Answer stable facts without a tool. For current facts (news, prices, schedules, weather), search once and prefer a dated primary source. Never invent details missing from the screen or search results. Do the whole task in ONE Bash call when you can (chain with ;). Need several things? Ask for them all in the same turn, not one after another.
Before EACH tool call write a 3-8 word note of what you are doing; it shows live. Plain words only, never tool or command names (no peekaboo, screenshot, osascript): "Looking at your screen", "Clicking Play".
Do, don't describe: "make a list / add / remind me / note this" means create it in the app, never only list it back or offer to.
Final reply: plain text, no markdown, at most 3 short sentences, answer first. For a done task, one sentence saying it's done; no "but I didn't check".
Be cheap: fewest tool calls. Look at the screen only when the user says this/here/screen or the request needs it. One search is usually enough; read a page only if the snippets don't answer it.
Tools:
- See the user's screen: when the request is about it, a labeled picture of each of the user's screens comes with the request: use the one the user means ("my monitor" = the external one; "my screen" with no hint = the one the user's front window is on), don't take another. Otherwise run {LOOK} (prints an image path), then Read that path.
- The app, window or tab the request is about is not on screen (any app: "in Chrome, where do I click", "what's in my Excel sheet", "show me in Settings"): never say you can't see it and never ask the user to bring it up; run {FRONT} "<app>" "<words from the window or tab title, or site>" (brings it forward, switches to that window or tab, prints fresh screen pictures), Read the right one, then answer.
- Web (free, use first): {SEARCH} "<query>" -> top 5 titles, urls, snippets, plus the start of the top 3 pages (usually enough: answer from it). {READ} "<url>" -> that page as text. WebSearch/WebFetch only if those fail. 
- Open anything for the user (page, file, app): {SHOW} "<url | path | app name>". It opens where the user's Settings say (in front, or on the other screen with focus kept); don't open or focus it again.
- The user's files (Spotlight): mdfind -name "budget" | head -20  |  by content: mdfind -onlyin ~/Documents "photosynthesis" | head
- Weather: curl -s 'wttr.in/<place>?format=%l:+%C+%t+(feels+%f),+wind+%w&u'
- Papers: curl -s "http://export.arxiv.org/api/query?search_query=all:<q>&max_results=5"
- Move, snap or resize a window (does the math): {PLACE} "<process>" <left|right|full|center> <laptop|monitor|current|exact screen name from Context> ["part of title"]. Use it for EVERY window move. "Move to my laptop/monitor" = center. Several windows (side by side, a layout) = ONE call, moves joined with +: {PLACE} "Google Chrome" right current + "Terminal" left current; they all move at the same moment. Never sleep between steps: the helpers wait for windows themselves.
- Window names: osascript -e 'tell application "System Events" to get name of every window of process "Safari"'. Raise one: ... to perform action "AXRaise" of (first window whose name contains "x"), then activate the app.
- Type text into the box the user is in (search bar, chat box, doc): {TYPE} "<text>". It goes back to the user's app and pastes. Never use peekaboo for typing.
- Filling a box that is not already the focused one, or several boxes (a form, a title and a description, a chat in another pane or tab): click that exact box first (peekaboo click), then {TYPE}; never Tab between boxes on a web page. Copy text with pbpaste or from the page, never retype it from a picture. Before you say it is filled, pasted or sent, {LOOK} once and check it landed in the right place; fix it if not.
- "Tell me when / watch / monitor X" (a reply finishes, a build or download ends, a page shows something): look once, pick a short phrase on screen that shows the state, then ONE call: {WATCH} "<app>" "<window title words or empty>" <gone|shows> "<phrase>" ["<what to do then, as a request>"]. gone = done when the phrase disappears (e.g. "esc to interrupt" while a coding agent works); shows = done when it appears. It reads that window on this Mac every 5 s with no AI (it must stay on this desktop), notifies the user, then runs the request. Without the screen: {WATCH} cmd '<shell test, exit 0 = done>' ["<request>"]; a Codex task is done when: tail -n 2 "$(ls -t ~/.codex/sessions/*/*/*/*.jsonl | head -1)" | grep -q task_complete. Prefer cmd when it fits. Reply that you are watching. "Stop watching": {ME} --unwatch.
- Last resort, only when nothing above works (it moves the user's real mouse and pulls the app forward) — click or read buttons/menus inside an app: peekaboo see --app "<App>" --json --no-remote | head -c 3000 (always cut it; lists elements with IDs), then peekaboo click --on <ID> --no-remote, peekaboo type "text" --no-remote, peekaboo menu click --app "<App>" --path "File > New" --no-remote. Run peekaboo <cmd> --help if unsure.
- Spotify: play any song, album, artist or the user's own playlist: {PLAY} "<name, plus artist if known>" (no clicks, the user's focus stays). Music requests mean Spotify unless the user names another app. Pause/skip: osascript -e 'tell application "Spotify" to playpause' (or next track). Never click in Spotify unless those fail.
- Task list, to-do, "remind me": Reminders, all items in one call: osascript -e 'tell application "Reminders"' -e 'repeat with t in {{"A", "B"}}' -e 'make new reminder with properties {{name:t}}' -e 'end repeat' -e 'end tell' (for a time add due date:(current date) + <seconds>). "Note this": osascript -e 'tell application "Notes" to make new note with properties {{body:"<h1>Title</h1>A<br>B"}}'. "Where is my list/note" = {SHOW} "Reminders" (or "Notes").
- Clipboard: pbpaste. Music: osascript -e 'tell application "Music" to playpause'.
Earlier: the last few questions from the past 5 minutes, if any; use them for follow-ups ("open the second one", "and tomorrow?").
Memory: the user's saved rules come under "Memory:" in the request; follow them. When the user says remember / from now on, save ONE short line with {REMEMBER} "<rule>". When the user says forget, run {FORGET} "<words from that line>".
Shortcuts: "when I say X, open/do Y" or "make X a command" where Y is fixed actions (open apps, sites, files): run Y once, then save it with {SHORTCUT} "<X>" '<the one shell command>' (chain with ;). It then runs instantly with no AI; it is not a rule. A rule that is really a command: save the shortcut, then {FORGET} the rule.
Atticus's own switches (tips, read aloud, ask me when unsure…): {SET} "<setting name>" on|off, never click in Settings. Show Settings: open -a Atticus
Corrections: when the user says "no, I meant…", fix it, then save ONE short rule with {REMEMBER} so it won't happen again.
Rules: "this", "it", "here" mean the front app/window in Context. Process names can differ from app names (VS Code is "Code").
NEVER_RISKY
If a command fails, try one other way, then say what failed."""
NEVER = ('Full access: delete, send, post or buy when the user clearly asks; just run it, no asking in words. '
         'Files: trash "<path>" (to the Trash), never rm.')
# safe mode: the user gets an Allow card first, so the AI may do what was clearly asked
ASK_FIRST = ('Delete, send, post or buy only when the user clearly asks; the user sees an Allow / Don\'t Allow card first, so just run it, '
             'never ask in words. Files: trash "<path>" (to the Trash), never rm. If the user says no, stop and say it was not done.')

LEARN_ME = f"""
About the user: if they state a lasting fact about themselves that helps day to day (their name, city, school or job,
favorite apps, units), save it in 8 words or fewer with {ABOUT} "<fact>". Never one-off or private details (health, money, passwords)."""

TEACH = """
Tip: if you could not do it, or the user had to correct you or ask again a second way ("focus it", "no, …"), end your reply with
[tip <the exact words the user can say so it works first time next time, e.g. Say "remember: open means bring it to the front">]."""

# Settings → Ask me when unsure (on by default): a choices card in the box instead of a guess
ASK_ME = f"""
Ask, don't guess: unsure about anything that changes what you do (which file, app, song or person, which meaning, a missing detail)? Never choose
yourself (not even the newest) and never ask in words: run {PICK} "<question>" "<option>"... (2-8 short options; full paths for files), then act on the one it prints."""

POINTER = """
Show me where (on): when the user asks where something is or how to do something on screen, answer in one line, then end with
[point N X Y]: N = the screen number of the labeled picture, X Y = the pixel spot in that picture. A ring appears there."""

# Full access: everything runs without asking, except these, which a misheard word must never trigger.
DENY = [f'Bash({c}:*)' for c in ('rm', 'rmdir', 'sudo', 'dd', 'diskutil', 'shutdown', 'reboot', 'halt', 'git push', 'srm')]

# Safe mode (Settings, on by default): Claude Code's sandbox lets the AI's commands write only in a throwaway folder.
# App control can't work inside it (Apple Events, opening apps), so those run outside, and the gate below checks every command.
# A chain runs outside only if every part is listed, so harmless read-only tools are too ("…; curl wttr.in", "| head").
SAFE_OUT = [f'{c} *' for c in ('osascript', 'open', 'peekaboo', 'mdfind', 'curl', 'head', 'tail', 'grep', 'sort', 'wc', 'echo', 'sleep', 'date', ME)] + ['pbpaste', 'date']
# What the user must OK first. Matched per command, so `open "…?q=buy shoes"` or a search for "delete" never asks.
RISKY = [('Delete something?', r'\b(rm|rmdir|srm|unlink|shred|trash|mv)\s|\bdelete\b|empty (the )?trash'),
         ('Send this?', r'\bsend\b|\bsendmail\b|curl\b.*\s(-X\s*(POST|PUT|PATCH|DELETE)|--data\S*|-d|-F|--form)\s'),
         ('Buy this?', r'\b(buy|purchase|checkout|check out|place (an |the )?order|pay)\b'),
         ('Change a file?', r'(?<![0-9&])>>?\s*(?!/dev/null|&|/tmp/)\S|\btee\s|curl\b.*\s(-o|--output|-O)\b'),
         ('Run a system command?', r'do shell script|\bsudo\b|\bkillall\b|\bdefaults write\b')]
QUIET = (SEARCH, READ, LOOK, FRONT, SHOW, PLACE, PLAY, TYPE, REMEMBER, FORGET, ABOUT, PICK, SET, 'open ', 'mdfind ', 'pbpaste')   # never delete, send or buy


def risky(cmd):
    """-> the question to ask the user, or '' when the command can just run."""
    for part in re.split(r'[;&|\n]', cmd):
        part = part.strip()
        if part.startswith(QUIET) and '$(' not in part and '`' not in part:
            continue
        for q, rx in RISKY:
            if re.search(rx, part, re.I):
                return q
    return ''


def wait_commit():
    """Early reasoning is allowed; tools and the final answer wait for the confirmed transcript."""
    path = os.environ.get('ATTICUS_COMMIT')
    if not path:
        return True
    parent = os.getppid()
    for _ in range(900):   # 60 s recording + finalization + an in-flight instant action
        if os.getppid() != parent:
            break
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


def gate():
    """Claude Code's PreToolUse hook in safe mode: a risky command waits for the user's Allow (60 s, then no).
    Allowed = it runs for real, outside the sandbox. The AI asking to leave the sandbox by itself asks too."""
    inp = json.load(sys.stdin).get('tool_input', {})
    if not wait_commit():
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                          'permissionDecisionReason': 'The request was cancelled before the transcript was confirmed.'}}))
        return
    cmd = inp.get('command', '')
    q = risky(cmd) or ('Run outside the safe folder?' if inp.get('dangerouslyDisableSandbox') else '')
    if not q or not os.environ.get('ATTICUS_ASK') or not pref('safe', True):
        return
    ok = ask_user({'q': q, 'cmd': cmd[:800]}, 60) == 'y'
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow', 'updatedInput': {**inp, 'dangerouslyDisableSandbox': True}}} if ok else
                     {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                             'permissionDecisionReason': 'The user said no. Do not try another way; say it was not done.'}}))


def ask_user(j, secs):
    """Hands a question to relay() (the box, or a dialog) and waits for the answer. None: no answer in time, or cancelled."""
    d, parent = os.environ['ATTICUS_ASK'], os.getppid()
    a = os.path.join(d, 'a')
    with open(os.path.join(d, 'q.tmp'), 'w') as f:
        f.write(json.dumps(j))
    os.rename(os.path.join(d, 'q.tmp'), os.path.join(d, 'q'))
    for _ in range(secs * 10):
        if os.path.exists(a) or os.getppid() != parent:   # parent gone: the ask was cancelled
            break
        time.sleep(0.1)
    if not os.path.exists(a):
        return None
    s = open(a).read().strip()
    os.remove(a)
    return s


def pick(question, options):
    """{PICK}: the AI isn't sure which one the user means, so the user picks in the box. Prints the one picked."""
    if not options:
        return print('No options given.')
    s = ask_user({'pick': question, 'options': options[:8]}, 110) if os.environ.get('ATTICUS_ASK') else None   # 110 s: under the AI's 2-minute tool limit
    if s is None:
        return print('The user did not pick. Do not guess; say you need them to pick one.')
    print(options[int(s)] if s.isdigit() and int(s) < len(options) else 'None of these, the user said. Do not guess; say so in one line.')


def switches():
    """{key: label} for every on/off switch in Settings, read from the Settings page itself. Safe mode and pause stay manual."""
    page = open(os.path.join(os.path.dirname(ME), 'ui', 'settings.html')).read()
    return {k: html.unescape(b) for b, k in re.findall(r'<b[^>]*>([^<]+)</b>(?:<span[^>]*>.*?</span>)?</div><label class="sw"><input type="checkbox" data-k="(\w+)"', page)
            if k not in ('safe', 'paused')}


def set_switch(words, on):
    """{SET}: the switch whose name shares the most words with what the user said (first 3 letters, so "asking" finds "Ask")."""
    rows, stem = switches(), lambda t: {w[:3] for w in re.findall(r'[a-z]+', t.lower()) if w not in ('the', 'me', 'my', 'on', 'off', 'setting')}
    k = max(rows, key=lambda k: len(stem(words) & stem(rows[k])))
    if not stem(words) & stem(rows[k]) or on not in ('on', 'off'):
        return print('No such switch. Switches: ' + ', '.join(rows.values()))
    if os.environ.get('ATTICUS_ASK'):
        ask_user({'set': k, 'value': on == 'on'}, 5)
    print(f"{rows[k]} is now {on}.")


def thumb(path):
    """A Quick Look thumbnail (images, PDFs, documents, videos) as a data URL, or '' if macOS has none."""
    out = tempfile.mkdtemp()   # one folder each: files with the same name must not share a thumbnail
    try:
        subprocess.run(['qlmanage', '-t', '-s', '480', '-o', out, path], capture_output=True, timeout=4)
        png = os.path.join(out, os.path.basename(path) + '.png')
        return 'data:image/png;base64,' + base64.b64encode(open(png, 'rb').read()).decode() if os.path.exists(png) else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''
    finally:
        shutil.rmtree(out, True)


def choices(options):
    """What the box shows per option: a file gets its name, its folder and date, and a preview; anything else is just its text."""
    from concurrent.futures import ThreadPoolExecutor
    paths = [os.path.expanduser(o) for o in options]
    imgs = list(ThreadPoolExecutor(len(paths)).map(lambda p: thumb(p) if os.path.isfile(p) else '', paths))   # Quick Look hangs on folders
    return [{'label': os.path.basename(p.rstrip('/')) or p, 'sub': os.path.basename(os.path.dirname(p.rstrip('/'))) + time.strftime(' · %b %-d, %Y', time.localtime(os.path.getmtime(p))),
             'path': os.path.dirname(p.rstrip('/')).replace(HOME, '~', 1), 'img': img,
             'kind': 'Folder' if os.path.isdir(p) else os.path.splitext(p)[1][1:6].upper()}
            if os.path.exists(p) else {'label': o, 'sub': '', 'path': '', 'img': '', 'kind': ''} for o, p, img in zip(options, paths, imgs)]


def osa(script, *args):
    return subprocess.run(['osascript', '-e', script, *args], capture_output=True, text=True).stdout.strip()


def screens():
    """[(name, x, y, w, h, built_in)], top-left coordinates like System Events uses; the first is the main one.
    built_in comes from macOS itself, so laptop vs monitor works on any Mac, any number of displays."""
    out = subprocess.run(['osascript', '-l', 'JavaScript', '-e', 'ObjC.import("AppKit"); ObjC.bindFunction("CGDisplayIsBuiltin", ["bool", ["unsigned int"]]); '
        'var s=$.NSScreen.screens.js, H=s[0].frame.size.height; JSON.stringify(s.map(x=>[x.localizedName.js, x.frame.origin.x, '
        'H-x.frame.origin.y-x.frame.size.height, x.frame.size.width, x.frame.size.height, '
        '$.CGDisplayIsBuiltin(ObjC.unwrap(x.deviceDescription.objectForKey("NSScreenNumber")))]))'],
        capture_output=True, text=True).stdout
    return [(n, int(x), int(y), int(w), int(h), bool(b)) for n, x, y, w, h, b in json.loads(out)]


def describe(scr, fx=None):
    """The screens in words for the AI: kind, size, where each sits, which one the user is on."""
    mx, my = scr[0][1], scr[0][2]
    def spot(s):
        if s is scr[0]:
            return 'main'
        return ('left of main' if s[1] + s[3] <= mx else 'right of main' if s[1] >= mx + scr[0][3] else
                'above main' if s[2] + s[4] <= my else 'below main')
    on = next((s for s in scr if fx is not None and s[1] <= fx < s[1] + s[3]), None)
    return '; '.join(f'"{s[0]}" ({"built-in laptop screen" if s[5] else "external monitor"}, {s[3]}x{s[4]}, {spot(s)}, x={s[1]} y={s[2]}'
                     + (', the front window is here' if s is on else '') + ')' for s in scr)


def front():
    # Atticus.app passes the app the user was in when the user pressed the keys (its own windows can take focus, e.g. onboarding)
    app = os.environ.get('ATTICUS_FRONT') or osa('tell application "System Events" to get name of first process whose frontmost is true')
    return app, osa(f'tell application "System Events" to tell process "{app}" to get name of front window')


def restore_focus(app, task, cmds):
    if re.search(r'\b(switch|focus|go to|bring|full ?screen|close|quit|exit)\b', task.lower()) or any(
            FRONT in c or (SHOW in c and pref('openOn', 'here') == 'here') for c in cmds):
        return
    # Address a running process, never `activate` an app: that can relaunch it or reopen its closed window.
    osa('on run argv\ntell application "System Events"\n'
        'if exists process (item 1 of argv) then\nset frontmost of process (item 1 of argv) to true\n'
        'end if\nend tell\nend run', app)


def place(proc, where='full', screen='current', title=''):
    """Move a window with the math done here: where = left|right|full|center, screen = laptop|monitor|current."""
    if where not in ('left', 'right', 'full', 'center'):
        return 'where must be left, right, full or center'
    title = re.sub(r'\.[a-z0-9]{2,4}$', '', title, flags=re.I)   # "notes.pdf" must find a window titled "notes" (macOS hides extensions)
    # the real window: Chrome's window 1 can be a hidden popup bar
    win = f'(first window whose name contains "{title}")' if title else '(first window whose subrole is "AXStandardWindow")'
    aw = f'(first window whose name contains "{title}")' if title else 'window 1'   # never just the front window when a title was given
    tell = f'tell application "System Events" to tell process "{proc}" to '
    for tries in range(4):   # a window just opened (--show right before) can take a moment to appear
        # full screen (or split view) windows can't move; checked each try, since it only shows once the app is forward
        if osa(tell + f'get value of attribute "AXFullScreen" of {win}') == 'true':
            osa(tell + f'set value of attribute "AXFullScreen" of {win} to false')
            time.sleep(1.2)   # the exit animation
        try:
            px, py = map(int, osa(tell + f'get position of {win}').split(', '))
            ww, wh = map(int, osa(tell + f'get size of {win}').split(', '))
            app_mode = False
            break
        except ValueError:   # System Events can't see it (another Space): scriptable apps (Notes, Finder…) answer for their own windows, fast
            try:
                l, t, r, b = map(int, osa(f'tell application "{proc}" to get bounds of {aw}').split(', '))
                px, py, ww, wh, app_mode = l, t, r - l, b - t, True
                break
            except ValueError:   # not scriptable, or not open yet: bring it forward, look again
                if not tries:
                    osa(f'tell application "{proc}" to activate')
                if tries < 3:
                    time.sleep(0.4)
    else:
        return f'no window found for "{proc}" {title}'
    pick = {'laptop': lambda s: s[5], 'monitor': lambda s: not s[5]}.get(
        screen, (lambda s: s[0] == screen) if screen != 'current' else (lambda s: s[1] <= px + ww // 2 < s[1] + s[3]))
    scr = screens()
    n, x, y, w, h, _ = next((s for s in scr if pick(s)), scr[0])
    y, h = y + 25, h - 25   # menu bar
    cw, ch = min(ww, w), min(wh, h)
    bx, by, bw, bh = {'left': (x, y, w // 2, h), 'right': (x + w // 2, y, w - w // 2, h), 'full': (x, y, w, h),
                      'center': (x + (w - cw) // 2, y + (h - ch) // 2, cw, ch)}[where]
    # One step via the app when it is scriptable (Finder, Safari, Chrome…): after a window moves to the other
    # display, System Events often can't see it any more, so a later resize would miss.
    one = f'tell application "{proc}" to set bounds of {aw} to {{{bx}, {by}, {bx + bw}, {by + bh}}}'
    if (title and not app_mode) or subprocess.run(['osascript', '-e', one], capture_output=True).returncode:
        osa(tell + f'set size of {win} to {{{bw}, {bh}}}')   # size first, then move, then size again
        osa(tell + f'set position of {win} to {{{bx}, {by}}}')
        osa(tell + f'set size of {win} to {{{bw}, {bh}}}')
    return f'{proc} window -> {where} of {n}'


def show(target):
    """Open a URL, file or app where the user can see it, then hand focus back to the app the user was using."""
    mine, _ = front()
    t = os.path.expanduser(target)
    if pref('openOn', 'here') == 'here':   # Settings → Open apps on: This screen = in front, like a double-click
        r = subprocess.run(['open', t] if re.match(r'^[a-z]+://', t) or os.path.exists(t) else ['open', '-a', t])
        return f'no app or file called "{target}"' if r.returncode else f'opened {target} in front'
    if re.match(r'^[a-z]+://', t) or os.path.exists(t):
        subprocess.run(['open', '-g', t])
        js = ('ObjC.import("AppKit"); var t = %s; var u = /^[a-z]+:\\/\\//.test(t) ? $.NSURL.URLWithString(t) : $.NSURL.fileURLWithPath(t); '
              '$.NSWorkspace.sharedWorkspace.URLForApplicationToOpenURL(u).path.js') % json.dumps(t)
        app = os.path.basename(subprocess.run(['osascript', '-l', 'JavaScript', '-e', js], capture_output=True, text=True).stdout.strip())[:-4]
    else:
        if subprocess.run(['open', '-g', '-a', t]).returncode:
            return f'no app or file called "{target}"'
        app = t
    scr, other = screens(), None
    if len(scr) > 1 and app and app != mine:
        for _ in range(15):   # wait for its window, at most 1.5 s
            if osa(f'tell application "System Events" to count windows of process "{proc_of(app)}"') not in ('', '0'):
                break
            time.sleep(0.1)
        try:
            x = int(osa(f'tell application "System Events" to tell process "{mine}" to get position of window 1').split(', ')[0])
        except ValueError:
            x = 0
        other = next((s[0] for s in scr if not s[1] <= x + 50 < s[1] + s[3]), None)
        if other:
            place(app, 'center', other)
        osa(f'tell application "System Events" to set frontmost of process "{mine}" to true')
    return f'opened {target} in {app or "its app"}' + (f' on {other}; focus back on {mine}' if other else '')


NUMS = dict(zip('zero one two three four five six seven eight nine ten'.split(), map(str, range(11))))
PLAYLISTS = os.path.join(DIR, 'playlists.json')


def norm(name):
    """Spoken playlist name -> comparable key: "One M" and "1m" both give "1m"."""
    return re.sub(r'[^a-z0-9]', '', ''.join(NUMS.get(w, w) for w in name.lower().split()))


def playlist(name):
    """(uri, title) of a playlist in the user's Spotify app's cache whose title matches, else None."""
    names = json.load(open(PLAYLISTS)) if os.path.exists(PLAYLISTS) else {}
    hit = lambda: next(((f'spotify:playlist:{i}', t) for i, t in names.items() if t and norm(t) == norm(name)), None)
    if hit() or (os.path.exists(PLAYLISTS) and time.time() - os.path.getmtime(PLAYLISTS) < 3600):
        return hit()   # rescan at most hourly: mid-speech checks call this on every word
    ids = set()   # not known yet: read every playlist id from the cache, name the new ones (once each, saved)
    for root, _, files in os.walk(os.path.join(HOME, 'Library/Application Support/Spotify')):
        for f in files:
            try:
                ids |= set(re.findall(rb'spotify:playlist:([A-Za-z0-9]{22})', open(os.path.join(root, f), 'rb').read()))
            except OSError:
                pass
    new = [i.decode() for i in ids if i.decode() not in names and not i.startswith(b'37i9')]   # 37i9… = Spotify's own mixes
    def title(i):
        try:
            return i, json.loads(get(f'https://open.spotify.com/oembed?url=https://open.spotify.com/playlist/{i}'))['title']
        except Exception:
            return i, ''
    from concurrent.futures import ThreadPoolExecutor
    names.update(ThreadPoolExecutor(16).map(title, new))
    os.makedirs(DIR, exist_ok=True)
    json.dump(names, open(PLAYLISTS, 'w'))
    return hit()


def play(name, anything=True):
    """Play the user's playlist by that name; else (anything=True) the top Spotify song, album or artist for it from the web."""
    p = playlist(name)
    if not p and anything:
        try:
            for engine in ('https://html.duckduckgo.com/html/?q=', 'https://search.brave.com/search?q='):   # DDG misses some; Brave backs it up
                page = urllib.parse.unquote(get(engine + urllib.parse.quote(name + ' spotify')))
                m = re.search(r'open\.spotify\.com(?:/intl-\w+)?/(track|album|artist|playlist)/([A-Za-z0-9]{22})', page)
                if m:
                    break
            url = m and f'https://open.spotify.com/{m[1]}/{m[2]}'
            p = m and (f'spotify:{m[1]}:{m[2]}', json.loads(get('https://open.spotify.com/oembed?url=' + url))['title'])
        except Exception:
            p = None
    if not p:
        return None
    osa(f'tell application "Spotify" to play track "{p[0]}"')
    return f'Playing {p[1]} on Spotify.'


ALIASES = {'chrome': 'Google Chrome', 'vs code': 'Visual Studio Code', 'vscode': 'Visual Studio Code', 'code': 'Visual Studio Code',
           'settings': 'System Settings', 'terminal': 'Terminal', 'finder': 'Finder'}
LAST = os.path.join(DIR, 'last_app')


def app_named(words):
    """Spoken name -> installed app name, or None. Whole-name matches only, so "chrome history" is not Chrome."""
    w = re.sub(r'\s+(app|application)$', '', words.lower().strip(' .!?').removeprefix('the ').removeprefix('my '))
    if w in ALIASES:
        return ALIASES[w]
    key = lambda n: re.sub(r'[^a-z0-9]', '', n.lower())   # "trading view" = "TradingView"
    apps = [f[:-4] for d in ('/Applications', '/System/Applications', '/System/Applications/Utilities', HOME + '/Applications')
            if os.path.isdir(d) for f in os.listdir(d) if f.endswith('.app')]
    return next((a for a in apps if key(a) == key(w)), None) or next((a for a in apps if key(a.split()[-1]) == key(w)), None)


def proc_of(app):
    """App name -> its System Events process name (Visual Studio Code runs as "Code")."""
    bid = osa(f'id of application "{app}"')
    return osa(f'tell application "System Events" to get name of first process whose bundle identifier is "{bid}"') or app


def plain(text):
    """Spoken request -> its core words: "Hey Atticus, can you open Spotify please?" -> "open spotify"."""
    t = re.sub(r"^(hey |ok |okay )?(atticus,? )?(please |can you |could you |would you )?", '', text.lower().strip(' .!?'))
    t = re.sub(r'^(also |and |now |then )+', '', re.sub(r'\s+please$', '', t))
    return re.sub(r'[,;!?]|:(?!//)|(?<!\w)\.|\.(?!\w)', '', t)   # keep domain dots; speech also adds punctuation


# Learned shortcuts: an action the AI got right is saved as the exact commands it ran; the same words later replay
# them after confirmation with no AI (free). Kept in a file, never sent to the AI, so they cost nothing as they grow.
SKILLS = os.path.join(DIR, 'skills.json')
DEICTIC = re.compile(r'\b(this|that|these|those|it|here|there|them|him|her)\b')   # "close this" depends on the moment: never learned


# shortcut: a deny list, not proof of safety; shortcuts only ever come from the user's own ask
UNSAFE = re.compile(r'(^|[;&|(`\s])(rm|rmdir|sudo|dd|diskutil|shutdown|reboot|halt|srm|trash|mv|find|curl|wget)\s|git push|\$\(|`|>|\|\s*(ba|z)?sh\b|\bdelete\b')


def skills():
    try:
        return json.load(open(SKILLS))
    except (OSError, ValueError):
        return {}


def learn(task, cmds, reply):
    """Save the commands if every one is a replayable action (no reading, searching, clicking by element ID)."""
    k = plain(task)
    if any(x in c for c in cmds for x in (FORGET, SHORTCUT)):   # changing rules or shortcuts is not itself a shortcut
        return
    cmds = [c for c in cmds if not c.startswith((REMEMBER, ABOUT))]   # saving a rule or a fact is not part of the action
    act = (SHOW, PLACE, PLAY, TYPE, 'open ', 'osascript ')
    # 'make new': a reminder or note holds this moment's words ("make the task list" must not re-add old items)
    if not cmds or DEICTIC.search(k) or not all(c.startswith(act) and 'peekaboo' not in c and 'make new' not in c and not risky(c) for c in cmds):
        return
    sk = skills()
    sk[k] = {'cmds': cmds, 'reply': reply, 'at': time.strftime('%Y-%m-%d')}
    os.makedirs(DIR, exist_ok=True)
    json.dump(sk, open(SKILLS, 'w'), indent=1)


def unlearn(task):
    sk = skills()
    if sk.pop(plain(task), None) is not None:
        json.dump(sk, open(SKILLS, 'w'), indent=1)


def replay(k):
    s = skills().get(k)
    if not s:
        return None
    if pref('safe', True) and any(risky(c) for c in s['cmds']):
        return None   # the AI's tool gate can show the Allow card; direct replay cannot
    for c in s['cmds']:   # any failure: hand it to the AI instead
        try:
            if subprocess.run(['/bin/zsh', '-c', c], capture_output=True, timeout=20).returncode:
                return None
        except subprocess.TimeoutExpired:
            return None
    return s['reply']


def fast(text):
    """Simple commands with no AI: learned shortcuts, open <app/site>, snap/move <it/app> left/right/full/center, to laptop/monitor.
    Returns a reply, or None to hand the request to Haiku."""
    t = plain(text)
    if t in ('no', 'yes', 'ok', 'okay', 'nevermind', 'never mind', 'cancel', 'nothing', 'thanks', 'thank you'):
        return 'Okay.'   # stray word after a turn: no AI call
    if pref('learn', True) and t in skills():
        if os.environ.get('ATTICUS_LIVE') == '1':
            return None   # even a shortcut named "open Spotify" waits for confirmed words
        r = replay(t)
        if r:
            return r
    m = re.fullmatch(r'(?:search(?: up)?|google|look up)(?: for)? (.+?)(?: (?:on|in) (google|chrome|safari|youtube|spotify|amazon|the web))?', t)
    if m and not re.search(r'\b(this|that|it|these|my)\b', m[1]):   # "search this" needs the screen: AI
        q = urllib.parse.quote(m[1])
        subprocess.run(['open', {'youtube': f'https://www.youtube.com/results?search_query={q}', 'spotify': f'spotify:search:{q}',
                                 'amazon': f'https://www.amazon.com/s?k={q}'}.get(m[2], f'https://www.google.com/search?q={q}')])
        return f'Searched {m[1]}' + (f' on {m[2].title()}.' if m[2] in ('youtube', 'spotify', 'amazon') else '.')
    m = re.fullmatch(r'play (?:my |the )?(?:spotify )?(?:playlist )?(?:called |named )?(.+?)( playlist)?( on spotify)?', t)
    if m and not DEICTIC.search(m[1]):   # "play X" alone may mean a video: only the user's playlist here, anything else goes to the AI (which uses --play)
        return play(m[1], anything=bool(m[3]))
    m = re.fullmatch(r'(?:open|launch|start|pull up|bring up|focus(?: on)?|switch to|go to)\s+(.+?)'
                     r'(?:\s+(?:on|to|in|at)\s+(?:the\s+|my\s+)?(left|right|full ?screen|center|middle|laptop|monitor)(?:\s+(?:side|half|screen))?)?', t)
    if m and m[2]:   # "open Chrome on my monitor": open it, then the move command below
        if not fast(f'open {m[1]}'):
            return None
        for _ in range(20):   # a just-launched app needs a moment for its window
            r = fast(f'move {m[1]} to {m[2]}')
            if r:
                return r
            time.sleep(0.1)
        return None
    if m:
        target = m[1]
        site = target.replace(' dot ', '.')
        site = next((w.rstrip('.,!') for w in text.split() if plain(w) == target), site)
        if re.fullmatch(r'(?:https?://)?[\w-]+(\.[\w-]+)+(/\S*)?', site) and not re.search(r'\.(pdf|docx?|xlsx?|pptx?|txt|csv|png|jpe?g|heic|key|pages|numbers|zip|mp[34]|mov|md|py|java)$', target):
            subprocess.run(['open', site if site.startswith(('https://', 'http://')) else 'https://' + site])
            return f'Opened {target}.'
        app = app_named(target)
        if not app:
            return None
        if pref('openOn', 'here') == 'other':
            show(app)   # Settings: open on the other screen and keep focus (slower, ~1-3 s)
        else:
            subprocess.run(['open', '-a', app])   # plain open, ~0.1 s: here and in front
        open(LAST, 'w').write(app)
        return f'Opened {app}.'
    m = re.fullmatch(r'(?:snap|move|put|throw|send|make)\s+(.+?)\s+(?:to\s+|on\s+|onto\s+)?(?:the\s+|my\s+)?'
                     r'(left|right|full ?screen|center|middle|laptop|monitor)(?:\s+(?:side|half|screen))?(?:\s+of\s+(?:the|my)\s+screen)?'
                     r'(?:\s+(?:on|of)\s+(?:the\s+|my\s+)?(laptop|monitor))?', t)
    if not m:
        return None
    who, where, screen = m[1], m[2].replace(' ', ''), m[3] or 'current'
    if who in ('it', 'that', 'this', 'that window', 'this window', 'the window'):
        app = open(LAST).read().strip() if os.path.exists(LAST) and time.time() - os.path.getmtime(LAST) < 600 else front()[0]
    else:
        app = app_named(who)
        if not app:
            return None
    if where in ('laptop', 'monitor'):
        where, screen = 'center', where
    where = {'fullscreen': 'full', 'middle': 'center'}.get(where, where)
    out = place(proc_of(app), where, screen)
    if out.startswith('no window'):
        return None
    open(LAST, 'w').write(app)
    return f'{app}: {where}' + (f' on {screen}' if screen != 'current' else '') + '.'


def look():
    """Every display, at once, 1280 px wide (≈1.5K tokens each) -> [(path, label)]. "My monitor" needs the right one."""
    scr = screens()
    def one(i):
        fd, path = tempfile.mkstemp(prefix=f'atticus-screen-{i}-', suffix='.jpg')
        os.close(fd)
        os.remove(path)
        atexit.register(lambda: os.path.exists(path) and os.remove(path))
        # Atticus.app takes it, leaving its own box out; plain screencapture is the fallback (box included)
        if subprocess.run([APP, '--look', str(i), path], capture_output=True).returncode:
            subprocess.run(['screencapture', '-x', '-D', str(i), '-t', 'jpg', path])
            subprocess.run(['sips', '-Z', '1280', '-s', 'formatOptions', '70', path], capture_output=True)
        s = scr[i - 1]
        return path, f'Screen {i}: "{s[0]}" ({"built-in laptop screen" if s[5] else "external monitor"})'
    from concurrent.futures import ThreadPoolExecutor
    return [r for r in ThreadPoolExecutor(len(scr) or 1).map(one, range(1, len(scr) + 1)) if os.path.exists(r[0])]


def get(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh) AppleWebKit/605 Safari/605'})
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', 'replace')


def text_of(h):
    h = re.sub(r'(?is)<(script|style|noscript|svg|head|nav|header|footer|aside)\b.*?</\1>', ' ', h)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', h)).split())


def search(q):
    """Free web search: DuckDuckGo's HTML page -> 5 results, ~150 tokens each."""
    page = get('https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(q))
    out = []
    for u, t, sn in re.findall(r'class="result__a" href="([^"]+)">(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', page, re.S)[:5]:
        u = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(u)).query).get('uddg', [u])[0]
        out.append([text_of(t), u, text_of(sn)])
    def page(r):   # the top pages' text too, fetched at once: one search usually answers it, no extra read rounds
        try:
            return text_of(get(r[1], timeout=1.5))[:1200]
        except Exception:
            return ''
    from concurrent.futures import ThreadPoolExecutor
    for r, txt in zip(out, ThreadPoolExecutor(3).map(page, out[:3])):
        r.append(txt and 'Page: ' + txt)
    return '\n\n'.join('\n'.join(filter(None, r)) for r in out) or 'no results (DuckDuckGo may be blocking; use WebSearch)'


def label(name, inp):
    """Turn a tool call into a short line for the overlay."""
    if name == 'WebSearch':
        return 'Searching the web: ' + inp.get('query', '')
    if name == 'WebFetch':
        return 'Reading ' + re.sub(r'^https?://(www\.)?([^/]+).*', r'\2', inp.get('url', ''))
    if name == 'Read':
        return 'Reading your screen' if inp.get('file_path', '').startswith(SHOT) else 'Reading ' + os.path.basename(inp.get('file_path', ''))
    c = re.sub(r'^cd \S+\s*(;|&&)\s*', '', ' '.join(inp.get('command', '').split()))   # "cd /tmp; <helper>" reads as the helper
    if c.startswith(SEARCH):
        return 'Searching the web: ' + c[len(SEARCH):].strip(' "\'')
    if c.startswith(READ):
        return 'Reading ' + re.sub(r'^https?://(www\.)?([^/]+).*', r'\2', c[len(READ):].strip(' "\''))
    if c.startswith(SHOW):
        t = c[len(SHOW):].strip(' "\'')
        return 'Opening ' + (re.sub(r'^https?://(www\.)?([^/]+).*', r'\2', t) if '://' in t else os.path.basename(t))
    if c.startswith(ABOUT):
        return 'Noting that about you'
    if c.startswith(REMEMBER):
        return 'Remembering that'
    if c.startswith(SHORTCUT):
        return 'Saving your shortcut'
    if c.startswith(FORGET):
        return 'Forgetting that'
    if c.startswith(PICK):
        return 'Asking you which one'
    if c.startswith(SET):
        return 'Changing a setting'
    if c.startswith(PLAY):
        return 'Playing ' + c[len(PLAY):].strip(' "\'') + ' on Spotify'
    if c.startswith(LOOK):
        return 'Looking at your screen'
    if c.startswith(FRONT):
        return 'Bringing ' + (re.findall(r'"([^"]+)"', c) or ['the app'])[0] + ' forward'
    if c.startswith(PLACE):
        many = re.findall(r'"([^"]+)"\s+(?:left|right|full|center)', c)
        if len(many) > 1:
            return 'Moving ' + ', '.join(many[:-1]) + ' and ' + many[-1]
        a = c[len(PLACE):].replace('"', '').split()
        return f'Moving {a[0]} → {" ".join(a[1:3])}' if a else 'Moving a window'
    m = re.match(r'^peekaboo (\w+)', c)
    if m:
        verb, a = m[1], re.search(r'--app (?:"([^"]+)"|(\S+))', c)
        app = a and (a[1] or a[2]) or 'the app'
        return {'see': f'Looking at {app}', 'click': 'Clicking', 'type': 'Typing', 'menu': f'Using the {app} menu',
                'hotkey': 'Pressing keys', 'scroll': 'Scrolling'}.get(verb, f'Working in {app}')
    m = re.match(r'^open -a (?:"([^"]+)"|(\S+))', c)
    if m:
        return 'Opening ' + (m[1] or m[2]).rstrip(';')
    for pat, fmt in ((r'^open "?https?://(?:www\.)?([^/"]+)', 'Opening {}'),
                     (r'^mdfind', 'Searching your files'), (r'arxiv', 'Searching papers'), (r'^pbpaste', 'Reading the clipboard'),
                     (r'tell application "(?!System Events)([^"]+)"', 'Using {}'), (r'process "([^"]+)"', 'Checking {} windows')):
        m = re.search(pat, c)
        if m:
            return fmt.format(*m.groups())
    if c.startswith('open '):
        return 'Opening ' + os.path.basename(c[5:].strip('"\' '))
    return 'Working on it'   # never show the raw command


WATCHING = os.path.join(DIR, 'watching')
BUDGET = {'usd': 0.002, 'secs': 6}   # median of the last 50 AI asks must stay under this (Oct 10, 2026: $0.0009, 4.7 s)


def notify(text):
    osa('on run argv\ndisplay notification (item 1 of argv) with title "Atticus" sound name "Glass"\nend run', text)


def watch(what, then, app='', words='', mode='', phrase=''):
    """Free while it waits: every 5 s, a shell test (`what`, exit 0 = done) or one window's text read on this Mac. No AI;
    the AI runs again only for `then`."""
    norm = lambda s: ' '.join(s.lower().split())
    phrase, hits, misses, end = norm(phrase), 0, 0, time.time() + 3 * 3600
    while time.time() < end:
        if app:
            r = subprocess.run([APP, '--text', app, words], capture_output=True, text=True)
            misses = misses + 1 if r.returncode else 0
            if misses >= 3:
                return notify(f"Stopped watching {app}: can't see its window. It must stay open on this desktop (covered is fine, minimized is not).")
            done = not r.returncode and (phrase in norm(r.stdout)) == (mode == 'shows')
        else:
            done = subprocess.run(['/bin/sh', '-c', what], capture_output=True).returncode == 0
        hits = hits + 1 if done else 0
        if hits >= 2:   # twice in a row: one bad read or a flicker between steps must not fire it
            notify(f'{app or what[:40]}: done.' + (f' Now: {then}' if then else ''))
            return then and subprocess.run([sys.executable, ME, then])
        time.sleep(5)
    notify('Stopped watching after 3 hours.')


def licensed():
    return not GUMROAD_PRODUCT or os.path.exists(KEY_FILE)


def activate(key):
    req = urllib.request.Request('https://api.gumroad.com/v2/licenses/verify',
                                 urllib.parse.urlencode({'product_id': GUMROAD_PRODUCT, 'license_key': key}).encode())
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:   # Gumroad answers a bad key with a 404
        r = json.loads(e.read() or b'{}')
    except OSError:
        return 'Could not reach Gumroad. Check the internet and try again.'
    p = r.get('purchase') or {}
    if not r.get('success') or p.get('refunded') or p.get('chargebacked'):
        return 'That key did not work. Copy it from your Gumroad receipt and type: key <your key>'
    os.makedirs(DIR, exist_ok=True)
    open(KEY_FILE, 'w').write(key)
    return 'Thanks for buying Atticus! It is unlocked.'


def ping():
    day, stamp = time.strftime('%Y-%m-%d'), os.path.join(DIR, 'pinged')
    if not POSTHOG_KEY or not pref('stats', True) or (os.path.exists(stamp) and open(stamp).read() == day):
        return
    idf = os.path.join(DIR, 'id')
    if not os.path.exists(idf):
        os.makedirs(DIR, exist_ok=True)
        open(idf, 'w').write(base64.b32encode(os.urandom(10)).decode())
    open(stamp, 'w').write(day)
    body = json.dumps({'api_key': POSTHOG_KEY, 'event': 'used', 'distinct_id': open(idf).read(), 'properties': {'$process_person_profile': False}})
    subprocess.Popen(['curl', '-s', '-m', '10', '-H', 'Content-Type: application/json', '-d', body, 'https://us.i.posthog.com/i/v0/e/'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # its own process: never slows the answer


def say(tag, text):
    print(f'{tag} {" ".join(text.split())}', flush=True)


def log_turn(task, reply, started, cost=0, steps=0, ran=()):
    with open(os.path.join(DIR, 'log.jsonl'), 'a') as f:
        f.write(json.dumps({'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'task': task, 'reply': reply,
                            'usd': round(cost, 5), 'secs': round(time.time() - started, 1), 'steps': steps,
                            'ran': list(ran), 'live': os.environ.get('ATTICUS_LIVE') == '1'}) + '\n')


TIPS = ['Say "remember …" and Atticus keeps it as a rule.',
        'Say "when I say study mode, open …" to make your own instant command.',
        'Ask "where do I click to …" and a ring marks the spot.',
        'Tap the shortcut to type instead of talk.']
MISS = 'Saying the same thing more than once? Tell Atticus how you want it done. It remembers next time.'


def tip(reply, taught=''):
    """One small tip under the answer ("i <tip>"). After a miss or a redo: how to teach it (the AI's own words if it gave some),
    every time; otherwise a general tip at most every 6 h. Off in Settings."""
    if not pref('tips', True) or os.environ.get('ATTICUS_UI') != '1':
        return
    if taught or re.search(r"\b(can't|cannot|couldn't|could not|unable|failed|didn't work|not able)\b", reply.lower()):
        return say('i', taught or MISS)
    stamp = os.path.join(DIR, 'tip')
    if not os.path.exists(stamp):   # never on the very first ask
        return open(stamp, 'w').write('0')
    n = int(open(stamp).read() or 0)
    if time.time() - os.path.getmtime(stamp) < 6 * 3600:
        return
    open(stamp, 'w').write(str(n + 1))
    say('i', TIPS[n % len(TIPS)])


def plan():
    """Can Atticus think? Claude Code must be installed and signed in to a paid Claude plan (or an API key).
    -> {'ok': bool, 'need': '' | 'install' | 'login' | 'upgrade', 'text': one line for the user}"""
    try:
        s = json.loads(subprocess.run(['claude', 'auth', 'status'], capture_output=True, text=True, timeout=20).stdout)
    except FileNotFoundError:
        return {'ok': False, 'need': 'install', 'text': 'Claude Code is not installed.'}
    except (ValueError, subprocess.TimeoutExpired):
        return {'ok': False, 'need': 'login', 'text': 'Could not read your Claude sign-in.'}
    if not s.get('loggedIn'):
        return {'ok': False, 'need': 'login', 'text': 'Sign in to Claude to start.'}
    if s.get('authMethod') != 'claude.ai':
        return {'ok': True, 'need': '', 'text': 'Using your Anthropic API key (billed per use).'}
    sub = (s.get('subscriptionType') or '').lower()
    if sub in ('', 'free'):
        return {'ok': False, 'need': 'upgrade', 'text': 'Atticus needs a paid Claude plan (Pro or Max).'}
    return {'ok': True, 'need': '', 'text': f'Using your Claude {sub.title()} plan.'}


def stop_claude(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)   # includes running tools and permission hooks
    except OSError:   # already gone, or its group is out of reach (EPERM): nothing left to stop
        pass


def stop_workers(*_):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpid(), signal.SIGTERM)
    sys.exit(0)


def start_claude():
    """`claude -p` started ahead, waiting for the request on stdin: its ~1 s startup happens while the user is still talking."""
    if not shutil.which('claude'):
        return None
    system = SYSTEM.replace('NEVER_RISKY', ASK_FIRST if pref('safe', True) else NEVER) + (ASK_ME if pref('askMe', True) else '') + (POINTER if pref('pointer', False) else '') + (LEARN_ME if pref('learnMe', True) else '') + (TEACH if pref('tips', True) else '')
    settings = {}
    ask = tempfile.mkdtemp(prefix='atticus-ask-')   # the Allow card (safe mode) and the choices card talk through it
    atexit.register(shutil.rmtree, ask, True)   # however this run ends
    if pref('safe', True):
        settings['sandbox'] = {'enabled': True, 'autoAllowBashIfSandboxed': True, 'allowUnsandboxedCommands': True, 'excludedCommands': SAFE_OUT}
    if pref('safe', True) or os.environ.get('ATTICUS_COMMIT'):
        settings['hooks'] = {'PreToolUse': [{'matcher': '.*', 'hooks': [{'type': 'command', 'command': f'"{sys.executable}" "{ME}" --gate', 'timeout': 140}]}]}
    errors = tempfile.TemporaryFile(mode='w+')   # draining a pipe only after wait() can deadlock
    p = subprocess.Popen(['claude', '-p', '--input-format', 'stream-json', '--model', pref('model', 'haiku'), '--effort', 'low', '--system-prompt', system,
                          '--tools', 'Bash,Read,WebSearch,WebFetch', '--permission-mode', 'bypassPermissions', '--disallowedTools', *DENY, '--settings', json.dumps(settings),
                          '--setting-sources', '', '--no-session-persistence', '--strict-mcp-config', '--disable-slash-commands', '--output-format', 'stream-json', '--verbose'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, cwd=tempfile.gettempdir(), start_new_session=True,
                         env={**os.environ, 'ATTICUS_ASK': ask})
    p.ask, p.errors = ask, errors
    atexit.register(errors.close)
    atexit.register(stop_claude, p)
    signal.signal(signal.SIGTERM, lambda *_: (stop_claude(p), stop_workers()))
    return p


def relay(p):
    """Passes the gate's question to the box ("? <question>\t<command>\t<answer file>"), or a dialog when there is no box."""
    q = os.path.join(p.ask, 'q')
    while p.poll() is None:
        if os.path.exists(q):
            j = json.load(open(q))
            os.remove(q)
            a = os.path.join(p.ask, 'a')
            if 'set' in j:   # "⚙ <key> <true|false>": the app flips it, so the Settings window and the app agree
                if os.environ.get('ATTICUS_UI') == '1':
                    print(f"⚙ {j['set']} {'true' if j['value'] else 'false'}", flush=True)
                else:   # no app running: it reads the file when it starts
                    json.dump({**(json.load(open(PREFS)) if os.path.exists(PREFS) else {}), j['set']: j['value']}, open(PREFS, 'w'), indent=1)
                open(a + '.tmp', 'w').write('ok')
                os.rename(a + '.tmp', a)
            elif 'pick' in j and os.environ.get('ATTICUS_UI') == '1':   # "◇ {json}": the choices card
                print('◇ ' + json.dumps({'q': j['pick'], 'options': choices(j['options']), 'a': a}), flush=True)
            elif 'pick' in j:
                r = osa('on run argv\nset r to choose from list (rest of argv) with prompt (item 1 of argv)\nif r is false then return ""\nreturn item 1 of r\nend run', j['pick'], *j['options'])
                open(a + '.tmp', 'w').write(str(j['options'].index(r)) if r in j['options'] else '')
                os.rename(a + '.tmp', a)   # whole, or the waiting side reads it half-written
            elif os.environ.get('ATTICUS_UI') == '1':
                print(f"? {j['q']}\t{' '.join(j['cmd'].split())}\t{a}", flush=True)   # not say(): it would squash the tabs
            else:
                yes = osa('on run argv\ndisplay dialog (item 2 of argv) with title (item 1 of argv) buttons {"Don\'t Allow", "Allow"}\n'
                          'return button returned of result\nend run', j['q'], j['cmd']) == 'Allow'
                open(a, 'w').write('y' if yes else 'n')
        time.sleep(0.1)


def run(task, p=None, shots=()):
    """One request through `claude -p --model haiku` (the user's plan), streaming notes and steps. shots: [(path, label)] sent with it."""
    run.cmds = run.ran = []
    run.ok = False
    p = p or start_claude()
    if not p:
        say('!', 'setup')   # Atticus.app opens its setup window
        return plan()['text'] + ' Atticus setup is open to fix it.', 0, []
    content = [b for path, label in shots if os.path.exists(path) for b in ({'type': 'text', 'text': label}, {'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/jpeg', 'data': base64.b64encode(open(path, 'rb').read()).decode()}})] + [{'type': 'text', 'text': task}]
    p.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}) + '\n')
    p.stdin.close()
    if getattr(p, 'ask', ''):
        threading.Thread(target=relay, args=(p,), daemon=True).start()
    reply, cost, steps, cmds, failed, completed, result_error = '', 0, [], [], False, False, False
    for line in p.stdout:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get('type') == 'assistant':
            blocks = j['message'].get('content', [])
            if any(b.get('type') == 'tool_use' for b in blocks):   # notes before a tool call; the final text comes as result
                for b in blocks:
                    if b.get('type') == 'text' and b['text'].strip():
                        say('·', b['text'])
                    elif b.get('type') == 'tool_use':
                        steps.append(label(b['name'], b.get('input', {})))
                        say('▸', steps[-1])
                        cmds.append(b.get('input', {}).get('command') or f"<{b['name']}>")   # non-Bash tools block learning
        elif j.get('type') == 'user':   # tool results
            failed |= any(isinstance(b, dict) and b.get('is_error') for b in j.get('message', {}).get('content', []))
        elif j.get('type') == 'result':
            completed = True
            result_error = bool(j.get('is_error'))
            reply, cost = (j.get('result') or '').strip(), j.get('total_cost_usd', 0)
    p.wait()
    run.ran = cmds   # the exact commands, for the ask log
    if not reply and p.returncode:
        c = plan()   # the usual cause: not installed, signed out, or no paid plan
        if not c['ok']:
            say('!', 'setup')
        p.errors.seek(0)
        reply = c['text'] + ' Atticus setup is open to fix it.' if not c['ok'] else f'Claude failed: {p.errors.read(200)}'
    run.cmds = [] if failed or result_error or p.returncode or not completed or not reply else cmds
    run.ok = bool(completed and reply and not result_error and not p.returncode)
    return reply or 'No answer came back. Please try again.', cost, steps


def screen_for(task, snap, fx=None):   # snap.shots: what look() returned; fx: x of the front window
    """The key-down screenshot, if the request points at the screen ("what does this say", "articles on what I'm reading")."""
    if pref('screen', 'auto') == 'never' or not re.search(r"\b(screen|this|these|that|here|it|reading|looking|see|says?|shown?|page|tab|window|error|question|problem|where|how do i|button|click)\b", task.lower()):
        return None
    snap.join(5)
    if fx is None or re.search(r"\b(monitor|laptop|other|second|both|all|external|built.in|screens)\b", task.lower()):
        return snap.shots
    on = next((i for i, s in enumerate(screens()) if s[1] <= fx < s[1] + s[3]), 0)   # just the one the user is on: half the cost
    return [shot for shot in snap.shots if shot[1].startswith(f'Screen {on + 1}:')]


STOP = set('a an the to of in on and or for my me i you it is be do can could would please with at this that what how'.split())


def relevant_rules(task, most=5):
    """Saved rules that share a word with the request. All of them while there are few; never more than `most`."""
    rules = [l.strip() for l in open(MEMORY)] if os.path.exists(MEMORY) else []
    rules = [r for r in rules if r]
    if sum(map(len, rules)) < 400:
        return '\n'.join(rules)
    words = set(re.findall(r'[a-z0-9]+', task.lower())) - STOP
    hits = [r for r in rules if words & set(re.findall(r'[a-z0-9]+', r.lower()))]
    return '\n'.join(hits[:most])


def point(reply):
    """Experimental "show me where": the AI ends with [point N X Y] (screen N's picture, 1280 px wide).
    The overlay gets "@ <x> <y>" in screen points; the user sees the reply without the tag."""
    m = re.search(r'\[point (\d+) (\d+) (\d+)\]', reply)
    if not m:
        return reply
    scr = screens()
    n = min(max(int(m[1]), 1), len(scr)) - 1
    k = scr[n][3] / 1280
    say('@', f'{round(scr[n][1] + int(m[2]) * k)} {round(scr[n][2] + int(m[3]) * k)}')
    return (reply[:m.start()] + reply[m.end():]).strip()


def read_rows(path):
    try:
        with open(path) as f:
            rows = []
            for line in f:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except ValueError:
                    continue   # a cancelled write must not hide the rest of the history
            return rows
    except OSError:
        return []


def recent():
    return [r for r in read_rows(RECENT) if isinstance(r.get('t'), (int, float)) and
            isinstance(r.get('q'), str) and isinstance(r.get('a'), str)]


def remember_turn(q, a):
    turns = recent()
    turns = [r for r in turns if time.time() - r['t'] < 300][-2:] + [{'t': time.time(), 'q': q, 'a': a}]
    with open(RECENT, 'w') as f:
        f.writelines(json.dumps(r) + '\n' for r in turns)


def ask():
    out = osa('tell application "System Events"\nactivate\nset r to display dialog "What should I do?" default answer "" '
              'buttons {"Cancel", "Go"} default button "Go" with title "Atticus"\nreturn text returned of r\nend tell')
    return out.strip()


def main():
    args = sys.argv[1:]
    if args == ['--wait'] or args[:1] == ['--fast']:
        os.setpgrp()   # cancellation also stops the worker's own helper processes
        signal.signal(signal.SIGTERM, stop_workers)
    if args[:1] == ['--watch'] and (len(args) in (5, 6) and args[3] in ('gone', 'shows') or len(args) in (3, 4) and args[1] == 'cmd'):
        os.makedirs(WATCHING, exist_ok=True)   # the watcher runs on after this ask ends
        w = subprocess.Popen([sys.executable, ME, '--watching', *args[1:]], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        open(os.path.join(WATCHING, str(w.pid)), 'w').write(' '.join(args[1:]))
        return print(f'watching {args[1]} (free until it fires)')
    if args[:1] == ['--watching']:
        try:
            if args[1] == 'cmd':
                return watch(args[2], args[3] if len(args) > 3 else '')
            return watch('', args[5] if len(args) > 5 else '', *args[1:5])
        finally:
            f = os.path.join(WATCHING, str(os.getpid()))
            os.path.exists(f) and os.remove(f)
    if args == ['--unwatch']:
        pids = os.listdir(WATCHING) if os.path.isdir(WATCHING) else []
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass
            os.remove(os.path.join(WATCHING, pid))
        return print(f'stopped {len(pids)} watch(es)')
    if args == ['--budget']:
        ai = [r for r in read_rows(os.path.join(DIR, 'log.jsonl')) if r.get('usd', 0) > 0][-50:]
        if not ai:
            return print('no AI asks yet')
        med = lambda k: sorted(r.get(k, 0) for r in ai)[len(ai) // 2]
        ok = med('usd') <= BUDGET['usd'] and med('secs') <= BUDGET['secs']
        print(f"{'OK' if ok else 'OVER BUDGET'}: last {len(ai)} AI asks, median ${med('usd')} (max ${BUDGET['usd']}), {med('secs')} s (max {BUDGET['secs']} s)")
        return sys.exit(0 if ok else 1)
    if args == ['--gate']:
        return gate()
    if args[:1] == ['--set'] and len(args) == 3:
        return set_switch(args[1], args[2].lower())
    if args[:1] == ['--pick'] and len(args) > 1:
        return pick(args[1], args[2:])
    if args[:1] == ['--place']:   # several moves joined with +: all at once, not one window after another
        moves = [m.split('\0') for m in '\0'.join(args[1:]).split('\0+\0') if m and m != '+']
        from concurrent.futures import ThreadPoolExecutor
        return print('\n'.join(ThreadPoolExecutor(len(moves) or 1).map(lambda m: place(*m), moves)))
    if args == ['--look']:
        return print('\n'.join(f'{label}: {path}' for path, label in look()) or 'no screenshot: allow Atticus in Screen Recording (System Settings)')
    if args[:1] == ['--front'] and len(args) > 1:   # "show me where" on an app or tab the user isn't looking at
        app, words = args[1], ' '.join(args[2:])
        osa(f'tell application "{app}" to activate')
        if words:   # any app: the window whose title has the words; browsers: also that tab (fails quietly elsewhere). `contains` ignores case.
            osa(f'on run argv\ntell application "System Events" to tell (first process whose name is "{proc_of(app)}")\n'
                'perform action "AXRaise" of (first window whose name contains item 1 of argv)\nend tell\nend run', words)
            set_tab = 'set current tab of w to t' if app == 'Safari' else 'set active tab index of w to i'
            name = 'name' if app == 'Safari' else 'title'
            osa(f'on run argv\ntell application "{app}"\nrepeat with w in windows\nset i to 0\nrepeat with t in tabs of w\nset i to i + 1\n'
                f'if ({name} of t contains item 1 of argv) or (URL of t contains item 1 of argv) then\n{set_tab}\nset index of w to 1\nreturn\n'
                'end if\nend repeat\nend repeat\nend tell\nend run', words)
        time.sleep(0.5)   # the window draws before the picture
        return print('\n'.join(f'{label}: {path}' for path, label in look()) or 'no screenshot: allow Atticus in Screen Recording (System Settings)')
    if args[:1] == ['--type']:   # paste into the focused field of the user's app, then put the user's clipboard back
        app, text = front()[0], ' '.join(args[1:])
        old = subprocess.run(['pbpaste'], capture_output=True).stdout
        subprocess.run(['pbcopy'], input=text.encode())
        r = subprocess.run(['osascript', '-e', f'tell application "{app}" to activate', '-e', 'delay 0.25',
                            '-e', 'tell application "System Events" to keystroke "v" using command down'], capture_output=True, text=True)
        time.sleep(0.4)
        subprocess.run(['pbcopy'], input=old)
        return print(f'typed into {app}' if not r.returncode else f'could not type: {r.stderr.strip()} (allow Atticus in Accessibility)')
    if args[:1] == ['--show']:
        return print(show(' '.join(args[1:])))
    if args[:1] == ['--remember']:
        os.makedirs(DIR, exist_ok=True)
        with open(MEMORY, 'a') as f:
            f.write('- ' + ' '.join(' '.join(args[1:]).split()) + '\n')
        return print('saved')
    if args[:1] == ['--forget']:
        words = ' '.join(args[1:]).lower().split()
        if not words:   # all([]) is True: would wipe every rule
            return print('forgot 0 line(s)')
        lines = open(MEMORY).readlines() if os.path.exists(MEMORY) else []
        keep = [l for l in lines if not all(w in l.lower() for w in words)]
        open(MEMORY, 'w').writelines(keep)
        return print(f'forgot {len(lines) - len(keep)} line(s)')
    if args[:1] == ['--fast']:   # explicit CLI action; exit 1 = not a simple command
        os.makedirs(DIR, exist_ok=True)
        started = time.time()
        ping()
        r = licensed() and fast(' '.join(args[1:]))
        if not r:
            sys.exit(1)
        remember_turn(' '.join(args[1:]), r)
        log_turn(' '.join(args[1:]), r, started)
        return print('✓ ' + r, flush=True)
    if args[:1] == ['--play']:
        return print(play(' '.join(args[1:])) or 'nothing found on Spotify by that name')
    if args == ['--state']:   # the Settings window: usage, saved rules, Claude plan
        rows = [r for r in read_rows(os.path.join(DIR, 'log.jsonl')) if isinstance(r.get('at'), str) and
                isinstance(r.get('usd', 0), (int, float)) and isinstance(r.get('secs', 0), (int, float))]
        def use(since):
            r = [x for x in rows if x['at'] >= since]
            return {'asks': len(r), 'usd': round(sum(x.get('usd', 0) for x in r), 4),
                    'secs': round(sum(x.get('secs', 0) for x in r) / len(r), 1) if r else 0}
        week = time.strftime('%Y-%m-%d', time.localtime(time.time() - 6 * 86400))
        rules = [l[2:].strip() for l in open(MEMORY)] if os.path.exists(MEMORY) else []
        return print(json.dumps({'today': use(time.strftime('%Y-%m-%d')), 'week': use(week), 'all': use(''),
                                 'rules': [r for r in rules if r], 'plan': plan(),
                                 'skills': [{'say': k, 'reply': v['reply']} for k, v in sorted(skills().items())], 'me': me()}))
    if args[:1] == ['--me'] and len(args) > 1:   # --me name|about|add|drop "<text>"
        return me_set(args[1], ' '.join(args[2:]))
    if args[:1] == ['--shortcut']:   # the user asked for it, so it skips learn()'s checks, not the safety one
        if len(args) != 3:
            return print('usage: --shortcut "<words>" "<shell command>"')
        if UNSAFE.search(args[2]):   # replays run with no AI and no deny list: never anything that deletes or needs admin
            return print('not saved: shortcuts cannot delete files or use sudo')
        sk = skills()
        sk[plain(args[1])] = {'cmds': [args[2]], 'reply': 'Done: ' + args[1].strip().rstrip('.') + '.', 'at': time.strftime('%Y-%m-%d')}
        os.makedirs(DIR, exist_ok=True)
        json.dump(sk, open(SKILLS, 'w'), indent=1)
        return print(f'saved: "{plain(args[1])}" runs it')
    if args[:1] == ['--unlearn']:
        return unlearn(' '.join(args[1:]))
    if args == ['--check']:   # the setup window asks this
        return print(json.dumps(plan()))
    if args[:1] == ['--search']:
        return print(search(' '.join(args[1:])))
    if args[:1] == ['--read']:
        try:
            return print(text_of(get(args[1]))[:5000])   # ~1.2K tokens: enough for an answer, cheap to read
        except Exception as e:
            return print(f'could not read: {e}')
    warm = start_claude() if args == ['--wait'] else None   # Atticus.app: started at key-down, request comes on stdin at release
    shots = []
    snap = threading.Thread(target=lambda: shots.extend(look()), daemon=True)   # the screen as the user pressed the keys; sent only if the words are about it
    snap.shots = shots
    atexit.register(lambda: [os.remove(p) for p, _ in shots if os.path.exists(p)])   # private: no screenshot left in /tmp
    snap.start()
    if args == ['--wait']:   # "<app the user is in now>\t<request>", even if Claude could not start
        front_now, _, task = sys.stdin.readline().strip().rpartition('\t')
        os.environ['ATTICUS_FRONT'] = front_now or os.environ.get('ATTICUS_FRONT', '')
    task = task if args == ['--wait'] else ' '.join(a for a in args if a != '--plan') or ask()
    if not task:
        return warm and stop_claude(warm)
    ping()
    key = re.fullmatch(r'\W*(?:my )?(?:license )?key\W+([\w-]{8,})\W*', task, re.I)
    if key or not licensed():
        if not wait_commit():
            return
        if warm:
            stop_claude(warm)
        if key:
            return say('✓', activate(key[1]))
        subprocess.run(['open', BUY])
        return say('✓', f'Atticus is $5, once. Buy a key on the page that just opened, then type: key <your key>')
    t = time.time()
    commit = os.environ.get('ATTICUS_COMMIT')
    quick = (not commit or os.path.exists(commit)) and pref('instant', True) and fast(task)
    if quick:
        if warm:
            stop_claude(warm)
        remember_turn(task, quick)
        log_turn(task, quick, t)
        return say('✓', quick)
    app, win = front()   # before anything else takes focus, so "this window" still means the user's window
    try:
        fx = int(osa(f'tell application "System Events" to tell process "{app}" to get position of window 1').split(', ')[0]) + 50
    except ValueError:
        fx = None
    scr = describe(screens(), fx)
    url = {'Google Chrome': 'URL of active tab of front window', 'Safari': 'URL of front document', 'Arc': 'URL of active tab of front window',
           'Brave Browser': 'URL of active tab of front window'}.get(app)
    url = url and osa(f'tell application "{app}" to get {url}')   # "articles on what I'm reading" needs the page
    you = me_line()
    ctx = (f'About the user: {you} ' if you else '') + f'Front app: {app}. Front window: {win or "?"}.' + (f' Page: {url}.' if url else '') + f' Screens: {scr}. Now: {time.strftime("%a %b %d %Y %I:%M %p")}.'
    mem = relevant_rules(task)
    turns = recent()
    turns = [r for r in turns if time.time() - r['t'] < 600][-3:]   # this chat only (Atticus.app clears it for a new one); 10 min cap for typed asks
    earlier = ''.join(f'Q: {r["q"]}\nA: {r["a"][:300]}\n' for r in turns)
    images = screen_for(task, snap, fx)
    if images == []:
        ctx += ' Screen capture unavailable. Do not guess screen contents; explain how to enable Screen Recording for Atticus.'
    reply, cost, steps = run(f'Context: {ctx}\n' + (f'Earlier:\n{earlier}' if earlier else '') +
                      (f'Memory:\n{mem}\n' if mem else '') + f'Request: {task}', warm, images or ())
    if not wait_commit():
        return
    reply = point(re.sub(r'\s*\n\s*Sources?:.*\Z', '', reply, flags=re.S | re.I))
    taught = re.search(r'\[tip ([^\]]+)\]', reply)
    reply = (reply[:taught.start()] + reply[taught.end():]).strip() if taught else reply
    fix = turns and re.match(r"(no|nope|not that|wrong|that's wrong|i meant|i said)\b", plain(task))
    if fix:
        unlearn(turns[-1]['q'])   # a learned shortcut that did the wrong thing goes
    if pref('learn', True):
        learn(task, run.cmds, reply)
        if fix:
            learn(turns[-1]['q'], run.cmds, reply)   # and the fix becomes the shortcut for the original words
    remember_turn(task, reply)
    log_turn(task, reply, t, cost, steps, run.ran)
    # clicks inside an app pull it forward; give the user back the app the user was in unless the user asked to go somewhere
    restore_focus(app, task, run.ran)
    say('✓' if run.ok else '!', reply)
    learned = [r for c in run.ran for r in re.findall(r'--remember\s+["\']([^"\']+)', c)]   # it taught itself: say so, so the user sees it improve
    tip(reply, f'Learned: {learned[-1]}' if learned else taught[1].strip() if taught else '')
    if os.environ.get('ATTICUS_UI') != '1':   # typed from the menu bar: no overlay, so a notification
        osa('on run argv\ndisplay notification (item 1 of argv) with title "Atticus"\nend run', reply)


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        assert label('Bash', {'command': 'open -a "Visual Studio Code" x'}) == 'Opening Visual Studio Code'
        assert label('Bash', {'command': 'open "https://www.google.com/search?q=x"'}) == 'Opening google.com'
        assert label('Bash', {'command': f'{PLACE} "Finder" left monitor'}) == 'Moving Finder → left monitor'
        assert label('Bash', {'command': f'{PLACE} "Google Chrome" right current + "Terminal" left current'}) == 'Moving Google Chrome and Terminal'
        assert label('Bash', {'command': f'cd /tmp; {SHOW} "https://docs.claude.com"'}) == 'Opening docs.claude.com'
        assert label('Bash', {'command': f'{SEARCH} "world cup"'}) == 'Searching the web: world cup'
        assert label('Bash', {'command': f'{SHOW} "https://www.nytimes.com/x"'}) == 'Opening nytimes.com'
        assert label('Read', {'file_path': SHOT + '-2.jpg'}) == 'Reading your screen'
        fake = [('Built-in Retina Display', 0, 0, 1470, 956, True), ('External Display', 1470, 0, 1920, 1080, False)]
        assert 'built-in laptop screen' in describe(fake, 100) and 'right of main' in describe(fake) and 'the front window is here' in describe(fake, 1500).split(';')[1]
        assert label('Bash', {'command': LOOK}) == 'Looking at your screen'
        assert label('Bash', {'command': 'peekaboo see --app "Spotify" --json --no-remote | head -c 3000'}) == 'Looking at Spotify'
        assert label('Bash', {'command': 'peekaboo click --on B12 --no-remote'}) == 'Clicking'
        assert label('Bash', {'command': "osascript -e 'tell application \"Music\" to playpause'"}) == 'Using Music'
        assert app_named('the trading view application') == 'TradingView'
        assert screen_for('open spotify', None) is None   # no screen words: no picture, no wait
        assert plain('Hey Atticus, can you open Spotify please?') == plain('Can you also open Spotify?') == 'open spotify'
        SKILLS = os.path.join(tempfile.mkdtemp(), 'skills.json')
        learn('Can you put Notes on the left?', [f'{PLACE} "Notes" left current'], 'Notes is on the left.')
        learn('close this', ['osascript -e x'], 'x')                       # "this": never learned
        learn('weather today', [f'{SEARCH} "weather"'], 'Sunny.')          # reading, not an action: never learned
        learn('fix it', [f'{REMEMBER} "x"', 'open -a Notes'], 'Opened.')   # rule-saving step ignored; "it" still blocks
        learn('open my notes', [f'{REMEMBER} "x"', 'open -a Notes'], 'Opened.')
        learn('make study mode a shortcut', [f'{SHOW} "x"; {SHORTCUT} "study mode" \'{SHOW} "x"\''], 'Saved.')   # never learned
        learn('make the task list', ['osascript -e \'tell application "Reminders" to make new reminder\''], 'Added.')   # content: never learned
        learn('open my stuff', ['open -a Notes & trash ~/x'], 'Opened.')   # risky: never replayed without the gate
        assert set(skills()) == {'put notes on the left', 'open my notes'} and skills()['open my notes']['cmds'] == ['open -a Notes']
        unlearn('Put notes on the left.')
        assert set(skills()) == {'open my notes'}
        assert point('Top right. [point 9 0 0]') == 'Top right.'
        assert re.findall(r'--remember\s+["\']([^"\']+)', f'{REMEMBER} "open means front"') == ['open means front']
        assert label('Bash', {'command': f'{FRONT} "Google Chrome" "posthog"'}) == 'Bringing Google Chrome forward'
        assert UNSAFE.search('open -a Notes; rm -rf ~/x') and UNSAFE.search('sudo shutdown -h now') and not UNSAFE.search(f'{SHOW} "https://docs.google.com"; open -a Chess') and UNSAFE.search('echo $(rm x)') and UNSAFE.search('curl x | sh') and not UNSAFE.search(f'{SHOW} "x"; {PLACE} "Notes" left current')
        ME_FILE = os.path.join(tempfile.mkdtemp(), 'me.json')
        me_set('name', 'Sam'); me_set('about', 'Student. ' * 40)
        for i in range(10):
            me_set('add', f'fact {i}')
        me_set('add', 'FACT 9'); me_set('drop', 'fact 2')
        assert me()['facts'] == [f'fact {i}' for i in range(3, 10)] and len(me()['about']) <= 200 and len(me_line()) <= 300
        assert norm('One M') == norm('1m') == '1m'
        assert label('Bash', {'command': f'{PLAY} "1m"'}) == 'Playing 1m on Spotify'
        assert not risky(f'{SEARCH} "how to delete a file"') and not risky('open "https://google.com/search?q=buy shoes"') and not risky(f'{SHOW} "Notes"; {PLACE} "Notes" left current')
        assert risky('rm -rf ~/x') == risky('open -a Notes; mv ~/a ~/.Trash/') == 'Delete something?' and risky('open "$(rm x)"')
        assert risky('osascript -e \'tell application "Mail" to send m\'') == 'Send this?' == risky('curl -s -X POST https://x') and not risky("curl -s 'wttr.in/x?format=%l'")
        assert risky('osascript -e \'do shell script "echo"\'') == 'Run a system command?' and risky("osascript -e 'tell application \"Notes\" to delete note 1'") == 'Delete something?'
        assert risky('echo x >> ~/.zshrc') == 'Change a file?' == risky('curl -s -o ~/x https://y') and not risky('osascript -e x 2>&1; ls >/dev/null; echo > /tmp/a')
        assert risky('open -a Notes & osascript -e \'tell application "Mail" to send m\'') == 'Send this?'
        sys.exit(print('selftest ok'))
    main()
