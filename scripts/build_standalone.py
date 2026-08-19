"""
NetGravity — Standalone Single-File Bundle Builder
==================================================
Bundles frontend HTML, CSS, datasets, and scripts into a self-contained portable HTML file.
"""

import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
FRONTEND_DIR = os.path.join(REPO_ROOT, "app", "frontend")

html_path = os.path.join(FRONTEND_DIR, "index.html")
css_path = os.path.join(FRONTEND_DIR, "css", "style.css")
js_files = ["data.js", "map.js", "charts.js", "scenarios.js", "agent.js", "app.js"]

with open(html_path, "r", encoding="utf-8") as f:
    html = f.read()

with open(css_path, "r", encoding="utf-8") as f:
    css = f.read()

js_combined = []
for jf in js_files:
    p = os.path.join(FRONTEND_DIR, "js", jf)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            code = f.read()
            # Clean multiline import statements
            code = re.sub(r'import\s+[\s\S]*?from\s+[\'"][^\'"]+[\'"];?', '', code)
            code = re.sub(r'import\s+[\'"][^\'"]+[\'"];?', '', code)
            
            # Clean export statements
            code = re.sub(r'\bexport\s+const\s+', 'const ', code)
            code = re.sub(r'\bexport\s+let\s+', 'let ', code)
            code = re.sub(r'\bexport\s+function\s+', 'function ', code)
            code = re.sub(r'\bexport\s+default\s+', '', code)
            code = re.sub(r'\bexport\s*\{[\s\S]*?\};?', '', code)
            
            js_combined.append(f"/* ═════════ {jf} ═════════ */\n" + code)

# Replace CSS link
css_tag = '<link rel="stylesheet" href="css/style.css">'
html = html.replace(css_tag, f"<style>\n{css}\n</style>")

# Replace module script with bundled script
js_script_tag = '<script type="module" src="js/app.js"></script>'
bundled_js = "\n\n".join(js_combined)
html = html.replace(js_script_tag, f"<script>\n{bundled_js}\n</script>")

out_standalone = os.path.join(REPO_ROOT, "app", "standalone", "netgravity_standalone.html")
out_root_standalone = os.path.join(REPO_ROOT, "netgravity_standalone.html")

with open(out_standalone, "w", encoding="utf-8") as f:
    f.write(html)

with open(out_root_standalone, "w", encoding="utf-8") as f:
    f.write(html)

print(f"Successfully generated standalone HTML files at:\n  - {out_standalone}\n  - {out_root_standalone}")
