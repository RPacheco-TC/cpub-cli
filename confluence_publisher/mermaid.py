"""Mermaid diagrams to PNG via ``npx @mermaid-js/mermaid-cli`` (used by :mod:`.storage`).

:class:`MermaidRenderer` holds the image folder and the ``npx`` to run (found by
``AppDirs.find_executable``); :class:`~.markdown_html.MarkdownRenderer` calls it
for each Mermaid block. A leaf: it imports nothing of the package.

:requires: ``npx`` for rendering (optional; without it a diagram stays a styled block).
"""
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path


class MermaidRenderer:
    """Render Mermaid diagrams to content-hash-named PNGs with ``npx mmdc``.

    :param img_dir: directory the PNGs are written to (and cached in).
    :param img_rel: path prefix used in the emitted ``<img src>``.
    :param runner: ``subprocess.run``-compatible callable (injectable for tests).
    :param npx: ``(command, child_env)`` from ``AppDirs.find_executable``, or
        ``None`` when npx was not found (then nothing is tried and :attr:`skipped`
        counts the diagrams left unrendered). Default: ``npx`` on ``PATH``.
    """

    def __init__(self, img_dir, img_rel: str = "img", runner=subprocess.run,
                 npx=("npx", None)):
        self.img_dir, self.img_rel, self.runner, self.npx = Path(img_dir), img_rel, runner, npx
        self.skipped = 0

    def render(self, mermaid_code: str) -> str | None:
        """Render Mermaid code to a PNG via ``mmdc``.

        The PNG is named by a hash of the diagram source and cached in ``img_dir``
        (an existing file is reused, not re-rendered).

        :param mermaid_code: the Mermaid diagram source.
        :type mermaid_code: str
        :returns: an ``<img>`` tag string, or ``None`` if rendering failed.
        :rtype: str | None
        """
        img_dir, img_rel = self.img_dir, self.img_rel
        digest = hashlib.md5(mermaid_code.encode()).hexdigest()[:10]
        filename = f"mermaid_{digest}.png"
        out_path = img_dir / filename

        if out_path.exists():
            return f'<img src="{img_rel}/{filename}" alt="Mermaid diagram" />'
        if self.npx is None:
            self.skipped += 1
            return None

        img_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.mmd', delete=False) as f:
            f.write(mermaid_code)
            tmp_mmd = f.name

        try:
            argv, env = self._command(tmp_mmd, out_path)
            result = self.runner(argv, capture_output=True, text=True, timeout=30, env=env)
            if result.returncode == 0 and out_path.exists():
                return f'<img src="{img_rel}/{filename}" alt="Mermaid diagram" />'
            else:
                print(f"    WARNING: mmdc failed: {result.stderr[:200]}")
                return None
        except Exception as e:
            print(f"    WARNING: mmdc error: {e}")
            return None
        finally:
            os.unlink(tmp_mmd)

    def _command(self, mmd_path, out_path):
        """The ``mmdc`` command line and the environment to run it with."""
        return ([self.npx[0], '-y', '-p', '@mermaid-js/mermaid-cli', 'mmdc',
                 '-i', str(mmd_path), '-o', str(out_path), '-b', 'white', '-s', '2', '-q'],
                self.npx[1])
