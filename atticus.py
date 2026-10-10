#!/usr/bin/env python3
"""Atticus: a small Mac helper run by Claude Haiku through the Claude Code CLI (your Claude plan). Idea from Caleb Writes Code's "Atticus".

Hold Control+Option+Space (Atticus.app) -> talk -> let go. It answers questions (web, files, screen) and does
tasks on the Mac. Output lines for the overlay: "· note", "▸ step", "✓ answer".
  atticus.py "what's on my screen?"      run one request (no args = a typed pop-up, used by the SwiftBar menu)
  atticus.py --place <proc> <left|right|full|center> <laptop|monitor|current> [title]
  atticus.py --look                       screenshot of the screen the user is using -> prints the image path
  atticus.py --show <url|path|app>       open it on the user's other screen and give focus back to what the user was using
  atticus.py --remember "<line>"  |  --forget "<words>"   the user's saved rules, one line each, sent with every request
  atticus.py --fast "<words>"            run it instantly with no AI if it is a simple command (exit 1 if not)
  atticus.py --play "<playlist name>"     play one of the user's Spotify playlists, no clicks, focus stays put
  atticus.py --search <query>  |  --read <url>   free web search (DuckDuckGo) / one page as plain text
Every run goes to ~/.config/atticus/log.jsonl with its cost.
"""
import base64, html, json, shutil, threading, os, re, signal, subprocess, sys, tempfile, time, urllib.parse, urllib.request

