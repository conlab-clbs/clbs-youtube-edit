#!/usr/bin/env python3
"""
takumi-youtube-edit / 出荷前QA（新設）
final.mp4 を機械検証し qa_report.md を出力する。exit 1 = 出荷不可。

検証項目:
 A) 尺ドリフト: final.mp4 の実尺 vs timing_plan の total_jetcut（旧スキルの
    「テロップ先行・音声遅れ」はここが数秒ズレることで起きていた）
 B) 境界同期: 各ジェットカット境界で、実音声の喋り出し（silence_end）が
    「境界 + keep_lead」に一致しているか（許容±tolerance-frames）
 C) テロップ品質: 行長超過 / 表示時間過短 / タグ混入（[ 【 で始まる字幕、
    feedback_cc_srt_no_leading_tags 対応）/ 禁則辞書語の行またぎ
 D) 先頭カード契約: 各セグメント最初のカードの開始 == カット境界
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent


def ff() -> str:
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str | None:
    cand = Path(ff()).resolve().parent / "ffprobe"
    return str(cand) if cand.exists() else shutil.which("ffprobe")


def stream_durations(path: Path) -> dict:
    fp = ffprobe_bin()
    if fp:
        proc = subprocess.run(
            [fp, "-v", "error", "-show_entries",
             "stream=codec_type,duration:format=duration", "-of", "json", str(path)],
            text=True, capture_output=True)
        try:
            data = json.loads(proc.stdout or "{}")
            out = {"format": float(data.get("format", {}).get("duration", 0) or 0)}
            for st in data.get("streams", []):
                d = st.get("duration")
                if d is not None:
                    out[st.get("codec_type", "?")] = float(d)
            if out["format"] > 0:
                return out
        except (ValueError, KeyError):
            pass
    # フォールバック: ffmpeg -i stderr の Duration（コンテナ尺のみ）
    proc = subprocess.run([ff(), "-i", str(path)], text=True, capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    d = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    return {"format": d}


def detect_speech_onsets(video: Path, noise_db: float, min_d: float) -> list[float]:
    proc = subprocess.run(
        [ff(), "-i", str(video), "-map", "a:0",
         "-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"],
        text=True, capture_output=True)
    return [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", proc.stderr)]


def load_no_break_words(pdir: Path) -> list[str]:
    words: set[str] = set()
    paths = [SKILL_DIR / "assets" / "no_break_words.txt", pdir / "no_break_words.txt"]
    for parent in [pdir, *pdir.parents]:
        cand = parent / "_factory" / "no_break_words.txt"
        if cand.exists():
            paths.append(cand)
            break
    for p in paths:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                w = line.split("#", 1)[0].strip()
                if len(w) >= 2:
                    words.add(w)
    return sorted(words, key=len, reverse=True)


def _vis_len(line: str) -> float:
    """視覚幅: ASCII/半角=0.5、全角=1.0（英字混在テロップの誤検出防止）"""
    return sum(0.5 if ord(ch) < 0x3000 and ch != "\u3000" else 1.0 for ch in line)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--tolerance-frames", type=float, default=2.0,
                    help="境界同期の許容ズレ（フレーム数）")
    ap.add_argument("--noise-db", type=float, default=-32.0)
    ap.add_argument("--max-line-chars", type=int, default=17)
    ap.add_argument("--min-card-dur", type=float, default=0.55,
                    help="これ未満の表示時間のカードを警告")
    args = ap.parse_args(argv)

    pdir = Path(args.project_dir).expanduser().resolve()
    final = pdir / "final.mp4"
    jet = json.loads((pdir / "jetcut_plan.json").read_text(encoding="utf-8"))
    caps = json.loads((pdir / "telop_jetcut.json").read_text(encoding="utf-8"))
    if not final.exists():
        print(f"ERROR: missing {final}", file=sys.stderr)
        return 1

    fps = float(jet.get("fps", 30.0))
    tol = args.tolerance_frames / fps
    total_jc = float(jet["total_jetcut"])
    keep_lead = float(jet["params"].get("keep_lead", 0.08))
    kept = jet["kept_chunks"]
    seg_bounds = jet.get("seg_bounds")
    if not seg_bounds:
        seg_bounds = [0.0]
        acc = 0
        for c in kept:
            acc += int(round((c["end"] - c["start"]) * fps))
            seg_bounds.append(acc / fps)

    hard_fails: list[str] = []
    warns: list[str] = []
    lines = ["# takumi-youtube-edit QA Report", ""]

    # A) 尺ドリフト
    durs = stream_durations(final)
    v_dur = durs.get("video", durs["format"])
    a_dur = durs.get("audio", durs["format"])
    dv = v_dur - total_jc
    da = a_dur - total_jc
    lines += ["## A. 尺ドリフト", "",
              f"- plan total_jetcut: {total_jc:.3f}s",
              f"- final video: {v_dur:.3f}s (drift {dv*1000:+.0f}ms)",
              f"- final audio: {a_dur:.3f}s (drift {da*1000:+.0f}ms)", ""]
    # 音声はAACプライミング等で数十ms長くなるため映像より緩く見る
    if abs(dv) > max(tol, 1.5 / fps):
        hard_fails.append(f"映像尺ドリフト {dv*1000:+.0f}ms（許容 {tol*1000:.0f}ms）")
    if abs(da) > max(tol, 1.5 / fps) + 0.10:
        hard_fails.append(f"音声尺ドリフト {da*1000:+.0f}ms")

    # B) 境界同期（喋り出し = 境界 + keep_lead）
    onsets = detect_speech_onsets(final, args.noise_db, 0.15)
    lines += ["## B. カット境界の音声同期", ""]
    deltas = []
    fail_b = 0
    unverifiable = 0
    for b in seg_bounds[1:-1]:
        expected = b + keep_lead
        if not onsets:
            break
        # 期待位置の近傍ウィンドウ内のonsetのみ採用（遠方onsetとの偽比較を防ぐ）
        cand = [x for x in onsets if expected - 0.25 <= x <= expected + 0.6]
        if not cand:
            unverifiable += 1
            continue
        nearest = min(cand, key=lambda x: abs(x - expected))
        d = nearest - expected
        deltas.append(d)
        if abs(d) > tol:
            fail_b += 1
            lines.append(f"- NG boundary {b:.3f}s: onset {nearest:.3f}s (Δ{d*1000:+.0f}ms)")
    if deltas:
        lines.append(f"- 検証不可(近傍onsetなし)={unverifiable}")
        mx = max(abs(d) for d in deltas)
        avg = sum(deltas) / len(deltas)
        lines += ["", f"- boundaries={len(deltas)} maxΔ={mx*1000:.0f}ms meanΔ={avg*1000:+.0f}ms "
                  f"NG={fail_b} (tol ±{tol*1000:.0f}ms)", ""]
        if fail_b:
            hard_fails.append(f"境界同期NG {fail_b}箇所（maxΔ {mx*1000:.0f}ms）")
    else:
        lines += ["- （カット境界なし or 音声検出不可）", ""]
        if len(seg_bounds) > 2:
            warns.append("境界同期を検証できなかった（onset検出0件）")

    # C) テロップ品質
    lines += ["## C. テロップ品質", ""]
    words = load_no_break_words(pdir)
    n_long = n_short = n_tag = n_split = 0
    for i, c in enumerate(caps, 1):
        text = c["text"]
        cls = text.split("\n")
        for ln in cls:
            if _vis_len(ln) > args.max_line_chars + 1:
                n_long += 1
                lines.append(f"- 行長超過 #{i}「{ln}」({_vis_len(ln):.1f}字)")
        if (c["end"] - c["start"]) < args.min_card_dur:
            n_short += 1
            lines.append(f"- 表示過短 #{i} {c['end']-c['start']:.2f}s「{cls[0][:12]}…」")
        if re.match(r"^\s*[\[【]", text):
            n_tag += 1
            lines.append(f"- タグ混入 #{i}「{text[:20]}」")
        if len(cls) == 2:
            joined = "".join(cls)
            for w in words:
                p = joined.find(w)
                while p >= 0:
                    if p < len(cls[0]) < p + len(w):
                        n_split += 1
                        lines.append(f"- 禁則語の行またぎ #{i}「{w}」")
                        break
                    p = joined.find(w, p + 1)
    lines += ["", f"- 行長超過={n_long} 表示過短={n_short} タグ混入={n_tag} 禁則またぎ={n_split}", ""]
    if n_tag:
        hard_fails.append(f"テロップにタグ混入 {n_tag}件")
    if n_long:
        hard_fails.append(f"行長超過 {n_long}件")
    if n_split:
        warns.append(f"禁則語の行またぎ {n_split}件")
    if n_short:
        warns.append(f"表示時間過短 {n_short}件")

    # D) 先頭カード契約（各セグメント最初のカード開始 == 境界）
    lines += ["## D. 先頭カード契約", ""]
    fail_d = 0
    starts = sorted(c["start"] for c in caps)
    for si in range(len(seg_bounds) - 1):
        b, nb = seg_bounds[si], seg_bounds[si + 1]
        seg_caps = [s for s in starts if b - 1e-3 <= s < nb - 1e-3]
        if not seg_caps:
            continue
        if abs(seg_caps[0] - b) > 2e-3:
            fail_d += 1
            lines.append(f"- NG seg{si}: 先頭カード {seg_caps[0]:.3f}s ≠ 境界 {b:.3f}s")
    lines += ["", f"- NG={fail_d}", ""]
    if fail_d:
        hard_fails.append(f"先頭カードが境界に固定されていない {fail_d}セグメント")

    # まとめ
    verdict = "PASS" if not hard_fails else "FAIL"
    summary = [f"## 判定: {verdict}", ""]
    for f_ in hard_fails:
        summary.append(f"- FAIL: {f_}")
    for w_ in warns:
        summary.append(f"- WARN: {w_}")
    if not hard_fails and not warns:
        summary.append("- 問題なし")
    report = "\n".join(lines[:2] + summary + [""] + lines[2:]) + "\n"
    (pdir / "qa_report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"- {pdir / 'qa_report.md'}")
    return 1 if hard_fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
