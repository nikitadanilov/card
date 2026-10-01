"""Tests of card: syntax, attributes, links, notes, styles, series and the
validity of the HTML output.

Run from the top directory of the repository:

    python3 -m unittest discover -s tests

Every successful build is checked by card.check_html_dir(): all generated
HTML files must be well-formed, and all internal hyperlinks must lead to
existing files and anchors. feed.xml must be well-formed XML.

"""

import contextlib
import http.server
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CARD = os.path.join(ROOT, 'card')

def load_card():
    loader = importlib.machinery.SourceFileLoader('cardtool', CARD)
    spec   = importlib.util.spec_from_loader('cardtool', loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules['cardtool'] = module
    loader.exec_module(module)
    return module

cardtool = load_card()

CONF = """/// dir out
/// deck.url https://example.com/deck
/// deck.title Test deck
/// deck.description A deck for testing.
"""

STYLES = r"""/// +it (^|[ ])(?P<open>_)[^_]*(?P<close>_)
/// +em (^|[ ])(?P<open>\*)[^*]*(?P<close>\*)
/// +bd (^|[ ])(?P<open>\+)[^+]*(?P<close>\+)
/// +un (^|[ ])(?P<open>-)[^-]*(?P<close>-)
/// +cd (^|[ ])(?P<open>`)[^`]*(?P<close>`)
/// +ma \\\((?P<open>)[^\\]*(?P<close>)\\\)
/// +al (^|[ ])(?P<open><<)[^<]*(?P<close><<)
/// +ar (^|[ ])(?P<open>>>)[^>]*(?P<close>>>)
"""

class Timeout(Exception):
    pass

def alarm(signum, frame):
    raise Timeout('card did not terminate')

class Build:
    def __init__(self, tmp, deck, stdout):
        self.tmp    = tmp
        self.deck   = deck
        self.stdout = stdout
        self.out    = os.path.join(tmp, deck.get('dir', str, 'html_out'))
    def path(self, name):
        return os.path.join(self.out, name)
    def exists(self, name):
        return os.path.exists(self.path(name))
    def html(self, name):
        with open(self.path(name), encoding = 'utf-8') as fd:
            return fd.read()
    def body(self, name):
        """Card HTML with layout whitespace removed."""
        return re.sub(r'\n *', '', self.html(name).split('<body>', 1)[1])
    def frag(self, name, anchor):
        """HTML of the fragment with the given anchor, up to the next anchor."""
        b = self.body(name)
        start = b.index('<a name="{}"></a>'.format(anchor))
        rest  = b[start + 1:]
        m = re.search(r'<a name="|<p class="margin-|<footer>', rest)
        return b[start : start + 1 + (m.start() if m else len(rest))]
    def text(self, name, anchor):
        return re.sub(r'<[^>]*>', '', self.frag(name, anchor))
    def node(self, ident):
        return self.deck.ids[ident]

class CardTest(unittest.TestCase):
    maxDiff = None

    def build(self, *sources, conf = CONF, validate = True, names = None):
        """Writes the sources into cardfiles and runs card on them.

        Returns a Build. Raises whatever card raises.
        """
        tmp = tempfile.mkdtemp(prefix = 'card-test-')
        self.addCleanup(shutil.rmtree, tmp, True)
        files = []
        if conf is not None:
            sources = (conf,) + sources
            names = ['conf'] + (names or ['{}.deck'.format(chr(ord('a') + i)) for i in range(len(sources) - 1)])
        else:
            names = names or ['{}.deck'.format(chr(ord('a') + i)) for i in range(len(sources))]
        for (name, src) in zip(names, sources):
            with open(os.path.join(tmp, name), 'w', encoding = 'utf-8') as fd:
                fd.write(src)
            files.append(name)
        cwd = os.getcwd()
        out = io.StringIO()
        old = signal.signal(signal.SIGALRM, alarm)
        signal.alarm(20)
        os.chdir(tmp)
        try:
            with contextlib.redirect_stdout(out):
                d = cardtool.deck()
                for f in files:
                    d.cardread(f)
                d.crossref()
                d.build()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
            os.chdir(cwd)
        b = Build(tmp, d, out.getvalue())
        if validate:
            self.validate(b)
        return b

    def validate(self, b):
        self.assertEqual(cardtool.check_html_dir(b.out), [])
        if b.exists('feed.xml'):
            ET.parse(b.path('feed.xml'))

    def fails(self, *sources, exc = Exception, **kw):
        with self.assertRaises(exc):
            self.build(*sources, **kw)

    def cli(self, args, sources, cwd = None):
        tmp = tempfile.mkdtemp(prefix = 'card-test-')
        self.addCleanup(shutil.rmtree, tmp, True)
        for (name, src) in sources.items():
            with open(os.path.join(tmp, name), 'w', encoding = 'utf-8') as fd:
                fd.write(src)
        p = subprocess.run([sys.executable, CARD] + args, cwd = tmp,
                           capture_output = True, text = True, timeout = 120)
        return (p, tmp)

# ----------------------------------------------------------------------
# Syntax
# ----------------------------------------------------------------------

class TestSyntax(CardTest):
    def test_empty_lines_separate_fragments(self):
        b = self.build('// id c\nOne\ntwo.\n\n\n\nThree.\n')
        c = b.node('c')
        self.assertEqual([f.content() for f in c.frags], ['One two.', 'Three.'])
        self.assertIn('<p>One two.</p>', b.body('c.html'))

    def test_default_fragment_type_is_para(self):
        b = self.build('// id c\nText.\n')
        self.assertIsInstance(b.node('c').frags[0], cardtool.para)

    def test_fragment_types(self):
        b = self.build('// id c\n; para\nP\n\n; code\nC\n\n; math\nx^2\n\n'
                       '; head\nH\n\n; img\n/ url https://example.com/i.png\nI\n')
        types = [type(f) for f in b.node('c').frags]
        self.assertEqual(types, [cardtool.para, cardtool.code, cardtool.math,
                                 cardtool.head, cardtool.img])

    def test_type_line_ends_previous_fragment(self):
        b = self.build('// id c\nfirst\n; code\nsecond\n')
        self.assertEqual([f.content() for f in b.node('c').frags], ['first', 'second'])

    def test_unknown_fragment_type_fails(self):
        self.fails('// id c\n; table\nx\n')

    def test_type_line_without_type_fails(self):
        self.fails('// id c\n;\nx\n')

    def test_new_card(self):
        b = self.build('// id c0\nA\n\\\n// id c1\nB\n')
        self.assertEqual([c.id() for c in b.deck.cards[1:]], ['c0', 'c1'])
        self.assertTrue(b.exists('c0.html'))
        self.assertTrue(b.exists('c1.html'))

    def test_each_cardfile_starts_a_card(self):
        b = self.build('// id c0\nA\n', '// id c1\nB\n')
        self.assertEqual([c.id() for c in b.deck.cards[1:]], ['c0', 'c1'])

    def test_stray_separators_create_empty_cards(self):
        b = self.build('\\\n// id c0\nA\n\\\n')
        self.assertEqual([c.id() for c in b.deck.cards[1:]],
                         ['a.deck-00000', 'c0', 'a.deck-00002'])

    def test_attribute_levels(self):
        b = self.build('/// deckattr D V\n// cardattr C V\n// id c\nText\n/ fragattr F V\n')
        self.assertEqual(b.deck.get('deckattr'), 'D V')
        self.assertEqual(b.node('c').get('cardattr'), 'C V')
        self.assertEqual(b.node('c').frags[0].get('fragattr'), 'F V')

    def test_attribute_value_spaces(self):
        b = self.build('// id c\n//   key    a  b   c   \nx\n')
        self.assertEqual(b.node('c').get('key'), 'a  b   c')

    def test_list_attribute(self):
        b = self.build('// id c\n// +l a\n// +l b c\nx\n')
        self.assertEqual(b.node('c').get('l', list), ['a', 'b c'])

    def test_attribute_does_not_break_fragment(self):
        b = self.build('// id c\nOne\n/ k v\ntwo\n// k v\nthree\n/// k v\nfour\n')
        self.assertEqual([f.content() for f in b.node('c').frags], ['One two three four'])

    def test_deck_attribute_in_any_card(self):
        b = self.build('// id c0\nx\n\\\n// id c1\n/// k v\ny\n')
        self.assertEqual(b.deck.get('k'), 'v')

    def test_fragment_attribute_without_fragment_fails(self):
        self.fails('// id c\n/ k v\n')
        self.fails('// id c\nx\n\n/ k v\n')

    def test_attribute_without_value_fails(self):
        self.fails('// id c\nx\n/ k\n')
        self.fails('// k\n// id c\nx\n')
        self.fails('/// k\n// id c\nx\n')

    def test_markers_are_whole_words(self):
        b = self.build('// id c\n/usr/bin is a directory.\n//not an attribute\n;not a type\n')
        self.assertEqual(b.node('c').frags[0].content(),
                         '/usr/bin is a directory. //not an attribute ;not a type')

    def test_leading_blanks_do_not_protect(self):
        b = self.build('// id c\n; code\nint i;\n    // increment the counter\ni++;\n')
        self.assertEqual(b.node('c').get('increment'), 'the counter')
        self.assertNotIn('increment', b.body('c.html'))

    def test_continuation(self):
        b = self.build('// id c\n; code\n|// not an attribute\n|\n|; not a type\n||x\n|\\\n')
        self.assertEqual(b.node('c').frags[0].content(),
                         '// not an attribute\n\n; not a type\n|x\n\\')

    def test_continuation_over_blank_line_in_para(self):
        b = self.build('// id c\nOne\n|\ntwo\n')
        self.assertEqual(len(b.node('c').frags), 1)

    def test_continuation_only_in_first_column(self):
        b = self.build('// id c\n; code\n  |x\n')
        self.assertEqual(b.node('c').frags[0].content(), '  |x')

    def test_indentation(self):
        b = self.build('// id c\n>One\n\n>>Two\n\n; code\n>>>x\n')
        frags = b.node('c').frags
        self.assertEqual([f.get('>') for f in frags], ['>', '>>', '>>>'])
        body = b.body('c.html')
        self.assertIn('<p style="margin-left: 3em;">One</p>', body)
        self.assertIn('<p style="margin-left: 6em;">Two</p>', body)
        self.assertIn('<pre><code style="margin-left: 9em;">x</code></pre>', body)

    def test_indentation_last_wins(self):
        b = self.build('// id c\n>One\n>>>two\n')
        self.assertEqual(b.node('c').frags[0].get('>'), '>>>')

    def test_indentation_only_in_first_column(self):
        b = self.build('// id c\n; code\n >x\n')
        self.assertFalse(b.node('c').frags[0].has('>'))
        self.assertEqual(b.node('c').frags[0].content(), ' &gt;x'.replace('&gt;', '>'))

    def test_code_keeps_leading_blanks(self):
        b = self.build('// id c\n; code\n  a\n    b\n')
        self.assertIn('<code>  a\n    b</code>', b.html('c.html'))

    def test_para_lines_joined(self):
        b = self.build('// id c\nOne\n   two\nthree\n')
        self.assertEqual(b.node('c').frags[0].content(), 'One    two three')

    def test_no_trailing_newline(self):
        b = self.build('// id c\nlast line')
        self.assertEqual(b.node('c').frags[0].content(), 'last line')

    def test_html_escaping(self):
        b = self.build('// id c\n<b>bold</b> & "q"\n\n; code\nif (a < b && c > d)\n')
        body = b.body('c.html')
        self.assertIn('&lt;b&gt;bold&lt;/b&gt; &amp; &quot;q&quot;', body)
        self.assertIn('if (a &lt; b &amp;&amp; c &gt; d)', body)

    def test_unicode(self):
        b = self.build('// id c\n// name Ünïcödé — тест\nΑλφα – βήτα.\n')
        self.assertIn('Αλφα – βήτα.', b.body('c.html'))
        self.assertIn('Ünïcödé — тест', b.html('c.html'))

    def test_note_attribute_without_note_fails(self):
        self.fails('// id c\nx\n: id n\n')

    def test_note_attribute_must_follow_note(self):
        self.fails('// id c\nx y\n/ +* "x" Note.\n/ k v\n: id n\n')

# ----------------------------------------------------------------------
# Fragments
# ----------------------------------------------------------------------

class TestFragments(CardTest):
    def test_head(self):
        b = self.build('// id c\n; head\nOne\n\n; head\n/ level 3\nThree\n')
        body = b.body('c.html')
        self.assertIn('<h1>One</h1>', body)
        self.assertIn('<h3>Three</h3>', body)

    def test_img(self):
        b = self.build('// id c\n; img\n/ url https://example.com/pic.png\nCaption.\n\n; img\n/ url https://example.com/p2.png\n/ width 120\n')
        body = b.body('c.html')
        self.assertIn('<img src="https://example.com/pic.png" border="0" width="50%">', body)
        self.assertIn('<img src="https://example.com/p2.png" border="0" width="120">', body)
        self.assertIn('Caption.', body)

    def test_img_caption_below(self):
        b = self.build('// id c\n; img\n/ url https://example.com/p.png\nCaption.\n')
        self.assertRegex(b.frag('c.html', 'c-00000'), r'<img [^>]*><br>Caption\.')

    def test_img_indented(self):
        b = self.build('// id c\n; img\n/ url https://example.com/p.png\n>Caption.\n')
        self.assertIn('margin-left: 3em;', b.frag('c.html', 'c-00000'))

    def test_img_without_url_fails(self):
        self.fails('// id c\n; img\nCaption.\n')

    def test_math_fragment(self):
        b = self.build('// id c\n; math\n\\frac{a}{b}\n')
        html = b.html('c.html')
        self.assertIn('$$\\frac{a}{b}', html)
        self.assertIn('mathjax', html)

    def test_no_mathjax_without_math(self):
        b = self.build('// id c\nNo maths.\n')
        self.assertNotIn('mathjax', b.html('c.html'))

    def test_fragment_style_attribute(self):
        b = self.build('// id c\nRed.\n/ +html.style color: red;\n')
        self.assertIn('<p style="color: red;">Red.</p>', b.body('c.html'))

# ----------------------------------------------------------------------
# Attributes
# ----------------------------------------------------------------------

class TestAttributes(CardTest):
    def test_single_value_replaced(self):
        b = self.build('// id c\n// k a\n// k b\nx\n')
        self.assertEqual(b.node('c').get('k'), 'b')

    def test_list_without_plus_splits_characters(self):
        b = self.build('// id c\n// tags foo\nx\n')
        self.assertEqual(b.node('c').get('tags', list), ['f', 'o', 'o'])

    def test_plus_after_plain_fails(self):
        self.fails('// id c\n// k a\n// +k b\nx\n')

    def test_comment_attribute_ignored(self):
        b = self.build('// id c\n// C a comment\nx\n/ C another\n')
        self.assertNotIn('comment', b.body('c.html'))

    def test_title_name(self):
        b = self.build('// id c\n// name The Name\n// label The Label\nx\n')
        self.assertIn('<title>The Name</title>', b.html('c.html'))
        self.assertIn('<header>The Name</header>', b.html('c.html'))

    def test_title_label(self):
        b = self.build('// id c\n// label The Label\nx\n')
        self.assertIn('<title>The Label</title>', b.html('c.html'))

    def test_title_default(self):
        b = self.build('// id c\nx\n')
        self.assertIn('<title>Card c</title>', b.html('c.html'))

    def test_title_escaped(self):
        b = self.build('// id c\n// name A & <B>\n// +# s 1\n// +tags t\nx\n')
        self.assertIn('<title>A &amp; &lt;B&gt;</title>', b.html('c.html'))
        self.assertIn('>A &amp; &lt;B&gt;</a>', b.html('series_s.html'))
        self.assertIn('>A &amp; &lt;B&gt;</a>', b.html('tags_t.html'))

    def test_html_title_prefix(self):
        b = self.build('/// +html.title Deck:\n// id c\n// name N\nx\n')
        self.assertIn('<title>Deck: N</title>', b.html('c.html'))

    def test_html_head(self):
        b = self.build('/// +html.head <meta name="a" content="1">\n'
                       '// id c\n// +html.head <meta name="b" content="2">\nx\n')
        head = b.html('c.html').split('</head>')[0]
        self.assertIn('<meta name="a" content="1">', head)
        self.assertIn('<meta name="b" content="2">', head)

    def test_html_style_deck(self):
        b = self.build('/// +html.style p { color: green; }\n// id c\nx\n')
        with open(b.path('style.css')) as fd:
            self.assertIn('p { color: green; }', fd.read())

    def test_html_style_card(self):
        b = self.build('// id c\n// +html.style p { color: blue; }\nx\n')
        self.assertIn('<style>p { color: blue; }</style>', b.html('c.html'))

    def test_html_style_file(self):
        b = self.build('/// html.style.file my.css\n// id c\nx\n')
        self.assertTrue(b.exists('my.css'))
        self.assertIn('href="my.css"', b.html('c.html'))

    def test_html_file_and_anchor(self):
        b = self.build('// id c\n// html.file other.html\n// html.anchor top\nx\n/ html.anchor first\n')
        self.assertTrue(b.exists('other.html'))
        self.assertFalse(b.exists('c.html'))
        html = b.html('other.html')
        self.assertIn('<a name="top"></a>', html)
        self.assertIn('<a name="first"></a>', html)

    def test_brief(self):
        b = self.build('// id c\n// brief Short.\n// +# s 1\nx\n\\\n// id d\n// +# s 2\n' +
                       'Long ' * 40 + '\n')
        html = b.html('series_s.html')
        self.assertIn('Short. ...', html)
        self.assertIn(('Long ' * 16)[:80] + ' ...', html)

    def test_frag_id_show(self):
        b = self.build('/// +frag.id.show +\n// id c\nx\n/ id first\n\ny\n')
        body = b.body('c.html')
        self.assertIn('[c-first]', body)
        self.assertIn('[c-00001]', body)

    def test_frag_id_show_first_value(self):
        b = self.build('/// +frag.id.show +\n// id c\n// +frag.id.show -\nx\n')
        self.assertNotIn('[c-00000]', b.body('c.html'))

    def test_system_attributes(self):
        b = self.build('// id c\nx\n\ny\n')
        c = b.node('c')
        self.assertEqual(c.get('file'), 'a.deck')
        self.assertEqual(c.get('seq', int), 0)
        self.assertTrue(c.has('mtime'))
        self.assertTrue(c.has('ctime'))
        self.assertEqual([f.get('seq', int) for f in c.frags], [0, 1])

    def test_debug(self):
        b = self.build('/// +it (?P<open>_)x(?P<close>_)\n// id c\n_x_ y\n/ DEBUG 3\n/ k v\n')
        self.assertIn('apply: markers-in', b.stdout)
        self.assertIn('out', b.stdout)
        self.assertNotIn('set:', b.stdout)
        b = self.build('// id c\nx\n/ DEBUG 4\n/ k v\n')
        self.assertIn('set:   "k" to "v"', b.stdout)

    def test_debug_not_inherited(self):
        b = self.build('/// DEBUG 3\n/// +it (?P<open>_)x(?P<close>_)\n// id c\n_x_ y\n')
        self.assertNotIn('apply:', b.stdout)

# ----------------------------------------------------------------------
# Identifiers
# ----------------------------------------------------------------------

class TestIdentifiers(CardTest):
    def test_automatic_card_ids(self):
        b = self.build('x\n\\\ny\n\\\n// id named\nz\n', names = ['notes.deck'])
        self.assertEqual([c.id() for c in b.deck.cards[1:]],
                         ['notes.deck-00000', 'notes.deck-00001', 'named'])
        self.assertTrue(b.exists('notes.deck-00001.html'))

    def test_fragment_ids(self):
        b = self.build('// id c\na\n/ id top\n\nb\n\nc\n')
        self.assertEqual([f.id() for f in b.node('c').frags], ['c-top', 'c-00001', 'c-00002'])
        html = b.html('c.html')
        for a in ['c-top', 'c-00001', 'c-00002']:
            self.assertIn('<a name="{}"></a>'.format(a), html)

    def test_note_ids(self):
        b = self.build('// id c\na b\n/ id p\n/ +* "a" One.\n/ +* "b" Two.\n: id two\n')
        notes = b.node('c-p').notes
        self.assertEqual([n.id() for n in notes], ['c-p-note-00000', 'c-p-two'])

    def test_duplicate_card_ids_fail(self):
        self.fails('// id c\nx\n\\\n// id c\ny\n')

    def test_duplicate_fragment_ids_fail(self):
        self.fails('// id c\nx\n/ id f\n\ny\n/ id f\n')

    def test_spaces_in_automatic_ids(self):
        b = self.build('x\n', names = ['my notes.deck'])
        self.assertEqual(b.deck.cards[1].id(), 'my-notes.deck-00000')

# ----------------------------------------------------------------------
# Links
# ----------------------------------------------------------------------

class TestLinks(CardTest):
    def test_span_to_url(self):
        b = self.build('// id c\nSee here and here.\n/ += . "here" -> https://example.com/x\n')
        self.assertEqual(b.frag('c.html', 'c-00000').count('<a href="https://example.com/x">here</a>'), 2)

    def test_span_to_span_same_card(self):
        b = self.build('// id c\nSource text.\n/ id s\n/ += . "Source" -> !c-t "target"\n\n'
                       'The target text.\n/ id t\n')
        self.assertIn('<a href="#c-t">Source</a>', b.frag('c.html', 'c-s'))
        t = b.body('c.html')
        self.assertIn('<span class="linktarget" data-uid=', t)
        self.assertRegex(t, r'<p class="margin-left">.*<a href="#c-s">')

    def test_span_to_span_other_card(self):
        b = self.build('// id c\nSource.\n/ += . "Source" -> !d-t "Target"\n\\\n// id d\nTarget.\n/ id t\n')
        self.assertIn('<a href="d.html#d-t">Source</a>', b.body('c.html'))
        self.assertIn('<a href="c.html#c-00000">', b.body('d.html'))

    def test_link_attached_elsewhere(self):
        b = self.build('// id c\nAlpha.\n/ id a\n\nBeta.\n/ id b\n\nGamma.\n'
                       '/ += !c-a "Alpha" -> !c-b "Beta"\n')
        self.assertIn('<a href="#c-b">Alpha</a>', b.frag('c.html', 'c-a'))

    def test_url_to_span(self):
        b = self.build('// id c\nThis is underlined.\n/ += https://example.com -> . "underlined"\n')
        body = b.body('c.html')
        self.assertIn('class="linktarget"', body)
        self.assertIn('<a href="https://example.com">', body)

    def test_span_to_whole_fragment(self):
        b = self.build('// id c\nGo there.\n/ += . "there" -> !c-t\n\nTarget.\n/ id t\n')
        self.assertIn('<a href="#c-t">there</a>', b.body('c.html'))

    def test_span_to_whole_card(self):
        b = self.build('// id c\nGo there.\n/ += . "there" -> !d\n\\\n// id d\nTarget.\n')
        self.assertIn('<a href="d.html#d">there</a>', b.body('c.html'))

    def test_whole_node_source_ignored(self):
        b = self.build('// id c\nText.\n/ += . -> https://example.com\n')
        self.assertNotIn('example.com', b.body('c.html'))

    def test_unknown_identifier_fails(self):
        self.fails('// id c\nText.\n/ += . "Text" -> !nowhere\n')

    def test_identifier_with_space_after_bang(self):
        b = self.build('// id c\nText.\n/ += . "Text" -> ! c-t\n\nT.\n/ id t\n')
        self.assertIn('<a href="#c-t">Text</a>', b.body('c.html'))

    def test_named_groups(self):
        b = self.build('/// += . "fn:(?P<name>[a-z]+)" -> https://example.com/{name}\n'
                       '// id c\nCall fn:foo and fn:bar.\n')
        body = b.body('c.html')
        self.assertIn('<a href="https://example.com/foo">fn:foo</a>', body)
        self.assertIn('<a href="https://example.com/bar">fn:bar</a>', body)

    def test_doubled_braces(self):
        b = self.build('// id c\nText.\n/ += . "Text" -> https://example.com/{{x}}\n')
        self.assertIn('href="https://example.com/{x}"', b.body('c.html'))

    def test_single_braces_fail(self):
        self.fails('// id c\nText.\n/ += . "Text" -> https://example.com/{x}\n')
        self.fails('// id c\nText.\n/ += . "Text" -> https://example.com/{}\n')

    def test_automatic_links(self):
        b = self.build('/// += . "deckword" -> https://example.com/d\n'
                       '// id c\n// += . "cardword" -> https://example.com/c\n'
                       'deckword cardword\n\\\n// id d\ndeckword cardword\n')
        self.assertIn('href="https://example.com/d"', b.body('c.html'))
        self.assertIn('href="https://example.com/c"', b.body('c.html'))
        self.assertIn('href="https://example.com/d"', b.body('d.html'))
        self.assertNotIn('href="https://example.com/c"', b.body('d.html'))

    def test_lookahead(self):
        b = self.build('// id c\nEV: EV and EV.\n/ += . "EV(?=:)" -> https://example.com\n')
        self.assertEqual(b.body('c.html').count('https://example.com'), 1)

    def test_whitespace_across_lines(self):
        b = self.build('// id c\nTwo\n   words.\n/ += . "Two\\s+words" -> https://example.com\n')
        self.assertIn('https://example.com', b.body('c.html'))

    def test_link_in_code(self):
        b = self.build('// id c\n; code\ncall(x)\n/ += . "call" -> https://example.com\n')
        self.assertIn('<code><a href="https://example.com">call</a>(x)</code>', b.body('c.html'))

    def test_link_matched_before_styles(self):
        b = self.build(STYLES + '// id c\nSee _word_ here.\n/ += . "_word_" -> https://example.com\n')
        self.assertIn('<a href="https://example.com"><i>word</i></a>', b.body('c.html'))

    def test_link_regex_matching_empty_string_terminates(self):
        b = self.build('// id c\nabc\n/ += . "z*" -> https://example.com\n')
        self.assertIn('<p>abc</p>', b.body('c.html'))

    def test_style_regex_matching_empty_string_terminates(self):
        b = self.build('/// +it (?P<open>)z*(?P<close>)\n// id c\nabc z\n')
        self.assertIn('<p>abc <i>z</i></p>', b.body('c.html'))

# ----------------------------------------------------------------------
# Notes
# ----------------------------------------------------------------------

class TestNotes(CardTest):
    def test_note(self):
        b = self.build('// id c\nOne thing.\n/ +* "thing" A note.\n')
        body = b.body('c.html')
        self.assertRegex(body, r'<p class="margin-right"><a name="c-00000-note-00000"></a>'
                         r'<span class="annotation" data-uid="\d">A note\.</span>')
        self.assertRegex(body, r'One <span class="annotation" data-uid="\d">thing</span>\.')

    def test_note_on_code(self):
        b = self.build('// id c\n; code\nx = 1\n/ +* "x" The variable.\n')
        self.assertIn('The variable.', b.body('c.html'))

    def test_note_styles(self):
        b = self.build(STYLES + '// id c\nOne thing.\n/ +* "thing" A _styled_ note.\n')
        self.assertIn('A <i>styled</i> note.', b.body('c.html'))

    def test_note_colours(self):
        b = self.build('// id c\na b c d e f g\n' +
                       ''.join('/ +* "{}" N{}.\n'.format(x, x) for x in 'abcdefg'))
        uids = re.findall(r'<span class="annotation" data-uid="(\d)">N', b.body('c.html'))
        self.assertEqual(len(uids), 7)
        self.assertEqual(len(set(uids[:6])), 6)

    def test_note_regex_quote_required(self):
        self.fails('// id c\nx\n/ +* x Note.\n')

    def test_link_in_note(self):
        b = self.build('// id c\nOne thing.\n/ +* "thing" See example.\n'
                       ': += . "example" -> https://example.com\n')
        self.assertIn('See <a href="https://example.com">example</a>.', b.body('c.html'))

    def test_link_from_note_to_fragment(self):
        b = self.build('// id c\nOne thing.\n/ +* "thing" See target.\n'
                       ': += . "target" -> !c-t "Target"\n\nTarget.\n/ id t\n')
        self.assertIn('See <a href="#c-t">target</a>.', b.body('c.html'))

    def test_link_to_note_set_elsewhere(self):
        b = self.build('// id c\nOne thing.\n/ id p\n/ +* "thing" See example.\n: id n\n\n'
                       'Other.\n/ += !c-p-n "example" -> https://example.com\n')
        self.assertIn('See <a href="https://example.com">example</a>.', b.body('c.html'))

    def test_automatic_links_in_notes(self):
        b = self.build('/// += . "example" -> https://example.com\n'
                       '// id c\nOne thing.\n/ +* "thing" See example.\n')
        self.assertIn('See <a href="https://example.com">example</a>.', b.body('c.html'))

    def test_fragment_links_not_applied_to_notes(self):
        b = self.build('// id c\nOne thing here.\n/ += . "here" -> https://example.com\n'
                       '/ +* "thing" Not here.\n')
        self.assertIn('Not here.', b.body('c.html'))
        self.assertEqual(b.body('c.html').count('https://example.com'), 1)

    def test_link_to_span_in_note(self):
        b = self.build('// id c\nSource.\n/ += . "Source" -> !c-p-n "target"\n\n'
                       'One thing.\n/ id p\n/ +* "thing" The target.\n: id n\n')
        self.assertIn('<a href="#c-p-n">Source</a>', b.body('c.html'))

    def test_link_to_whole_note(self):
        b = self.build('// id c\nSource.\n/ += . "Source" -> !c-p-n\n\n'
                       'One thing.\n/ id p\n/ +* "thing" The note.\n: id n\n')
        self.assertIn('<a href="#c-p-n">Source</a>', b.body('c.html'))

    def test_back_link_to_note(self):
        b = self.build('// id c\nOne thing.\n/ +* "thing" See target.\n: id n\n'
                       ': += . "target" -> !c-t "Target"\n\nTarget.\n/ id t\n')
        self.assertIn('<a href="#c-00000-n">', b.body('c.html'))

# ----------------------------------------------------------------------
# Styles
# ----------------------------------------------------------------------

class TestStyles(CardTest):
    def test_styles(self):
        b = self.build(STYLES + '// id c\n_it_ *em* +bd+ -un- `cd` x\n')
        self.assertIn('<i>it</i> <em>em</em> <b>bd</b> <u>un</u> <code class="inline">cd</code> x',
                      b.body('c.html'))

    def test_alignment(self):
        b = self.build(STYLES + '// id c\n<<left<< >>right>>\n')
        body = b.body('c.html')
        self.assertIn('<span class="align-left">left</span>', body)
        self.assertIn('<span class="align-right">right</span>', body)

    def test_empty_groups(self):
        b = self.build('/// +cd (?P<open>)[a-z]+\\(\\)(?P<close>)\n// id c\nCall f() now.\n')
        self.assertIn('Call <code class="inline">f()</code> now.', b.body('c.html'))

    def test_inline_math_loads_mathjax(self):
        b = self.build(STYLES + '// id c\nFormula \\(x+1\\).\n')
        self.assertIn('mathjax', b.html('c.html'))
        self.assertIn('\\(x+1\\)', b.body('c.html'))

    def test_styles_not_in_code_head_math_img(self):
        b = self.build(STYLES + '// id c\n; code\n_a_\n\n; head\n_b_\n\n; math\n_c_\n\n'
                       '; img\n/ url https://example.com/p.png\n_d_\n')
        body = b.body('c.html')
        for x in 'abcd':
            self.assertIn('_{}_'.format(x), body)
        self.assertNotIn('<i>', body)

    def test_style_levels(self):
        b = self.build('// id c\n// +it (?P<open>_)[^_]*(?P<close>_)\n_a_\n\n'
                       '%b%\n/ +em (?P<open>%)[^%]*(?P<close>%)\n\\\n// id d\n_a_\n')
        self.assertIn('<i>a</i>', b.body('c.html'))
        self.assertIn('<em>b</em>', b.body('c.html'))
        self.assertIn('_a_', b.body('d.html'))

    def test_order_same_markers(self):
        b = self.build('/// +bd (?P<open>\\*)[^*]*(?P<close>\\*)\n'
                       '/// +em (?P<open>\\*)[^*]*(?P<close>\\*)\n// id c\n*x*\n')
        self.assertIn('<em>x</em>', b.body('c.html'))
        self.assertNotIn('<b>', b.body('c.html'))

    def test_style_without_groups_fails(self):
        self.fails('/// +it _[^_]*_\n// id c\n_x_\n')

# ----------------------------------------------------------------------
# Series and tags
# ----------------------------------------------------------------------

SERIES = ('// id c1\n// name One\n// +# s 1\n// +tags t\nFirst.\n\\\n'
          '// id c2\n// name Two\n// +# s 2\n// +tags t\n// +tags u\nSecond.\n\\\n'
          '// id c3\n// name Three\n// +# s 3\nThird.\n')

class TestSeries(CardTest):
    def test_navigation(self):
        b = self.build(SERIES)
        self.assertIn('<footer>&lt;&nbsp;<a href="series_s.html">s</a>&nbsp;<a href="c2.html#c2">&gt;</a>',
                      b.body('c1.html'))
        self.assertIn('<a href="c1.html#c1">&lt;</a>&nbsp;<a href="series_s.html">s</a>&nbsp;'
                      '<a href="c3.html#c3">&gt;</a>', b.body('c2.html'))
        self.assertIn('<a href="series_s.html">s</a>&nbsp;&gt;&nbsp;', b.body('c3.html'))

    def test_index_variants(self):
        b = self.build(SERIES)
        for compact in ['', '_compact']:
            for reverse in ['', '_reverse']:
                for flat in ['', '_flat']:
                    self.assertTrue(b.exists('series_s{}{}{}.html'.format(compact, reverse, flat)))

    def test_index_order(self):
        b = self.build(SERIES)
        h = b.html('series_s.html')
        self.assertLess(h.index('>One<'), h.index('>Two<'))
        self.assertLess(h.index('>Two<'), h.index('>Three<'))
        h = b.html('series_s_reverse.html')
        self.assertGreater(h.index('>One<'), h.index('>Two<'))

    def test_flat_index_contains_text(self):
        b = self.build(SERIES)
        h = b.html('series_s_flat.html')
        for t in ['First.', 'Second.', 'Third.']:
            self.assertIn(t, h)

    def test_positions_compared_as_strings(self):
        b = self.build('// id a\n// +# s 10\nx\n\\\n// id b\n// +# s 9\ny\n')
        h = b.html('series_s.html')
        self.assertLess(h.index('>10<'), h.index('>9<'))

    def test_default_position(self):
        b = self.build('// id zz\n// +# s\nx\n\\\n// id aa\n// +# s\ny\n')
        h = b.html('series_s.html')
        self.assertLess(h.index('aa.html'), h.index('zz.html'))

    def test_duplicate_position_fails(self):
        self.fails('// id a\n// +# s 1\nx\n\\\n// id b\n// +# s 1\ny\n')

    def test_twice_in_series_fails(self):
        self.fails('// id a\n// +# s 1\n// +# s 2\nx\n')

    def test_several_series(self):
        b = self.build('// id a\n// +# s 1\n// +# r 1\nx\n')
        self.assertIn('series_s.html', b.body('a.html'))
        self.assertIn('series_r.html', b.body('a.html'))

    def test_series_style(self):
        b = self.build('/// html.series_compact +\n/// html.series_reverse +\n' + SERIES)
        self.assertIn('href="series_s_compact_reverse.html"', b.body('c1.html'))

    def test_tags(self):
        b = self.build(SERIES)
        h = b.html('tags_t.html')
        self.assertIn('>One<', h)
        self.assertIn('>Two<', h)
        self.assertNotIn('>Three<', h)
        self.assertTrue(b.exists('tags_u.html'))

    def test_tags_on_fragments_fail(self):
        self.fails('// id c\nx\n/ +tags t\n')

    def test_feed(self):
        b = self.build('// id a\n// name A\n// +# timeline 2025-04-01 12:30\nx\n\\\n'
                       '// id b\n// +# timeline 2025-05-01\ny\n\\\n// id c\nz\n')
        root = ET.parse(b.path('feed.xml')).getroot()
        self.assertEqual(root.find('channel/title').text, 'Test deck')
        items = root.findall('channel/item')
        self.assertEqual([i.find('pubDate').text for i in items], ['2025-04-01', '2025-05-01'])
        self.assertEqual(items[0].find('link').text, 'https://example.com/deck/a.html#a')

    def test_timeline_without_url_fails(self):
        self.fails('/// dir out\n// id a\n// +# timeline 2025-04-01\nx\n', conf = None)

    def test_no_url(self):
        b = self.build('/// dir out\n// id a\nx\n', conf = None)
        self.assertNotIn('None', b.html('a.html'))

# ----------------------------------------------------------------------
# Mathematics
# ----------------------------------------------------------------------

class TestMath(CardTest):
    def test_tex_definitions(self):
        b = self.build('/// +TeX \\( \\def\\foo{F} \\)\n// id c\n// +TeX \\( \\def\\bar{B} \\)\n'
                       '; math\n\\foo\\bar\n\\\n// id d\nNo maths.\n')
        c = b.html('c.html')
        self.assertIn('\\def\\foo{F}', c)
        self.assertIn('\\def\\bar{B}', c)
        self.assertNotIn('\\def', b.html('d.html'))

    def test_math_escaped(self):
        b = self.build('// id c\n; math\na < b\n')
        self.assertIn('$$a &lt; b', b.html('c.html'))

# ----------------------------------------------------------------------
# HTML output
# ----------------------------------------------------------------------

class TestOutput(CardTest):
    def test_default_dir(self):
        b = self.build('// id c\nx\n', conf = None)
        self.assertTrue(os.path.exists(os.path.join(b.tmp, 'html_out', 'c.html')))

    def test_current_dir(self):
        b = self.build('/// dir .\n/// deck.url https://example.com\n// id c\nx\n', conf = None)
        self.assertTrue(os.path.exists(os.path.join(b.tmp, 'c.html')))

    def test_nested_dir(self):
        b = self.build('/// dir a/b\n/// deck.url https://example.com\n// id c\nx\n', conf = None)
        self.assertTrue(os.path.exists(os.path.join(b.tmp, 'a', 'b', 'c.html')))

    def test_working_directory_restored(self):
        tmp = tempfile.mkdtemp(prefix = 'card-test-')
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, 'a.deck'), 'w') as fd:
            fd.write('/// dir a/b\n// id c\nx\n')
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            d = cardtool.deck()
            d.cardread('a.deck')
            d.crossref()
            d.build()
            self.assertEqual(os.getcwd(), os.path.realpath(tmp))
        finally:
            os.chdir(cwd)

    def test_files(self):
        b = self.build(SERIES)
        for f in ['c1.html', 'c2.html', 'c3.html', 'style.css', 'feed.xml',
                  'series_s.html', 'tags_t.html', 'tags_u.html']:
            self.assertTrue(b.exists(f), f)

    def test_feed_reference(self):
        b = self.build('// id c\nx\n')
        self.assertIn('href="https://example.com/deck/feed.xml"', b.html('c.html'))

    def test_card_structure(self):
        b = self.build('// id c\n// name N\nx\n')
        self.assertRegex(b.body('c.html'), r'^<a name="c"></a><div class="column"><header>N</header>.*'
                         r'<footer></footer></div></body></html>$')

    def test_everything_valid(self):
        """A deck using every feature produces valid HTML."""
        self.build(STYLES +
                   '/// += . "auto" -> https://example.com/auto\n'
                   '/// +TeX \\( \\def\\foo{F} \\)\n'
                   '/// +frag.id.show +\n',
                   SERIES + '\\\n// id all\n// name All & everything\n// +# s 4\n// +tags t\n'
                   '; head\nHeading\n\n'
                   '_it_ *em* +bd+ -un- `cd` \\(x\\) auto <<l<<\n/ id p\n'
                   '/ += . "em" -> !all-code "x"\n/ += https://example.com -> . "bd"\n'
                   '/ +* "it" A note with auto and a link.\n: id n\n'
                   ': += . "link" -> https://example.com/n\n\n'
                   '; code\n>x = 1\n/ id code\n/ +* "x" Variable.\n\n'
                   '; math\n\\foo\n\n'
                   '; img\n/ url https://example.com/p.png\nCaption.\n\n'
                   'Back to the note.\n/ += . "note" -> !all-p-n\n')

