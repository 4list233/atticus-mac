"""Syntax checks for the embedded UI modules using the installed Node runtime."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'Node is optional for UI syntax checks')
class JavaScriptTests(unittest.TestCase):
    def test_embedded_scripts_parse(self):
        root = Path(__file__).resolve().parents[1]
        files = list((root / 'ui').glob('*.html')) + list((root / 'tests').glob('*.html'))
        for path in files:
            for attrs, code in re.findall(r'<script([^>]*)>(.*?)</script>', path.read_text(), re.S):
                if 'text/plain' in attrs or 'src=' in attrs:
                    continue
                with self.subTest(file=path.name):
                    p = subprocess.run(['node', '--check', '--input-type=module'], input=code, capture_output=True, text=True)
                    self.assertEqual(p.returncode, 0, p.stderr)

    def test_failed_meeting_stream_restores_retry(self):
        source = (Path(__file__).resolve().parents[1] / 'ui' / 'meeting.html').read_text()
        helper = re.search(r'const retryAnswer=.*?;\n', source).group(0)
        script = '''
let sent, touched=new Set(), post=value=>sent=value;
const old={removed:false,remove(){this.removed=true}};
const body={button:null,append(value){this.button=value}};
const card={dataset:{id:'q1',question:'Question?'},querySelector(selector){
 if(selector==='.response.streaming')return old;
 if(selector==='.answer')return body.button;
 if(selector==='.body')return body;
}};
const document={createElement:()=>({className:'',classList:{remove(){}},disabled:true,textContent:'',onclick:null})};
''' + helper + '''
const button=retryAnswer(card);
button.onclick();
if(!old.removed||button.disabled||button.textContent!=='Try again'||sent.id!=='q1'||sent.text!=='Question?'||!touched.has('q1'))process.exit(1);
'''
        result = subprocess.run(['node', '--input-type=module'], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_onboarding_finish_names_missing_voice_requirements(self):
        source = (Path(__file__).resolve().parents[1] / 'ui' / 'onboarding.html').read_text()
        helper = source[source.index('  const paintDone='):source.index('\n  window.ob', source.index('  const paintDone='))]
        script = '''
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,{textContent:''});return nodes.get(id)};
''' + helper + '''
paintDone({claude:false,mic:true,speech:false,ax:false});
if($('done-title').textContent!=='Setup isn’t finished yet'||!$('done-sub').textContent.includes('AI provider, Speech Recognition, Accessibility')||$('finish').textContent!=='Finish Setup Later')process.exit(1);
paintDone({claude:true,mic:true,speech:true,ax:false});
if($('done-title').textContent!=='Voice is ready'||$('finish').textContent!=='Start Using Atticus')process.exit(2);
'''
        result = subprocess.run(['node', '--input-type=module'], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_start_failure_has_persistent_recovery_actions(self):
        root = Path(__file__).resolve().parents[1]
        page = (root / 'ui' / 'ui.html').read_text()
        native = (root / 'Atticus.swift').read_text()
        self.assertIn('Atticus’s helper couldn’t start', page)
        self.assertIn("post({workerRetry:true})", page)
        self.assertIn("post({setup:true})", page)
        self.assertIn('pinned = true; js("workerFailed"); show()', native)
        self.assertIn('if b["workerRetry"] != nil, !busy', native)

    def test_paused_shortcut_offers_rate_limited_resume(self):
        root = Path(__file__).resolve().parents[1]
        page = (root / 'ui' / 'ui.html').read_text()
        native = (root / 'Atticus.swift').read_text()
        self.assertIn("post({resume:true})", page)
        self.assertIn("Date().timeIntervalSince(pausedNotice) > 4", native)
        resume = native[native.index('if b["resume"] != nil {'):native.index('if b["kofi"]')]
        self.assertIn('Atticus.settings.apply("paused", false)', resume)


if __name__ == '__main__':
    unittest.main()
