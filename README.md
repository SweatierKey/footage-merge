# footage-merge

Concatenate video files (e.g. the segments produced by
[`rtsp-record`](https://github.com/SweatierKey/rtsp-record)) into a single
output file. Wraps `ffmpeg`'s `concat` demuxer.

## Demo

![demo](demo.gif)

Watch with pause/seek on [asciinema.org](https://asciinema.org/a/ELohuueIMeNLuN7M).

## Install

    chmod +x footage-merge
    cp footage-merge ~/.local/bin/    # or /usr/local/bin/
    apt install ffmpeg

## Usage

Merge a list of segments by glob:

    footage-merge -o cam1-full.mkv cam1-20260426-*.mkv

Merge what `ls` finds (one path per line on stdin):

    ls cam1-20260426-*.mkv | footage-merge -o cam1-full.mkv

Inputs differ in codec/resolution → re-encode instead of stream-copying:

    footage-merge --reencode -o joined.mkv a.mkv b.mkv

Overwrite an existing output file:

    footage-merge -f -o existing.mkv a.mkv b.mkv

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `FILE...` (positional) | from stdin | input video files; **either** positional or stdin, never both |
| `-o`, `--output FILE` | (mandatory) | output file; binary content is never written to stdout |
| `-f`, `--force` | off | overwrite the output if it already exists |
| `--reencode` | off | re-encode with ffmpeg defaults (drops `-c copy`); needed when inputs differ in codec/resolution |
| `-v`, `--verbose` | off | let ffmpeg's stderr through, plus log a one-line summary on success |
| `-V`, `--version` | | print version and exit |
| `-h`, `--help` | | show help and exit |

### Behaviour

- All input files are checked for existence and read access **before** ffmpeg
  is invoked; if any are missing the script lists them and exits 1 without
  starting ffmpeg.
- The intermediate concat-list file is written into a `tempfile.NamedTemporaryFile`
  and removed in a `try/finally` block, even on error.
- `-c copy` is the default — preserves both quality and CPU. Use `--reencode`
  only when the inputs disagree on codec or resolution.
- Mixing positional file arguments with a real stdin pipe (FIFO) is rejected
  with a clear message. `/dev/null`, ttys and inherited stdins are not
  considered "stdin input", so running under cron/systemd does not trigger
  the check.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage error (missing `-o`, missing input file, output exists without `-f`, ffmpeg not in PATH, both modes used together) |
| 4 | ffmpeg exited with a non-zero status |
| 130 | interrupted with Ctrl-C |

## Dependencies

- Python 3.8+ (stdlib only)
- `ffmpeg` (any reasonably recent version with the `concat` demuxer)

## Place in the chain

`footage-merge` is the **last** script of the chain — it consumes the segments
that `rtsp-record` produces and emits a single playable file:

    onvif-discover → onvif-rtsp → go2rtc-gen → rtsp-play / rtsp-record → footage-merge