# ----------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------

class TestCommandLine(CardTest):
    def test_verbose(self):
        (p, tmp) = self.cli(['-v', 'a.deck'], { 'a.deck': CONF + '// id c\n; head\nH\n\n; code\nx\n\nText.\n/ +* "Text" N.\n' })
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('HEAD_BEGIN', p.stdout)
        self.assertIn('CODE_BEGIN', p.stdout)
        self.assertIn('PARA_BEGIN', p.stdout)

    def test_error_exit(self):
        (p, tmp) = self.cli(['a.deck'], { 'a.deck': '// id c\n/ k v\n' })
        self.assertNotEqual(p.returncode, 0)

    def test_manual(self):
        """The manual builds and its output is valid."""
        tmp = tempfile.mkdtemp(prefix = 'card-test-')
        self.addCleanup(shutil.rmtree, tmp, True)
        for f in ['self.conf', 'self.deck']:
            shutil.copy(os.path.join(ROOT, f), tmp)
        p = subprocess.run([sys.executable, CARD, 'self.conf', 'self.deck'], cwd = tmp,
                           capture_output = True, text = True, timeout = 120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(cardtool.check_html_dir(os.path.join(tmp, 'docs')), [])

# ----------------------------------------------------------------------
# Consistency checks
# ----------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        code = { '/ok' : 200, '/gone' : 404, '/busy' : 503, '/nohead' : 200 }.get(self.path, 404)
        if self.path == '/nohead' and self.command == 'HEAD':
            code = 405
        self.send_response(code)
        self.end_headers()
    def do_GET(self):
        self.do_HEAD()
    def log_message(self, *args):
        pass

class TestCheck(CardTest):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.url = 'http://127.0.0.1:{}'.format(cls.server.server_address[1])
        threading.Thread(target = cls.server.serve_forever, daemon = True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def check(self, *sources, offline = True, tex = None, files = None):
        """Runs check_deck() on the sources. Returns the list of problems as
        (file, line, kind, message)."""
        tmp = tempfile.mkdtemp(prefix = 'card-test-')
        self.addCleanup(shutil.rmtree, tmp, True)
        names = ['{}.deck'.format(chr(ord('a') + i)) for i in range(len(sources))]
        for (name, src) in zip(names, sources):
            with open(os.path.join(tmp, name), 'w', encoding = 'utf-8') as fd:
                fd.write(src)
        for (name, content) in (files or {}).items():
            path = os.path.join(tmp, name)
            os.makedirs(os.path.dirname(path), exist_ok = True)
            with open(path, 'w') as fd:
                fd.write(content)
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                p = cardtool.check_deck(names, offline = offline, tex = tex)
        finally:
            os.chdir(cwd)
        return [(w[0] if w else None, w[1] if w else None, k, m) for (w, k, m) in p.list]

    def assertProblem(self, problems, line, kind, text):
        for (f, l, k, m) in problems:
            if l == line and k == kind and text in m:
                return
        self.fail('no {} "{}" at line {} in {}'.format(kind, text, line, problems))

    def test_clean(self):
        self.assertEqual(self.check('/// dir out\n// id c\n// +# s 01\nSee here.\n'
                                    '/ += . "here" -> !c-t "Target"\n/ +* "See" A note.\n\n'
                                    'Target.\n/ id t\n'), [])

    def test_syntax_error_location(self):
        self.assertEqual(self.check('// id c\nx\n\n/ k v\n'),
                         [('a.deck', 4, 'error', 'fragment attribute outside of a fragment.')])
        self.assertProblem(self.check('// id c\n; tabel\nx\n'), 2, 'error', 'unknown fragment type')
        self.assertProblem(self.check('// id c\nx\n/ id\n'), 3, 'error', 'attribute without a value')
        self.assertProblem(self.check('// id c\nx\n: id n\n'), 3, 'error', 'note attribute')
        self.assertProblem(self.check('// id c\nx\n/ +* x Note.\n'), 3, 'error', 'quoted')

    def test_build_error_location(self):
        with self.assertRaises(cardtool.CardError) as e:
            self.build('// id c\nx\n\n/ k v\n')
        self.assertEqual(str(e.exception), 'a.deck:4: fragment attribute outside of a fragment.')

    def test_unknown_identifier(self):
        self.assertProblem(self.check('// id c\nx\n/ += . "x" -> !nope\n'), 3, 'error',
                           'unknown identifier "nope"')

    def test_duplicate_identifier(self):
        self.assertProblem(self.check('// id c\nx\n/ id f\n\ny\n/ id f\n'), 6, 'error',
                           'duplicate identifier "c-f"')

    def test_duplicate_position(self):
        self.assertProblem(self.check('// id a\n// +# s 1\nx\n\\\n// id b\n// +# s 1\ny\n'),
                           6, 'error', 'position "1" in series "s" is already taken by a')

    def test_tags_on_fragment(self):
        self.assertProblem(self.check('// id c\nx\n/ +tags t\n'), 3, 'error', 'only cards')

    def test_bad_regex(self):
        self.assertProblem(self.check('// id c\nx\n/ += . "(" -> https://example.com\n'),
                           3, 'error', 'invalid regular expression "("')
        self.assertProblem(self.check('/// +it _[^_]*_\n// id c\n_x_\n'), 1, 'error',
                           'no group(s) named open, close')

    def test_url_braces(self):
        self.assertProblem(self.check('// id c\nx\n/ += . "x" -> https://example.com/{y}\n'),
                           3, 'error', 'literal braces must be doubled')

    def test_unmatched_link(self):
        p = self.check('/// += . "nowhere" -> https://example.com\n// id c\nText.\n'
                       '/ += . "absent" -> https://example.com\n\nOther.\n/ id o\n'
                       '/ += !c-00000 "Text" -> . "missing"\n')
        self.assertProblem(p, 1, 'error', 'regular expression "nowhere" matches nothing')
        self.assertProblem(p, 4, 'error', 'regular expression "absent" matches nothing')
        self.assertProblem(p, 8, 'error', 'regular expression "missing" matches nothing')
        self.assertEqual(len(p), 3)

    def test_unmatched_link_in_other_node(self):
        p = self.check('// id c\nText.\n/ id t\n\nOther.\n/ += !c-t "absent" -> https://example.com\n')
        self.assertProblem(p, 6, 'error', 'matches nothing in c-t')

    def test_automatic_link_used_somewhere(self):
        self.assertEqual(self.check('/// += . "word" -> https://example.com\n'
                                    '// id c\nNo.\n\\\n// id d\nA word.\n'), [])

    def test_unmatched_note(self):
        self.assertProblem(self.check('// id c\nText.\n/ +* "absent" A note.\n'), 3, 'error',
                           'note regular expression "absent" matches nothing')

    def test_list_attribute_without_plus(self):
        p = self.check('// id c\n// tags foo\nx\n/ html.style color: red;\n')
        self.assertProblem(p, 2, 'warning', '"tags" is set without "+"')
        self.assertProblem(p, 4, 'warning', '"html.style" is set without "+"')

    def test_explicit_link_on_card(self):
        self.assertProblem(self.check('// id c\n// += !c-t "x" -> https://example.com\nx\n/ id t\n'),
                           2, 'warning', 'instantiated once for every fragment')

    def test_identifier_with_spaces(self):
        self.assertProblem(self.check('// id my card\nx\n'), 1, 'warning', 'contains spaces')

    def test_same_output_file(self):
        self.assertProblem(self.check('// id a\n// html.file x.html\nx\n\\\n'
                                      '// id b\n// html.file x.html\ny\n'),
                           4, 'warning', 'overwriting card a')

    def test_series_positions(self):
        p = self.check('// id a\n// +# s 9\nx\n\\\n// id b\n// +# s 10\ny\n')
        self.assertProblem(p, 2, 'warning', 'pad them with zeroes')

    def test_timeline_positions(self):
        p = self.check('/// deck.url https://example.com\n// id a\n// +# timeline April\nx\n')
        self.assertProblem(p, 3, 'warning', 'does not start with a date')

    def test_images(self):
        p = self.check('/// dir out\n// id c\n; img\n/ url pics/here.png\n\n'
                       '; img\n/ url pics/missing.png\n', files = { 'out/pics/here.png' : '' })
        self.assertEqual(len(p), 1)
        self.assertProblem(p, 7, 'error', '"pics/missing.png" does not exist')

    def test_relative_link(self):
        p = self.check('/// dir out\n// id c\nOne two.\n/ += . "One" -> other.html\n'
                       '/ += . "two" -> c.html#c\n')
        self.assertEqual(len(p), 1)
        self.assertProblem(p, 4, 'error', '"other.html" does not exist')

    def test_urls(self):
        u = self.url
        p = self.check('// id c\na b c d\n/ += . "a" -> {0}/ok\n/ += . "b" -> {0}/gone\n'
                       '/ += . "c" -> {0}/busy\n/ += . "d" -> {0}/nohead#x\n'.format(u),
                       offline = False)
        self.assertProblem(p, 4, 'error', '/gone: HTTP 404')
        self.assertProblem(p, 5, 'warning', '/busy: HTTP 503')
        self.assertEqual(len(p), 2)

    def test_url_of_automatic_link(self):
        p = self.check('/// += . "(?P<n>gone)" -> {}/{{n}}\n// id c\nIt is gone.\n'.format(self.url),
                       offline = False)
        self.assertProblem(p, 3, 'error', '/gone: HTTP 404')

    def test_offline(self):
        self.assertEqual(self.check('// id c\na\n/ += . "a" -> {}/gone\n'.format(self.url)), [])

    def test_image_url(self):
        p = self.check('// id c\n; img\n/ url {}/gone\n'.format(self.url), offline = False)
        self.assertProblem(p, 3, 'error', 'HTTP 404')

    def test_formula_structure(self):
        p = self.check('// id c\n; math\n\\frac{a}{b\n\nInline \\(\\left( x\\) and \\(\\end{x}\\).\n')
        self.assertProblem(p, 2, 'error', 'unbalanced "{"')
        self.assertProblem(p, 5, 'error', 'unbalanced \\left and \\right')
        self.assertProblem(p, 5, 'error', 'unexpected \\end{x}')

    @unittest.skipUnless(shutil.which('pdflatex'), 'pdflatex is not installed')
    def test_latex(self):
        p = self.check('/// +TeX \\( \\def\\foo{F} \\)\n// id c\n'
                       '// +TeX \\( \\newcommand{\\bar}{B} \\)\n'
                       '; math\n\\foo + \\undefined\n\n'
                       'Fine \\(\\foo x\\), bad \\(\\frac{a}\\).\n/ +* "Fine" Note \\(\\nope\\).\n\n'
                       '; math\n\\begin{CD} A @>>> B \\end{CD}\n',
                       tex = shutil.which('pdflatex'))
        self.assertProblem(p, 3, 'error', 'TeX definitions')            # \bar exists.
        self.assertProblem(p, 4, 'error', 'formula "\\foo + \\undefined": Undefined control sequence')
        self.assertProblem(p, 7, 'error', 'formula "\\frac{a}"')
        self.assertProblem(p, 8, 'error', 'formula "\\nope": Undefined control sequence')
        self.assertFalse([x for x in p if '\\foo x' in x[3] or 'CD' in x[3]], p)

    def test_manual(self):
        p = self.check(open(os.path.join(ROOT, 'self.conf')).read(),
                       open(os.path.join(ROOT, 'self.deck')).read(),
                       tex = shutil.which('pdflatex'))
        self.assertEqual([x for x in p if x[2] == 'error'], [])

    def test_command_line(self):
        (p, tmp) = self.cli(['--check', '--offline', 'a.deck'], { 'a.deck' : '// id c\nx\n/ +* "y" N.\n' })
        self.assertEqual(p.returncode, 1)
        self.assertIn('a.deck:3: error: note regular expression "y" matches nothing.', p.stdout)
        self.assertIn('1 error(s), 0 warning(s).', p.stdout)
        (p, tmp) = self.cli(['-c', '--offline', 'a.deck'], { 'a.deck' : '// id c\nx\n' })
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertFalse(os.path.exists(os.path.join(tmp, 'html_out')))

    def test_command_line_error(self):
        (p, tmp) = self.cli(['a.deck'], { 'a.deck' : '// id c\n/ k v\n' })
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, 'a.deck:2: fragment attribute outside of a fragment.\n')

if __name__ == '__main__':
    unittest.main()
