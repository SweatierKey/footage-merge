"""Tests for footage-merge. ffmpeg is mocked through PATH so the suite runs
on a headless box and never touches the network or real codecs."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "footage-merge"


def _load_module():
    loader = SourceFileLoader("footage_merge", str(SCRIPT))
    spec = importlib.util.spec_from_loader("footage_merge", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fm = _load_module()


# ---------------------------------------------------------------------------
# Fake ffmpeg: parses argv, reads the concat list, writes the output file by
# concatenating the bytes of the inputs (close enough for our assertions).
# ---------------------------------------------------------------------------

FAKE_FFMPEG = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, re, sys

    if os.environ.get("FAKE_FFMPEG_FAIL", "") == "1":
        sys.stderr.write("fake-ffmpeg: deliberate failure\\n")
        sys.exit(int(os.environ.get("FAKE_FFMPEG_EXIT", "5")))

    argv = sys.argv[1:]
    list_file = argv[argv.index("-i") + 1]
    output = argv[-1]

    paths = []
    with open(list_file) as f:
        for line in f:
            line = line.strip()
            m = re.match(r"file '(.*)'$", line)
            if m:
                # ffmpeg concat-list quote-escape: '\\'' -> '
                paths.append(m.group(1).replace("'\\\\''", "'"))

    with open(output, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                out.write(f.read())

    sys.exit(0)
    """)


class _FakeFfmpegPath:
    def __init__(self):
        self.dir = None
        self.old = None

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        ff = os.path.join(self.dir, "ffmpeg")
        with open(ff, "w") as f:
            f.write(FAKE_FFMPEG)
        os.chmod(ff, 0o755)
        self.old = os.environ.get("PATH")
        return self

    def env(self, **extra):
        e = dict(os.environ)
        e["PATH"] = self.dir + os.pathsep + (self.old or "")
        e.update(extra)
        return e

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)


def _run(args, env=None, stdin_text=None, cwd=None, timeout=15):
    kwargs = dict(
        capture_output=True, text=True,
        env=env, cwd=cwd, timeout=timeout,
    )
    if stdin_text is None:
        # If we don't pass `input=`, subprocess inherits the parent's stdin —
        # which under unittest is a pipe, not a tty. The script can't tell
        # that pipe is empty from one that's about to deliver paths, so
        # explicitly close it for the "no piped input" tests.
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin_text
    return subprocess.run([sys.executable, str(SCRIPT), *args], **kwargs)


def _make_input(d: str, name: str, payload: bytes) -> str:
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(payload)
    return p


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------

class MakeConcatListTests(unittest.TestCase):
    def test_basic(self):
        out = fm.make_concat_list(["/tmp/a.mkv", "/tmp/b.mkv"])
        self.assertEqual(out, "file '/tmp/a.mkv'\nfile '/tmp/b.mkv'\n")

    def test_quotes_in_path_escaped(self):
        # path with a single quote in it: ffmpeg's documented escape sequence.
        out = fm.make_concat_list(["/tmp/foo's bar.mkv"])
        self.assertEqual(out, "file '/tmp/foo'\\''s bar.mkv'\n")

    def test_relative_paths_become_absolute(self):
        out = fm.make_concat_list(["a.mkv"])
        self.assertTrue(out.startswith("file '/"))


class BuildFfmpegCmdTests(unittest.TestCase):
    def test_stream_copy_default(self):
        cmd = fm.build_ffmpeg_cmd("/tmp/list.txt", "/tmp/out.mkv", reencode=False)
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertEqual(cmd[cmd.index("-f") + 1], "concat")
        self.assertEqual(cmd[cmd.index("-safe") + 1], "0")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/tmp/list.txt")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertEqual(cmd[-1], "/tmp/out.mkv")

    def test_reencode_drops_codec_copy(self):
        cmd = fm.build_ffmpeg_cmd("/tmp/list.txt", "/tmp/out.mkv", reencode=True)
        self.assertNotIn("-c", cmd)


class ValidateInputsTests(unittest.TestCase):
    def test_empty_raises(self):
        with self.assertRaises(fm._Err):
            fm.validate_inputs([])

    def test_missing_file_raises_with_list(self):
        with tempfile.TemporaryDirectory() as d:
            ok = _make_input(d, "good.mkv", b"x")
            with self.assertRaises(fm._Err) as cm:
                fm.validate_inputs([ok, os.path.join(d, "no-such.mkv")])
            self.assertIn("no-such.mkv", cm.exception.msg)


