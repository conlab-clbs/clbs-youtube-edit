#!/usr/bin/env python3
"""
clbs-youtube-edit / チャプター連結ヘルパー
チャプター分割生成されたMP4群（ch01.mp4, ch02.mp4, ...）を1本の video.mp4 に連結する。
- 既定: ffmpeg concat demuxer（再エンコードなし。同一コーデック・同一解像度前提）
- 失敗時: concat filter で再エンコードにフォールバック

usage:
  python3 concat_chapters.py <chapters_dir> [-o output.mp4] [--pattern 'ch*.mp4']
  python3 concat_chapters.py ch01.mp4 ch02.mp4 ... [-o output.mp4]
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def find_ffmpeg() -> str:
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    for c in ["ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        p = shutil.which(c) if "/" not in c else (c if Path(c).exists() else None)
        if p:
            return p
    print("ERROR: ffmpeg not found", file=sys.stderr)
    raise SystemExit(1)


def concat_copy(ffmpeg: str, files: list[Path], out: Path) -> bool:
    """concat demuxer（-c copy・再エンコードなし）。成功なら True。"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for p in files:
            # concat demuxer のエスケープ（シングルクォートを '\'' に）
            esc = str(p.resolve()).replace("'", "'\\''")
            f.write(f"file '{esc}'\n")
        list_path = f.name
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", "-movflags", "+faststart", str(out)],
            text=True, capture_output=True)
        if proc.returncode != 0:
            print("[concat] stream copy failed (tail):", file=sys.stderr)
            print("\n".join(proc.stderr.splitlines()[-8:]), file=sys.stderr)
            return False
        return True
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


def concat_reencode(ffmpeg: str, files: list[Path], out: Path) -> bool:
    """concat filter で再エンコード（コーデック/解像度が混在していても連結できる）。"""
    inputs: list[str] = []
    for p in files:
        inputs += ["-i", str(p)]
    n = len(files)
    parts = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    fc = f"{parts}concat=n={n}:v=1:a=1[v][a]"
    preset = os.environ.get("CLBS_PRESET", "veryfast")
    crf = os.environ.get("CLBS_CRF", "21")
    proc = subprocess.run(
        [ffmpeg, "-y", *inputs, "-filter_complex", fc,
         "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", preset, "-crf", crf,
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", str(out)],
        text=True, capture_output=True)
    if proc.returncode != 0:
        print("FFMPEG ERROR (tail):", file=sys.stderr)
        print("\n".join(proc.stderr.splitlines()[-15:]), file=sys.stderr)
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="チャプターMP4群を1本に連結する")
    ap.add_argument("inputs", nargs="+",
                    help="チャプターMP4が入ったディレクトリ、またはMP4ファイル列")
    ap.add_argument("-o", "--output", default=None,
                    help="出力パス（既定: <dir>/video.mp4 または カレント/video.mp4）")
    ap.add_argument("--pattern", default="ch*.mp4",
                    help="ディレクトリ指定時のグロブ（既定: ch*.mp4）")
    args = ap.parse_args(argv)

    if len(args.inputs) == 1 and Path(args.inputs[0]).is_dir():
        d = Path(args.inputs[0]).expanduser().resolve()
        files = sorted(d.glob(args.pattern))
        default_out = d / "video.mp4"
    else:
        files = [Path(x).expanduser().resolve() for x in args.inputs]
        default_out = Path.cwd() / "video.mp4"
    files = [p for p in files if p.suffix.lower() == ".mp4" and p.exists()]
    out = Path(args.output).expanduser().resolve() if args.output else default_out
    files = [p for p in files if p.resolve() != out]

    if not files:
        print("ERROR: no input mp4 files", file=sys.stderr)
        return 1
    print(f"[concat] {len(files)} files -> {out}")
    for p in files:
        print(f"  - {p.name}")

    ffmpeg = find_ffmpeg()
    if concat_copy(ffmpeg, files, out):
        print(f"[concat] done (stream copy): {out}")
        return 0
    print("[concat] falling back to re-encode...")
    if concat_reencode(ffmpeg, files, out):
        print(f"[concat] done (re-encode): {out}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