HOME = os.path.expanduser('~')
DIR = os.path.join(HOME, '.config/atticus')
ME = os.path.abspath(__file__)   # inside Atticus.app once built, so the AI's helper commands point here
PLACE, LOOK, SEARCH, READ, SHOW, REMEMBER, FORGET, TYPE, PLAY = (ME + f' --{c}' for c in ('place', 'look', 'search', 'read', 'show', 'remember', 'forget', 'type', 'play'))
MEMORY, RECENT = os.path.join(DIR, 'memory.md'), os.path.join(DIR, 'recent.jsonl')
PREFS = os.path.join(DIR, 'settings.json')   # written by the Settings window


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
Speed matters most; every extra round costs the user a second. Answer without a tool when you can. Do the whole task in ONE Bash call when you can (chain with ;). Need several things? Ask for them all in the same turn, not one after another. Answer from the first good source; don't double-check.
Before EACH tool call write a 3-8 word note of what you are doing; it shows live. Plain words only, never tool or command names (no peekaboo, screenshot, osascript): "Looking at your screen", "Clicking Play".
Final reply: plain text, no markdown, at most 3 short sentences, answer first. For a done task, one sentence.
Be cheap: fewest tool calls. Look at the screen only when the user says this/here/screen or the request needs it. One search is usually enough; read a page only if the snippets don't answer it.
Tools:
- See the user's screen: when the request is about it, a labeled picture of each of the user's screens comes with the request: use the one the user means ("my monitor" = the external one; "my screen" with no hint = the one the user's front window is on), don't take another. Otherwise run {LOOK} (prints an image path), then Read that path.
- Web (free, use first): {SEARCH} "<query>" -> top 5 titles, urls, snippets, plus the start of the top 3 pages (usually enough: answer from it). {READ} "<url>" -> that page as text. WebSearch/WebFetch only if those fail. 
- Open anything for the user (page, file, app): {SHOW} "<url | path | app name>". It appears on the user's other screen and the user's focus stays where it was, so the user keeps working. Use open -a / activate only when the user says switch to or focus.
- The user's files (Spotlight): mdfind -name "budget" | head -20  |  by content: mdfind -onlyin ~/Documents "photosynthesis" | head
- Weather: curl -s 'wttr.in/<place>?format=%l:+%C+%t+(feels+%f),+wind+%w&u'
- Papers: curl -s "http://export.arxiv.org/api/query?search_query=all:<q>&max_results=5"
- Move, snap or resize a window (does the math): {PLACE} "<process>" <left|right|full|center> <laptop|monitor|current|exact screen name from Context> ["part of title"]. Use it for EVERY window move. "Move to my laptop/monitor" = center. Side by side = two calls, left and right.
- Window names: osascript -e 'tell application "System Events" to get name of every window of process "Safari"'. Raise one: ... to perform action "AXRaise" of (first window whose name contains "x"), then activate the app.
- Type text into the box the user is in (search bar, chat box, doc): {TYPE} "<text>". It goes back to the user's app and pastes; one call, done. Never use peekaboo for typing.
- Last resort, only when nothing above works (it moves the user's real mouse and pulls the app forward) — click or read buttons/menus inside an app: peekaboo see --app "<App>" --json --no-remote | head -c 3000 (always cut it; lists elements with IDs), then peekaboo click --on <ID> --no-remote, peekaboo type "text" --no-remote, peekaboo menu click --app "<App>" --path "File > New" --no-remote. Run peekaboo <cmd> --help if unsure.
- Spotify: play any song, album, artist or the user's own playlist: {PLAY} "<name, plus artist if known>" (no clicks, the user's focus stays). Music requests mean Spotify unless the user names another app. Pause/skip: osascript -e 'tell application "Spotify" to playpause' (or next track). Never click in Spotify unless those fail.
- Clipboard: pbpaste. Music: osascript -e 'tell application "Music" to playpause'.
Earlier: the last few questions from the past 5 minutes, if any; use them for follow-ups ("open the second one", "and tomorrow?").
Memory: the user's saved rules come under "Memory:" in the request; follow them. When the user says remember / from now on / when I say X do Y, save ONE short line with {REMEMBER} "<rule>". When the user says forget, run {FORGET} "<words from that line>".
Corrections: when the user says "no, I meant…", fix it, then save ONE short rule with {REMEMBER} so it won't happen again.
Rules: "this", "it", "here" mean the front app/window in Context. Process names can differ from app names (VS Code is "Code").
Never delete, move files, send, post or buy anything. If a command fails, try one other way, then say what failed."""

POINTER = """
Show me where (on): when the user asks where something is or how to do something on screen, answer in one line, then end with
[point N X Y]: N = the screen number of the labeled picture, X Y = the pixel spot in that picture. A ring appears there."""

# Full access: everything runs without asking, except these, which a misheard word must never trigger.
DENY = [f'Bash({c}:*)' for c in ('rm', 'rmdir', 'sudo', 'dd', 'diskutil', 'shutdown', 'reboot', 'halt', 'git push', 'srm', 'trash')]


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


def place(proc, where='full', screen='current', title=''):
    """Move a window with the math done here: where = left|right|full|center, screen = laptop|monitor|current."""
    if where not in ('left', 'right', 'full', 'center'):
        return 'where must be left, right, full or center'
    # the real window: Chrome's window 1 can be a hidden popup bar
    win = f'(first window whose name contains "{title}")' if title else '(first window whose subrole is "AXStandardWindow")'
    tell = f'tell application "System Events" to tell process "{proc}" to '
    for tries in (0, 1):
        # full screen (or split view) windows can't move; checked each try, since it only shows once the app is forward
        if osa(tell + f'get value of attribute "AXFullScreen" of {win}') == 'true':
            osa(tell + f'set value of attribute "AXFullScreen" of {win} to false')
            time.sleep(1.2)   # the exit animation
        try:
            px, py = map(int, osa(tell + f'get position of {win}').split(', '))
            ww, wh = map(int, osa(tell + f'get size of {win}').split(', '))
            app_mode = False
            break
        except ValueError:   # System Events can't see windows on another Space: bring the app forward, look again
            if not tries:
                osa(f'tell application "{proc}" to activate')
                time.sleep(0.4)
    else:   # still hidden; scriptable apps (Finder, Safari…) still answer for their own windows
        try:
            l, t, r, b = map(int, osa(f'tell application "{proc}" to get bounds of window 1').split(', '))
        except ValueError:
            return f'no window found for "{proc}" {title}'
        px, py, ww, wh, app_mode = l, t, r - l, b - t, True
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
    one = f'tell application "{proc}" to set bounds of window 1 to {{{bx}, {by}, {bx + bw}, {by + bh}}}'
    if (title and not app_mode) or subprocess.run(['osascript', '-e', one], capture_output=True).returncode:
        osa(tell + f'set size of {win} to {{{bw}, {bh}}}')   # size first, then move, then size again
        osa(tell + f'set position of {win} to {{{bx}, {by}}}')
        osa(tell + f'set size of {win} to {{{bw}, {bh}}}')
    return f'{proc} window -> {where} of {n}'


def show(target):
    """Open a URL, file or app where the user can see it, then hand focus back to the app the user was using."""
    mine, _ = front()
    t = os.path.expanduser(target)
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
    t = re.sub(r'\s+please$', '', t)
    return re.sub(r'[,;:.!?]', '', t)   # the speech model adds punctuation


# Learned shortcuts: an action the AI got right is saved as the exact commands it ran; the same words later replay
# them with no AI (free, under a second). Kept in a file, never sent to the AI, so they cost nothing as they grow.
SKILLS = os.path.join(DIR, 'skills.json')
DEICTIC = re.compile(r'\b(this|that|these|those|it|here|there|them|him|her)\b')   # "close this" depends on the moment: never learned


def skills():
    try:
        return json.load(open(SKILLS))
    except (OSError, ValueError):
        return {}


def learn(task, cmds, reply):
    """Save the commands if every one is a replayable action (no reading, searching, clicking by element ID)."""
    k = plain(task)
    cmds = [c for c in cmds if not c.startswith((REMEMBER, FORGET))]   # saving a rule is not part of the action
    act = (SHOW, PLACE, PLAY, TYPE, 'open ', 'osascript ')
    if not cmds or DEICTIC.search(k) or not all(c.startswith(act) and 'peekaboo' not in c for c in cmds):
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
    for c in s['cmds']:   # any failure: hand it to the AI instead
        if subprocess.run(['/bin/zsh', '-c', c], capture_output=True, timeout=20).returncode:
            return None
    return s['reply']


def fast(text):
    """Simple commands with no AI: learned shortcuts, open <app/site>, snap/move <it/app> left/right/full/center, to laptop/monitor.
    Returns a reply, or None to hand the request to Haiku."""
    t = plain(text)
    if t in ('no', 'yes', 'ok', 'okay', 'nevermind', 'never mind', 'cancel', 'nothing', 'thanks', 'thank you'):
        return 'Okay.'   # stray word after a turn: no AI call
    if pref('learn', True) and t in skills():
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
    if m:   # "play X" alone may mean a video: only the user's playlist here, anything else goes to the AI (which uses --play)
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
        if re.fullmatch(r'[\w-]+(\.[\w-]+)+(/\S*)?', target.replace(' dot ', '.')):
            subprocess.run(['open', 'https://' + target.replace(' dot ', '.')])
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
        path = f'{SHOT}-{i}.jpg'
        if os.path.exists(path):
            os.remove(path)
        # Atticus.app takes it, leaving its own box out; plain screencapture is the fallback (box included)
        if subprocess.run([APP, '--look', str(i), path], capture_output=True).returncode:
            subprocess.run(['screencapture', '-x', '-D', str(i), '-t', 'jpg', path])
            subprocess.run(['sips', '-Z', '1280', '-s', 'formatOptions', '70', path], capture_output=True)
        s = scr[i - 1]
        return path, f'Screen {i}: "{s[0]}" ({"built-in laptop screen" if s[5] else "external monitor"})'
    from concurrent.futures import ThreadPoolExecutor
    return [r for r in ThreadPoolExecutor(len(scr)).map(one, range(1, len(scr) + 1)) if os.path.exists(r[0])]


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
    c = ' '.join(inp.get('command', '').split())
    if c.startswith(SEARCH):
        return 'Searching the web: ' + c[len(SEARCH):].strip(' "\'')
    if c.startswith(READ):
        return 'Reading ' + re.sub(r'^https?://(www\.)?([^/]+).*', r'\2', c[len(READ):].strip(' "\''))
    if c.startswith(SHOW):
        t = c[len(SHOW):].strip(' "\'')
        return 'Opening ' + (re.sub(r'^https?://(www\.)?([^/]+).*', r'\2', t) if '://' in t else os.path.basename(t))
    if c.startswith(REMEMBER):
        return 'Remembering that'
    if c.startswith(FORGET):
        return 'Forgetting that'
    if c.startswith(PLAY):
        return 'Playing ' + c[len(PLAY):].strip(' "\'') + ' on Spotify'
    if c.startswith(LOOK):
        return 'Looking at your screen'
    if c.startswith(PLACE):
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
    return c[:60]


def say(tag, text):
    print(f'{tag} {" ".join(text.split())}', flush=True)


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


def start_claude():
    """`claude -p` started ahead, waiting for the request on stdin: its ~1 s startup happens while the user is still talking."""
    if not shutil.which('claude'):
        return None
    system = SYSTEM + (POINTER if pref('pointer', False) else '')
    p = subprocess.Popen(['claude', '-p', '--input-format', 'stream-json', '--model', pref('model', 'haiku'), '--effort', 'low', '--system-prompt', system,
                          '--tools', 'Bash,Read,WebSearch,WebFetch', '--permission-mode', 'bypassPermissions', '--disallowedTools', *DENY,
                          '--setting-sources', '', '--no-session-persistence', '--strict-mcp-config', '--disable-slash-commands', '--output-format', 'stream-json', '--verbose'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tempfile.gettempdir())
    signal.signal(signal.SIGTERM, lambda *_: (p.kill(), sys.exit(0)))   # the hotkey pressed again = cancel
    return p


def run(task, p=None, shots=()):
    """One request through `claude -p --model haiku` (the user's plan), streaming notes and steps. shots: [(path, label)] sent with it."""
    run.cmds = []
    p = p or start_claude()
    if not p:
        say('!', 'setup')   # Atticus.app opens its setup window
        return plan()['text'] + ' Atticus setup is open to fix it.', 0, []
    content = [b for path, label in shots for b in ({'type': 'text', 'text': label}, {'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/jpeg', 'data': base64.b64encode(open(path, 'rb').read()).decode()}})] + [{'type': 'text', 'text': task}]
    p.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}) + '\n')
    p.stdin.close()
    reply, cost, steps, cmds, failed = '', 0, [], [], False
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
            reply, cost = (j.get('result') or '').strip(), j.get('total_cost_usd', 0)
    p.wait()
    if not reply and p.returncode:
        c = plan()   # the usual cause: not installed, signed out, or no paid plan
        if not c['ok']:
            say('!', 'setup')
        reply = c['text'] + ' Atticus setup is open to fix it.' if not c['ok'] else f'Claude failed: {p.stderr.read()[:200]}'
    run.cmds = [] if failed or p.returncode else cmds   # what learn() may save
    return reply or 'Done.', cost, steps


