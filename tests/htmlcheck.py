"""Structural checks of the HTML produced by card.

This is not a full HTML5 validator. It checks the properties that card's
output is expected to have: every element is properly closed and nested, no
block elements inside paragraphs, no nested hyperlinks, unique anchors, tables
with a consistent number of columns and internal hyperlinks that lead to
existing files and anchors.

"""

import html.parser
import os
import re
import urllib.parse

VOID = { 'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
         'meta', 'source', 'track', 'wbr' }

BLOCK = { 'address', 'article', 'aside', 'blockquote', 'center', 'details',
          'dialog', 'div', 'dl', 'fieldset', 'figcaption', 'figure', 'footer',
          'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'main',
          'nav', 'ol', 'p', 'pre', 'section', 'table', 'ul' }

class Checker(html.parser.HTMLParser):
    def __init__(self, name):
        super().__init__(convert_charrefs = True)
        self.name    = name
        self.errors  = []
        self.stack   = []
        self.anchors = set()
        self.hrefs   = []
        self.doctype = False
        self.title   = None
        self.tables  = []  # Stack of [rows], each row is a cell count.
        self.text    = []
    def error(self, msg):
        self.errors.append('{}:{}: {}'.format(self.name, self.getpos()[0], msg))
    def handle_decl(self, decl):
        if decl.lower() == 'doctype html':
            self.doctype = True
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for (k, v) in attrs:
            if v is not None and 'None' in v.split('/'):
                self.error('attribute {}="{}" in <{}>'.format(k, v, tag))
        if tag in BLOCK and 'p' in self.stack:
            self.error('block element <{}> inside <p>'.format(tag))
        if tag == 'a' and 'a' in self.stack:
            self.error('nested <a>')
        for k in ('name', 'id'):
            if k in a and a[k] != '' and (tag == 'a' or k == 'id'):
                if a[k] in self.anchors:
                    self.error('duplicate anchor "{}"'.format(a[k]))
                self.anchors.add(a[k])
        if tag == 'a' and 'href' in a:
            self.hrefs.append((self.getpos()[0], a['href']))
        if tag == 'link' and 'href' in a:
            self.hrefs.append((self.getpos()[0], a['href']))
        if tag == 'img' and 'src' in a:
            self.hrefs.append((self.getpos()[0], a['src']))
        if tag == 'table':
            self.tables.append([])
        elif tag == 'tr' and self.tables:
            self.tables[-1].append(0)
        elif tag in ('td', 'th') and self.tables and self.tables[-1]:
            self.tables[-1][-1] += 1
        if tag not in VOID:
            self.stack.append(tag)
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()
    def handle_endtag(self, tag):
        if tag in VOID:
            self.error('end tag for void element <{}>'.format(tag))
            return
        if not self.stack:
            self.error('stray </{}>'.format(tag))
            return
        if self.stack[-1] != tag:
            self.error('</{}> closes <{}>'.format(tag, self.stack[-1]))
            if tag in self.stack:
                while self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()
        if tag == 'table':
            rows = self.tables.pop()
            if len(set(rows)) > 1:
                self.error('table rows have different numbers of cells: {}'.format(rows))
    def handle_data(self, data):
        if self.stack and self.stack[-1] == 'title':
            self.title = (self.title or '') + data
        self.text.append(data)
    def close(self):
        super().close()
        if self.stack:
            self.error('unclosed elements: {}'.format(self.stack))
        if not self.doctype:
            self.error('missing <!DOCTYPE html>')
        if self.title is None:
            self.error('missing <title>')

def parse(path, name = None):
    c = Checker(name or os.path.basename(path))
    with open(path, encoding = 'utf-8') as fd:
        c.feed(fd.read())
    c.close()
    return c

def check_dir(d, external = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')):
    """Checks all HTML files in directory d. Returns the list of errors."""
    files  = {}
    errors = []
    for name in sorted(os.listdir(d)):
        if name.endswith('.html'):
            files[name] = parse(os.path.join(d, name), name)
            errors += files[name].errors
    for (name, c) in files.items():
        for (line, href) in c.hrefs:
            if external.match(href) or href.startswith('//'):
                continue
            (path, _, anchor) = href.partition('#')
            path = urllib.parse.unquote(path)
            target = name if path == '' else path
            where = '{}:{}: '.format(name, line)
            if not os.path.exists(os.path.join(d, target)):
                errors.append(where + 'link to a missing file "{}"'.format(href))
            elif anchor != '' and target in files and \
                 urllib.parse.unquote(anchor) not in files[target].anchors:
                errors.append(where + 'link to a missing anchor "{}"'.format(href))
    return errors
