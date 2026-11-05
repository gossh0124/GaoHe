#!/usr/bin/env bash
# Package the skill for claude.ai upload: the ZIP must contain the gaohe-probe/ folder at its root.
set -euo pipefail
cd "$(dirname "$0")"
rm -f gaohe-probe.zip
python3 - <<'PY'
import zipfile, pathlib
root = pathlib.Path('gaohe-probe')
with zipfile.ZipFile('gaohe-probe.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(root.rglob('*')):
        if p.is_file():
            z.write(p, p.as_posix())
print('wrote gaohe-probe.zip:', [i.filename for i in zipfile.ZipFile('gaohe-probe.zip').infolist()])
PY
