#!/usr/bin/env python3
"""Add BUILD markers to the template for the PR #2 build pipeline.

One-shot migration: the markers it inserts are already present in the template,
so a second run exits on the sanity check below. Kept for the record.
"""
import sys
from pathlib import Path

# Resolved from this file rather than hardcoded to one machine's absolute path.
HTML_PATH = Path(__file__).resolve().parent.parent / "src" / "template.html"

content = HTML_PATH.read_text(encoding="utf-8")

# Sanity: markers must not already exist
for marker in ("<!--BUILD:JSONLD_START-->", "<!--BUILD:JSONLD_END-->",
               "<!--BUILD:FAQ_HTML_START-->", "<!--BUILD:FAQ_HTML_END-->"):
    if marker in content:
        print(f"FATAL: marker {marker!r} already present in template. Aborting.", file=sys.stderr)
        sys.exit(1)

# Insert JSON-LD markers right before </style></head>
# We look for the exact closing of head.
old_head_close = "</style></head><body>"
new_head_close = "</style>\n<!--BUILD:JSONLD_START-->\n<!--BUILD:JSONLD_END-->\n</head><body>"
if old_head_close not in content:
    print("FATAL: could not find '</style></head><body>' in template.", file=sys.stderr)
    sys.exit(1)
content = content.replace(old_head_close, new_head_close, 1)

# Wrap the existing <div class="faq">...</div> block with FAQ HTML markers.
# We place the markers as siblings (right before <div and right after </div>)
# so they are visible in the source for review and the build can rewrite the
# inner div content deterministically.
old_faq_open = '<div class="faq">'
idx_open = content.find(old_faq_open)
if idx_open < 0:
    print("FATAL: could not find '<div class=\"faq\">' in template.", file=sys.stderr)
    sys.exit(1)
# Find the matching </div> for the <div class="faq"> block. The block is shallow
# (contains only <details>...</details> children, no nested divs).
idx_close = content.find('</div>', idx_open)
if idx_close < 0:
    print("FATAL: could not find closing </div> for <div class='faq'>.", file=sys.stderr)
    sys.exit(1)
# Sanity: ensure no other </div> appears between idx_open and idx_close
# (the FAQ block is flat; if any other </div> is in between, fail).
between = content[idx_open:idx_close]
if '</div>' in between:
    # If the FAQ block has nested <div>s, that would break our flatten. Check
    # for the exact case: between idx_open and idx_close (exclusive), is there
    # any '</div>' other than at the very end? We use IndexOf only once, so
    # this is a real check.
    print("FATAL: nested </div> found inside <div class='faq'>; cannot wrap safely.", file=sys.stderr)
    sys.exit(1)
faq_region = content[idx_open:idx_close + len('</div>')]
new_faq_region = "<!--BUILD:FAQ_HTML_START-->\n" + faq_region + "\n<!--BUILD:FAQ_HTML_END-->"
content = content[:idx_open] + new_faq_region + content[idx_close + len('</div>'):]

HTML_PATH.write_text(content, encoding="utf-8")
print("OK: BUILD markers added to template.")
print("    - JSONLD markers added before </style></head>")
print("    - FAQ_HTML markers added around <div class='faq'>...</div>")