# ---------------------------------------------------------------------------
# CLI integration with the fake ffmpeg
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):
    def test_positional_inputs_concat(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"AAAA")
            b = _make_input(d, "b.bin", b"BBBBBB")
            out = os.path.join(d, "out.bin")
            r = _run(["-o", out, a, b], env=fp.env())
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"AAAABBBBBB")

    def test_stdin_inputs_concat(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"X")
            b = _make_input(d, "b.bin", b"YY")
            out = os.path.join(d, "out.bin")
            r = _run(["-o", out],
                     env=fp.env(),
                     stdin_text=f"{a}\n{b}\n")
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"XYY")

    def test_combining_modes_rejected(self):
        # subprocess + input= sets stdin to a real PIPE (FIFO), so the
        # script's _stdin_is_real_pipe() returns True and it errors out
        # exactly as a `cat ... | footage-merge file.mkv` would.
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"x")
            r = _run(["-o", os.path.join(d, "out.bin"), a],
                     env=fp.env(),
                     stdin_text="anything\n")
            self.assertEqual(r.returncode, 1)
            self.assertIn("cannot combine", r.stderr)

    def test_positional_with_devnull_stdin_works(self):
        # Regression: stdin=/dev/null is NOT a pipe, so positional inputs
        # should be accepted (this is the systemd / cron / nohup case).
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"OK")
            out = os.path.join(d, "out.bin")
            r = _run(["-o", out, a], env=fp.env())  # stdin=DEVNULL by default
            self.assertEqual(r.returncode, 0, msg=r.stderr)

    def test_missing_input_listed(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            ok = _make_input(d, "good.bin", b"x")
            r = _run(["-o", os.path.join(d, "out.bin"),
                      ok, os.path.join(d, "missing.bin")],
                     env=fp.env())
            self.assertEqual(r.returncode, 1)
            self.assertIn("missing.bin", r.stderr)

    def test_output_exists_no_force(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"x")
            out = _make_input(d, "out.bin", b"already here")
            r = _run(["-o", out, a], env=fp.env())
            self.assertEqual(r.returncode, 1)
            self.assertIn("refusing to overwrite", r.stderr)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"already here")

    def test_output_exists_with_force(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"NEW")
            out = _make_input(d, "out.bin", b"OLD")
            r = _run(["-f", "-o", out, a], env=fp.env())
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), b"NEW")

    def test_output_missing_flag(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"x")
            r = _run([a], env=fp.env())
            self.assertEqual(r.returncode, 1)
            self.assertIn("-o/--output is required", r.stderr)

    def test_ffmpeg_failure_returns_4(self):
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"x")
            r = _run(["-o", os.path.join(d, "out.bin"), a],
                     env=fp.env(FAKE_FFMPEG_FAIL="1", FAKE_FFMPEG_EXIT="9"))
            self.assertEqual(r.returncode, 4)
            self.assertIn("ffmpeg exited with code", r.stderr)

    def test_ffmpeg_missing(self):
        env = {"PATH": ""}
        if "SystemRoot" in os.environ:
            env["SystemRoot"] = os.environ["SystemRoot"]
        with tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"x")
            r = _run(["-o", os.path.join(d, "out.bin"), a], env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("ffmpeg not found", r.stderr)

    def test_concat_list_is_cleaned_up(self):
        # The script writes a temp file in tempfile.gettempdir(); after a
        # successful run no footage-merge-*.txt should remain.
        with _FakeFfmpegPath() as fp, tempfile.TemporaryDirectory() as d:
            a = _make_input(d, "a.bin", b"x")
            tdir = tempfile.gettempdir()
            before = {p for p in os.listdir(tdir)
                      if p.startswith("footage-merge-")}
            r = _run(["-o", os.path.join(d, "out.bin"), a], env=fp.env())
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            after = {p for p in os.listdir(tdir)
                     if p.startswith("footage-merge-")}
            self.assertEqual(after - before, set(),
                             "leftover concat list file in tmp")


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

class MetaTests(unittest.TestCase):
    def test_version(self):
        r = _run(["-V"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"{fm.PROG} {fm.VERSION}")

    def test_help(self):
        r = _run(["-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("concat", r.stdout)


if __name__ == "__main__":
    unittest.main()