def screen_for(task, snap, fx=None):   # snap.shots: what look() returned; fx: x of the front window
    """The key-down screenshot, if the request points at the screen ("what does this say", "articles on what I'm reading")."""
    if pref('screen', 'auto') == 'never' or not re.search(r"\b(screen|this|these|that|here|it|reading|looking|see|says?|shown?|page|tab|window|error|question|problem|where|how do i|button|click)\b", task.lower()):
        return None
    snap.join(5)
    if len(snap.shots) < 2 or fx is None or re.search(r"\b(monitor|laptop|other|second|both|all|external|built.in|screens)\b", task.lower()):
        return snap.shots
    on = next((i for i, s in enumerate(screens()) if s[1] <= fx < s[1] + s[3]), 0)   # just the one the user is on: half the cost
    return [snap.shots[on]] if on < len(snap.shots) else snap.shots


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


def remember_turn(q, a):
    turns = [json.loads(l) for l in open(RECENT)] if os.path.exists(RECENT) else []
    turns = [r for r in turns if time.time() - r['t'] < 300][-2:] + [{'t': time.time(), 'q': q, 'a': a}]
    with open(RECENT, 'w') as f:
        f.writelines(json.dumps(r) + '\n' for r in turns)


def ask():
    out = osa('tell application "System Events"\nactivate\nset r to display dialog "What should I do?" default answer "" '
              'buttons {"Cancel", "Go"} default button "Go" with title "Atticus"\nreturn text returned of r\nend tell')
    return out.strip()


