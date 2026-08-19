import os
import re

frontend_dir = r"d:\Case Comp\Kearney\app\frontend"
html_path = os.path.join(frontend_dir, "index.html")
css_path = os.path.join(frontend_dir, "css", "style.css")
js_files = ["data.js", "map.js", "charts.js", "scenarios.js", "agent.js", "app.js"]

with open(html_path, "r", encoding="utf-8") as f:
    html = f.read()

with open(css_path, "r", encoding="utf-8") as f:
    css = f.read()

js_combined = []
for jf in js_files:
    p = os.path.join(frontend_dir, "js", jf)
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

out_root = r"d:\Case Comp\Kearney\netgravity_standalone.html"
out_frontend = os.path.join(frontend_dir, "netgravity_standalone.html")

with open(out_root, "w", encoding="utf-8") as f:
    f.write(html)

with open(out_frontend, "w", encoding="utf-8") as f:
    f.write(html)

print("Successfully regenerated standalone HTML files.")