def main():
    args = sys.argv[1:]
    if args[:1] == ['--place']:
        return print(place(*args[1:]))
    if args == ['--look']:
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
        lines = open(MEMORY).readlines() if os.path.exists(MEMORY) else []
        keep = [l for l in lines if not all(w in l.lower() for w in words)]
        open(MEMORY, 'w').writelines(keep)
        return print(f'forgot {len(lines) - len(keep)} line(s)')
    if args[:1] == ['--fast']:   # Atticus.app calls this mid-speech; exit 1 = not a simple command
        os.makedirs(DIR, exist_ok=True)
        r = fast(' '.join(args[1:]))
        if not r:
            sys.exit(1)
        remember_turn(' '.join(args[1:]), r)
        return print('✓ ' + r, flush=True)
    if args[:1] == ['--play']:
        return print(play(' '.join(args[1:])) or 'nothing found on Spotify by that name')
    if args == ['--state']:   # the Settings window: usage, saved rules, Claude plan
        rows = [json.loads(l) for l in open(os.path.join(DIR, 'log.jsonl'))] if os.path.exists(os.path.join(DIR, 'log.jsonl')) else []
        def use(since):
            r = [x for x in rows if x['at'] >= since]
            return {'asks': len(r), 'usd': round(sum(x.get('usd', 0) for x in r), 4),
                    'secs': round(sum(x.get('secs', 0) for x in r) / len(r), 1) if r else 0}
        week = time.strftime('%Y-%m-%d', time.localtime(time.time() - 6 * 86400))
        rules = [l[2:].strip() for l in open(MEMORY)] if os.path.exists(MEMORY) else []
        return print(json.dumps({'today': use(time.strftime('%Y-%m-%d')), 'week': use(week), 'all': use(''),
                                 'rules': [r for r in rules if r], 'plan': plan(),
                                 'skills': [{'say': k, 'reply': v['reply']} for k, v in sorted(skills().items())]}))
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
    snap.start()
    if warm:   # "<app the user is in now>\t<request>": the user may have switched apps mid-sentence ("open Chrome and…")
        front_now, _, task = sys.stdin.readline().strip().rpartition('\t')
        os.environ['ATTICUS_FRONT'] = front_now or os.environ.get('ATTICUS_FRONT', '')
    app, win = front()   # before anything else takes focus, so "this window" still means the user's window
    try:
        fx = int(osa(f'tell application "System Events" to tell process "{app}" to get position of window 1').split(', ')[0]) + 50
    except ValueError:
        fx = None
    scr = describe(screens(), fx)
    url = {'Google Chrome': 'URL of active tab of front window', 'Safari': 'URL of front document', 'Arc': 'URL of active tab of front window',
           'Brave Browser': 'URL of active tab of front window'}.get(app)
    url = url and osa(f'tell application "{app}" to get {url}')   # "articles on what I'm reading" needs the page
    ctx = f'Front app: {app}. Front window: {win or "?"}.' + (f' Page: {url}.' if url else '') + f' Screens: {scr}. Now: {time.strftime("%a %b %d %Y %I:%M %p")}.'
    task = task if warm else ' '.join(a for a in args if a != '--plan') or ask()
    if not task:
        return warm and warm.kill()
    t = time.time()
    quick = pref('instant', True) and fast(task)
    if quick:
        if warm:
            warm.kill()
        remember_turn(task, quick)
        return say('✓', quick)
    mem = relevant_rules(task)
    turns = [json.loads(l) for l in open(RECENT)] if os.path.exists(RECENT) else []
    turns = [r for r in turns if time.time() - r['t'] < 600][-3:]   # this chat only (Atticus.app clears it for a new one); 10 min cap for typed asks
    earlier = ''.join(f'Q: {r["q"]}\nA: {r["a"][:300]}\n' for r in turns)
    reply, cost, steps = run(f'Context: {ctx}\n' + (f'Earlier:\n{earlier}' if earlier else '') +
                      (f'Memory:\n{mem}\n' if mem else '') + f'Request: {task}', warm, screen_for(task, snap, fx) or ())
    reply = point(reply)
    fix = turns and re.match(r"(no|nope|not that|wrong|that's wrong|i meant|i said)\b", plain(task))
    if fix:
        unlearn(turns[-1]['q'])   # a learned shortcut that did the wrong thing goes
    if pref('learn', True):
        learn(task, run.cmds, reply)
        if fix:
            learn(turns[-1]['q'], run.cmds, reply)   # and the fix becomes the shortcut for the original words
    remember_turn(task, reply)
    with open(os.path.join(DIR, 'log.jsonl'), 'a') as f:
        f.write(json.dumps({'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'task': task, 'reply': reply,
                            'usd': round(cost, 5), 'secs': round(time.time() - t, 1), 'steps': steps}) + '\n')
    # clicks inside an app pull it forward; give the user back the app the user was in unless the user asked to go somewhere
    if not re.search(r'\b(switch|focus|go to|bring|full ?screen)\b', task.lower()) and osa('tell application "System Events" to get name of first process whose frontmost is true') != app:
        osa(f'tell application "{app}" to activate')
    say('✓', reply)
    if os.environ.get('ATTICUS_UI') != '1':   # typed from the menu bar: no overlay, so a notification
        osa('on run argv\ndisplay notification (item 1 of argv) with title "Atticus"\nend run', reply)


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        assert label('Bash', {'command': 'open -a "Visual Studio Code" x'}) == 'Opening Visual Studio Code'
        assert label('Bash', {'command': 'open "https://www.google.com/search?q=x"'}) == 'Opening google.com'
        assert label('Bash', {'command': f'{PLACE} "Finder" left monitor'}) == 'Moving Finder → left monitor'
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
        assert plain('Hey Atticus, can you open Spotify please?') == 'open spotify'
        SKILLS = os.path.join(tempfile.mkdtemp(), 'skills.json')
        learn('Can you put Notes on the left?', [f'{PLACE} "Notes" left current'], 'Notes is on the left.')
        learn('close this', ['osascript -e x'], 'x')                       # "this": never learned
        learn('weather today', [f'{SEARCH} "weather"'], 'Sunny.')          # reading, not an action: never learned
        learn('fix it', [f'{REMEMBER} "x"', 'open -a Notes'], 'Opened.')   # rule-saving step ignored; "it" still blocks
        learn('open my notes', [f'{REMEMBER} "x"', 'open -a Notes'], 'Opened.')
        assert set(skills()) == {'put notes on the left', 'open my notes'} and skills()['open my notes']['cmds'] == ['open -a Notes']
        unlearn('Put notes on the left.')
        assert set(skills()) == {'open my notes'}
        assert point('Top right. [point 9 0 0]') == 'Top right.'
        assert norm('One M') == norm('1m') == '1m'
        assert label('Bash', {'command': f'{PLAY} "1m"'}) == 'Playing 1m on Spotify'
        sys.exit(print('selftest ok'))
    main()
