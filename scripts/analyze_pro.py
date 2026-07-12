#!/usr/bin/env python3
"""
takumi-youtube-edit / コア解析（clbs-youtube-edit analyze_pro.py の後継・日本語専用）

改善点（旧スキルからの差分）:
 1) 全カット境界をフレームグリッドに量子化し、seg_bounds をフレーム数の累積から算出。
    render 側の trim/atrim 方式と合わせて「計画タイムライン＝実レンダ」が恒等になり、
    select/aselect の量子化誤差蓄積（テロップ先行・音声遅れ）を根治する。
 2) テロップカードは 文（。！？）> 節（、）> BudouX文節 の階層で分割。
    節の途中でカードを割らない（節が3行以上になる場合のみ例外）。
    BudouXには句読点で割った後の節単位テキストを渡す（run-on長文を渡さない）。
 3) 禁則辞書 no_break_words.txt（skill assets / _factory / プロジェクト直下の和集合）で
    「ゼロポイントフィールド」等の固有語が行・カードで泣き別れるのを防ぐ。
 4) stable-ts 強制アライメント（台本既知の強みを活用）。無ければ Whisper+difflib に
    フォールバック。--timestamps-json（HeyGen TTS等）も従来通り使用可。
 5) --telop-lead 既定 0.0（強制アライメント前提。旧0.12はWhisper遅れの経験的補正だった）。
    --telop-min-dur による短尺カードの統合を実装（旧スキルでは未接続だった）。

出力: transcript.json / jetcut_plan.json / timing_plan.json / timing_report.md
      telop_source.srt / telop_jetcut.srt / telop_jetcut.json
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

FPS = 30.0  # main() で ffprobe 実測値に置換

HEADING_RE = re.compile(r"[\[【]\s*見出し\s*[:：]\s*(.+?)\s*[\]】]")
TAG_RE = re.compile(
    r"[\[【]\s*(スライド|ピクチャー|ピクチャ|Bロール|ビーロール|ボード|Board|カムリターン)\s*(\d+)?\s*[\]】]",
)
BREAK_RE = re.compile(r"<\s*break\b[^>]*>", re.I)
PAUSE_OLD_RE = re.compile(r"<#[\d.]+#>")

CLAUSE_PUNCT = "、，,"
SENT_PUNCT = "。．！？!?"


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


def find_ffprobe(ffmpeg: str) -> str | None:
    cand = Path(ffmpeg).resolve().parent / "ffprobe"
    if cand.exists():
        return str(cand)
    return shutil.which("ffprobe")


def probe_video(ffmpeg: str, path: Path) -> tuple[float, float]:
    """(fps, duration_sec) を実測する。HeyGenは25fps出力のことがあるため決め打ちしない。
    ffprobe が無い環境（ffmpeg-static等）では ffmpeg stderr から読む。"""
    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate:format=duration",
             "-of", "json", str(path)],
            text=True, capture_output=True)
        try:
            data = json.loads(proc.stdout or "{}")
            st = data["streams"][0]
            rate = st.get("avg_frame_rate") or st.get("r_frame_rate") or "30/1"
            if rate in ("0/0", "0"):
                rate = st.get("r_frame_rate") or "30/1"
            num, den = (rate.split("/") + ["1"])[:2]
            fps = float(num) / float(den or 1)
            duration = float(data["format"]["duration"])
            if 10.0 <= fps <= 121.0:
                return fps, duration
        except (KeyError, IndexError, ValueError, ZeroDivisionError):
            pass
    # フォールバック: ffmpeg -i の stderr（"30 fps" / "29.97 fps"）
    proc = subprocess.run([ffmpeg, "-i", str(path)], text=True, capture_output=True)
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", proc.stderr)
    fps = float(m.group(1)) if m else 30.0
    if not (10.0 <= fps <= 121.0):
        fps = 30.0
    if not m:
        print("[warn] fps検出失敗 → 30 と仮定", file=sys.stderr)
    return fps, get_duration_ffmpeg(ffmpeg, path)


def get_duration_ffmpeg(ffmpeg: str, path: Path) -> float:
    proc = subprocess.run([ffmpeg, "-i", str(path)], text=True, capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        print("ERROR: cannot read duration", file=sys.stderr)
        raise SystemExit(1)
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(ss)


def qframe(t: float) -> float:
    """時刻をフレームグリッド（1/FPS の倍数）へ量子化する（改善点1の要）。"""
    return round(t * FPS) / FPS


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\s　]+", "", text)
    text = re.sub(r"[「」『』“”\"'’‘、。，．・：:；;！？!?…—―ー\-〜~（）()\[\]【】<>＜＞/／&＆]", "", text)
    text = re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]", "", text)
    return text


def strip_markup(text: str) -> str:
    text = BREAK_RE.sub(" ", text)
    text = PAUSE_OLD_RE.sub(" ", text)
    return text


# ---------------- 台本解析 ----------------

@dataclass
class VisualEvent:
    type: str            # heading|slide|picture|broll|board|cam_return
    number: int | None
    text: str
    label: str
    script_pos: int


@dataclass
class TimedWord:
    norm: str
    start: float
    end: float
    t_start: int
    t_end: int


def parse_script(script_path: Path) -> tuple[str, list[VisualEvent]]:
    raw = script_path.read_text(encoding="utf-8")

    # narration_style.md ガード: `##` 見出し行が地の文に残っているとタイミングが崩壊する
    md_heads = [ln.strip() for ln in raw.splitlines() if ln.strip().startswith("#")]
    if md_heads:
        print(f"[warn] script.txt に Markdown見出し行が {len(md_heads)} 行残っています"
              f"（照合ズレの原因）: {md_heads[0][:30]}...", file=sys.stderr)

    raw = strip_markup(raw)
    combined = re.compile(
        r"(?P<heading>[\[【]\s*見出し\s*[:：]\s*(?P<htext>.+?)\s*[\]】])"
        r"|(?P<tag>[\[【]\s*(?P<ttype>スライド|ピクチャー|ピクチャ|Bロール|ビーロール|ボード|Board|カムリターン)\s*(?P<tnum>\d+)?\s*[\]】])"
    )
    speech_parts: list[str] = []
    events: list[VisualEvent] = []
    cursor = 0
    for m in combined.finditer(raw):
        before = raw[cursor:m.start()]
        speech_parts.append(before)
        pos = len(normalize_text("".join(speech_parts)))
        if m.group("heading"):
            events.append(VisualEvent("heading", None, m.group("htext").strip(), m.group("heading"), pos))
        else:
            t = m.group("ttype")
            typ = {
                "スライド": "slide", "ピクチャー": "picture", "ピクチャ": "picture",
                "Bロール": "broll", "ビーロール": "broll",
                "ボード": "board", "Board": "board", "カムリターン": "cam_return",
            }[t]
            num = int(m.group("tnum")) if m.group("tnum") else None
            events.append(VisualEvent(typ, num, "", m.group("tag"), pos))
        cursor = m.end()
    speech_parts.append(raw[cursor:])
    speech_text = "".join(speech_parts)

    counters = {"slide": 0, "picture": 0, "broll": 0, "board": 0}
    for e in events:
        if e.type in counters:
            counters[e.type] += 1
            if e.number is None:
                e.number = counters[e.type]
    return speech_text, events


# ---------------- 沈黙検出 → ジェットカット（フレーム量子化） ----------------

def detect_silences(ffmpeg: str, path: Path, noise_db: float, min_d: float) -> list[tuple[float, float]]:
    proc = subprocess.run(
        [ffmpeg, "-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"],
        text=True, capture_output=True,
    )
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", proc.stderr)]
    sil = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is not None and e > s:
            sil.append((s, e))
    return sil


def build_jetcut(silences: list[tuple[float, float]], total: float,
                 keep_tail: float, keep_lead: float, min_cut: float,
                 forced: list[tuple[float, float]] | None = None
                 ) -> tuple[list[tuple[float, float]], list[dict], list[float], float]:
    """沈黙を非対称に残して切る。全境界をフレームグリッドへ量子化（改善点1）。
    forced: keep_tail/keep_lead を適用しない強制カット区間（--extra-cuts 由来）。
    戻り値: removed, kept, seg_bounds(jetcutタイムライン・フレーム数累積), total_jc"""
    removed: list[tuple[float, float]] = []
    for s, e in silences:
        if e - s < min_cut:
            continue
        rs = qframe(s + keep_tail)
        re_ = qframe(e - keep_lead)
        if re_ - rs >= 1.0 / FPS - 1e-6:
            removed.append((rs, re_))
    for s, e in (forced or []):
        rs, re_ = qframe(s), qframe(e)
        if re_ - rs >= 1.0 / FPS - 1e-6:
            removed.append((rs, re_))
    removed.sort()
    # 重複区間のマージ（forcedが沈黙カットと重なるケース）
    _merged: list[list[float]] = []
    for rs, re_ in removed:
        if _merged and rs <= _merged[-1][1] + 1e-6:
            _merged[-1][1] = max(_merged[-1][1], re_)
        else:
            _merged.append([rs, re_])
    removed = [(a, b) for a, b in _merged]
    kept: list[dict] = []
    cur = 0.0
    for rs, re_ in removed:
        if rs > cur + 1e-6:
            kept.append({"start": round(cur, 6), "end": round(rs, 6)})
        cur = max(cur, re_)
    if cur < total - 1e-6:
        kept.append({"start": round(cur, 6), "end": round(total, 6)})

    # seg_bounds はフレーム数の累積から算出（float累積誤差なし・実レンダと恒等）
    seg_bounds = [0.0]
    acc_frames = 0
    for c in kept:
        acc_frames += int(round((c["end"] - c["start"]) * FPS))
        seg_bounds.append(round(acc_frames / FPS, 6))
    total_jc = seg_bounds[-1]
    return removed, kept, seg_bounds, total_jc


def clamp_silences_to_words(silences: list[tuple[float, float]],
                            word_spans: list[tuple[float, float]]
                            ) -> list[tuple[float, float]]:
    """アライメント済み語区間に食い込む無音を語端でクランプ（語尾・語頭切れ防止）。
    語が無音区間に完全内包される場合（検出器が小声を無音扱い）は無音を分割する。"""
    spans = sorted(word_spans)
    out: list[tuple[float, float]] = []
    for s, e in silences:
        segs = [[s, e]]
        for a, b in spans:
            if b <= s:
                continue
            if a >= e:
                break
            nxt: list[list[float]] = []
            for ss, ee in segs:
                if b <= ss or a >= ee:
                    nxt.append([ss, ee])
                    continue
                if ss < a:
                    nxt.append([ss, a])
                if b < ee:
                    nxt.append([b, ee])
            segs = nxt
        out.extend((ss, ee) for ss, ee in segs if ee - ss > 1e-3)
    return out


def make_src_to_jc(removed: list[tuple[float, float]]):
    def f(t: float) -> float:
        jc = t
        for rs, re_ in removed:
            if t >= re_:
                jc -= (re_ - rs)
            elif t > rs:
                jc -= (t - rs)
            else:
                break
        return max(0.0, round(jc, 6))
    return f


# ---------------- 文字起こし / 強制アライメント ----------------

def extract_wav(ffmpeg: str, video: Path, wav: Path) -> None:
    subprocess.run([ffmpeg, "-y", "-i", str(video), "-vn", "-acodec", "pcm_s16le",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True, capture_output=True)


ALIGN_GAP_MIN = 3.0        # 無転写ギャップとみなす語間の下限秒
ALIGN_GAP_NONSILENT = 2.0  # ギャップ中、検出済み沈黙で説明できない部分がこれ以上なら脱線と判定
ALIGN_MAX_REPAIR = 5       # 再アライメント修復の上限回数


def _align_words(data: dict) -> list[dict]:
    return [w for seg in data.get("segments", []) for w in seg.get("words", []) or []]


def find_untranscribed_gaps(words: list[dict], silences: list[tuple[float, float]],
                            total: float) -> list[tuple[float, float, int]]:
    """語タイミング列の無転写ギャップのうち、検出済み沈黙で説明できないものを返す。
    Fish音声の部分置換つなぎ目（スプライス）でVADがnonspeech誤判定すると、
    発話があるのに語が付かない大穴が空く（シュレ猫回: 630s以降に11〜38s）。
    戻り値: [(gap_start, gap_end, idx)] idx=ギャップ直後の語インデックス。"""
    cand: list[tuple[float, float, int]] = []
    prev = 0.0
    for i, w in enumerate(words):
        s = float(w["start"])
        if s - prev >= ALIGN_GAP_MIN:
            cand.append((prev, s, i))
        prev = max(prev, float(w["end"]))
    if total - prev >= ALIGN_GAP_MIN:
        cand.append((prev, total, len(words)))
    gaps = []
    for a, b, i in cand:
        nonsil = b - a
        for ss, se in silences:
            ov = min(b, se) - max(a, ss)
            if ov > 0:
                nonsil -= ov
        if nonsil >= ALIGN_GAP_NONSILENT:
            gaps.append((a, b, i))
    return gaps


def _merge_aligned(head_data: dict, cut_idx: int, tail_data: dict, offset: float) -> dict:
    """head_data の先頭 cut_idx 語＋（offset を加算した）tail_data を結合する。"""
    for seg in tail_data.get("segments", []):
        seg["start"] = round(float(seg.get("start", 0.0)) + offset, 3)
        seg["end"] = round(float(seg.get("end", 0.0)) + offset, 3)
        for w in seg.get("words", []) or []:
            w["start"] = round(float(w["start"]) + offset, 3)
            w["end"] = round(float(w["end"]) + offset, 3)
    segs = []
    n = 0
    for seg in head_data.get("segments", []):
        ws = seg.get("words", []) or []
        take = ws[: max(0, cut_idx - n)]
        n += len(ws)
        if take:
            s2 = dict(seg)
            s2["words"] = take
            s2["end"] = float(take[-1]["end"])
            s2["text"] = "".join(str(w.get("word", "")) for w in take)
            segs.append(s2)
        if n >= cut_idx:
            break
    return {"language": head_data.get("language", "ja"),
            "segments": segs + list(tail_data.get("segments", []))}


def align_stable_ts(ffmpeg: str, video: Path, speech_text: str, out_json: Path,
                    model_name: str, silences: list[tuple[float, float]],
                    total: float) -> dict | None:
    """stable-ts で台本テキストを音声へ強制アライメント（改善点4）。
    台本が既知なのでASR+difflibより語タイミングが一桁精密。失敗時は None。

    スプライスでVADが脱線すると無転写ギャップが空き、以降の全語が末尾へ縮退する。
    沈黙で説明できないギャップを検出したら、直前の語までを確定して残り音声＋残り
    テキストのみ再アライメント（チャンク修復）。修復しきれなければ None を返して
    Whisperフォールバックへ。壊れた結果は transcript.json に書かない。"""
    try:
        import stable_whisper
    except ImportError:
        return None
    wav = out_json.parent / "_audio16k.wav"
    tail_wav = out_json.parent / "_audio16k_tail.wav"
    extract_wav(ffmpeg, video, wav)
    print(f"[align] stable-ts forced alignment (model={model_name})")
    try:
        model = stable_whisper.load_model(model_name)
        text = re.sub(r"[ \t]+", " ", speech_text).strip()
        data = model.align(str(wav), text, language="ja").to_dict()
        last_cut = -1.0
        for attempt in range(ALIGN_MAX_REPAIR + 1):
            words = _align_words(data)
            if not words:
                print("[align] stable-ts returned no words; falling back to whisper",
                      file=sys.stderr)
                return None
            gaps = find_untranscribed_gaps(words, silences, total)
            if not gaps:
                break
            a, b, idx = gaps[0]
            rest_text = "".join(str(w.get("word", "")) for w in words[idx:]).strip()
            if attempt == ALIGN_MAX_REPAIR or a <= last_cut or idx <= 0 or not rest_text:
                print(f"[align] 無転写ギャップ {a:.1f}s->{b:.1f}s を修復できない"
                      "（アライメント脱線）→ whisperへフォールバック", file=sys.stderr)
                return None
            print(f"[align] 無転写ギャップ {a:.1f}s->{b:.1f}s（沈黙で説明不可）を検出 → "
                  f"{a:.2f}s 以降の残り{len(words) - idx}語を再アライメント "
                  f"({attempt + 1}/{ALIGN_MAX_REPAIR})")
            last_cut = a
            subprocess.run([ffmpeg, "-y", "-i", str(wav), "-ss", f"{a:.3f}",
                            "-acodec", "pcm_s16le", str(tail_wav)],
                           check=True, capture_output=True)
            tail = model.align(str(tail_wav), rest_text, language="ja").to_dict()
            data = _merge_aligned(data, idx, tail, a)
    except Exception as e:  # アライメント失敗はWhisperへフォールバック
        print(f"[align] stable-ts failed ({e}); falling back to whisper", file=sys.stderr)
        return None
    finally:
        for p in (wav, tail_wav):
            try:
                p.unlink()
            except OSError:
                pass
    data["source"] = "stable-ts-align"
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def transcribe(ffmpeg: str, video: Path, out_json: Path, model_name: str) -> dict:
    import whisper
    wav = out_json.parent / "_audio16k.wav"
    extract_wav(ffmpeg, video, wav)
    print(f"Loading Whisper: {model_name}")
    model = whisper.load_model(model_name)
    result = model.transcribe(str(wav), language="ja", fp16=False, verbose=False,
                              condition_on_previous_text=True, word_timestamps=True)
    result["source"] = "whisper-transcribe"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        wav.unlink()
    except OSError:
        pass
    return result


def load_word_result(ffmpeg: str, video: Path, out_json: Path, model_name: str,
                     speech_text: str, align_mode: str,
                     silences: list[tuple[float, float]], total: float) -> dict:
    """align_mode: auto|stable|whisper。キャッシュは align_mode と source が
    整合する時のみ再利用（--align whisper 明示時に stable-ts-align キャッシュを
    再利用すると、壊れた転写での再解析が固定化されてしまう）。"""
    want_stable = align_mode in ("auto", "stable")
    cached: dict | None = None
    if out_json.exists():
        cached = json.loads(out_json.read_text(encoding="utf-8"))
        segs = cached.get("segments", [])
        if not (segs and any(s.get("words") for s in segs)):
            cached = None
        elif abs(float(cached.get("meta_video_duration", -1)) - total) > 0.5:
            # 動画が差し替わった（尺不一致）のにキャッシュを使うと全カードが文字ズレする
            print("[transcript] cache video-duration mismatch -> invalidate & re-align")
            cached = None
    if cached is not None:
        src = cached.get("source", "whisper-transcribe")
        if align_mode == "whisper":
            if src == "whisper-transcribe":
                print(f"Reuse transcript ({src}): {out_json}")
                return cached
            print(f"[transcript] cache source={src} だが --align whisper 指定 → 無視してWhisperで再転写")
            cached = None
        elif src == "stable-ts-align":
            print(f"Reuse transcript ({src}): {out_json}")
            return cached
        else:
            print("[transcript] cache is whisper-transcribe; upgrading to stable-ts align")
    def _stamp(d: dict) -> dict:
        d["meta_video_duration"] = total
        try:
            out_json.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return d

    if want_stable:
        data = align_stable_ts(ffmpeg, video, speech_text, out_json, model_name,
                               silences, total)
        if data is not None:
            return _stamp(data)
        if align_mode == "stable":
            print("ERROR: --align stable but stable-ts unavailable/failed", file=sys.stderr)
            raise SystemExit(1)
        print("[align] stable-ts not available; using whisper+difflib fallback")
        # whisper-transcribe の word-level キャッシュのみ再利用可（cached は上で選別済み）
        if cached is not None:
            print(f"Reuse transcript (whisper cache): {out_json}")
            return cached
    return _stamp(transcribe(ffmpeg, video, out_json, model_name))


def words_from_timestamp_json(ts_path: Path, audio_offset: float = 0.0) -> tuple[str, list[TimedWord]]:
    data = json.loads(ts_path.read_text(encoding="utf-8"))
    raw_words = data.get("word_timestamps") or data.get("words") or []
    parts: list[str] = []
    words: list[TimedWord] = []
    pos = 0
    for w in raw_words:
        norm = normalize_text(str(w.get("word", "")))
        if not norm:
            continue
        s = float(w.get("start", 0.0)) + audio_offset
        e = float(w.get("end", s)) + audio_offset
        if e < s:
            e = s
        parts.append(norm)
        words.append(TimedWord(norm, s, e, pos, pos + len(norm)))
        pos += len(norm)
    return "".join(parts), words


def transcript_words(result: dict) -> tuple[str, list[TimedWord]]:
    parts: list[str] = []
    words: list[TimedWord] = []
    pos = 0
    for seg in result.get("segments", []):
        for w in seg.get("words", []) or []:
            norm = normalize_text(str(w.get("word", "")))
            if not norm:
                continue
            s = float(w.get("start", seg.get("start", 0.0)))
            e = float(w.get("end", seg.get("end", s)))
            if e < s:
                e = s
            parts.append(norm)
            words.append(TimedWord(norm, s, e, pos, pos + len(norm)))
            pos += len(norm)
    return "".join(parts), words


def word_for_transcript_index(words: list[TimedWord], idx: int) -> TimedWord:
    if not words:
        raise ValueError("empty word timestamp list")
    starts = [w.t_start for w in words]
    i = bisect.bisect_right(starts, idx) - 1
    i = min(max(i, 0), len(words) - 1)
    if idx >= words[i].t_end and i + 1 < len(words):
        return words[i + 1]
    return words[i]


def map_anchors(script_text: str, transcript_text: str) -> list[tuple[int, int]]:
    matcher = difflib.SequenceMatcher(None, script_text, transcript_text, autojunk=False)
    anchors = [(0, 0)]
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "replace") and i2 > i1 and j2 > j1:
            anchors.append((i1, j1))
            anchors.append((i2, j2))
    # 末尾アンカーは (len, len) のまま保持する。旧実装は len-1 に丸めており、
    # equalブロック内の補間が (L-1)/L に線形圧縮されて文頭文字が前の語へ
    # マップされるオフバイワンがあった（インデックスのクランプは参照側で行う）。
    anchors.append((len(script_text), len(transcript_text)))
    anchors = sorted(set(anchors))
    cleaned, max_t = [], -1
    for si, ti in anchors:
        ti = min(max(ti, max_t), len(transcript_text))
        cleaned.append((si, ti))
        max_t = ti
    return cleaned


def interp(anchors: list[tuple[int, int]], pos: int) -> int:
    if pos <= anchors[0][0]:
        return anchors[0][1]
    for i in range(1, len(anchors)):
        ls, lt = anchors[i - 1]
        rs, rt = anchors[i]
        if pos <= rs:
            if rs == ls:
                return rt
            return round(lt + (pos - ls) / (rs - ls) * (rt - lt))
    return anchors[-1][1]


# ---------------- 禁則辞書（改善点3） ----------------

NO_BREAK_WORDS: list[str] = []


def load_no_break_words(pdir: Path) -> list[str]:
    """skill assets → _factory（プロジェクトの親を遡って探索）→ プロジェクト直下 の和集合。"""
    words: set[str] = set()
    paths = [SKILL_DIR / "assets" / "no_break_words.txt"]
    for parent in [pdir, *pdir.parents]:
        cand = parent / "_factory" / "no_break_words.txt"
        if cand.exists():
            paths.append(cand)
            break
    paths.append(pdir / "no_break_words.txt")
    env = os.environ.get("TAKUMI_NO_BREAK_FILE")
    if env:
        paths.append(Path(env))
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            w = line.split("#", 1)[0].strip()
            if len(w) >= 2:
                words.add(w)
    return sorted(words, key=len, reverse=True)


def merge_tokens_by_dict(toks: list[str], words: list[str]) -> list[str]:
    """辞書語の内部に落ちるトークン境界を禁じてマージする。"""
    if not words or len(toks) <= 1:
        return toks
    text = "".join(toks)
    forbidden: set[int] = set()
    for w in words:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            forbidden.update(range(i + 1, i + len(w)))
            start = i + 1
    if not forbidden:
        return toks
    out: list[str] = []
    cur = ""
    off = 0
    for t in toks:
        cur += t
        off += len(t)
        if off in forbidden:
            continue
        out.append(cur)
        cur = ""
    if cur:
        out.append(cur)
    return out


# ---------------- テロップ改行（BudouX + DP、改善点2/3） ----------------

_BUDOUX = None


def _budoux_parse(sentence: str) -> list[str]:
    global _BUDOUX
    if _BUDOUX is None:
        try:
            import budoux
            _BUDOUX = budoux.load_default_japanese_parser()
        except Exception:
            _BUDOUX = False
    if _BUDOUX:
        return _BUDOUX.parse(sentence)
    return list(sentence)


MAX_LINE_CHARS = 17
ORPHAN_MIN = 4       # 最終行がこれ未満だと孤児行ペナルティ
ORPHAN_PENALTY = 60


def _dp_lines(text: str, max_chars: int | None = None) -> list[str]:
    """BudouX文節（辞書マージ済み）をDPで改行。
    - 最終行の余白コストは0（Knuth-Plass流。旧実装は最終行も詰めようとして歪んでいた）
    - 1〜3文字の孤児最終行にペナルティ
    - 行長は max_chars 厳守（辞書マージで単一トークンが超える場合のみ許容）"""
    mc = max_chars or MAX_LINE_CHARS
    if len(text) <= mc:
        return [text]
    toks = merge_tokens_by_dict(_budoux_parse(text), NO_BREAK_WORDS)
    n = len(toks)
    if n == 0:
        return [text]
    INF = float("inf")
    dp = [INF] * (n + 1)
    dp[n] = 0.0
    choice = [-1] * (n + 1)
    for i in range(n - 1, -1, -1):
        w = 0
        for j in range(i, n):
            w += len(toks[j])
            if w > mc and j > i:
                break
            if j + 1 == n:  # 最終行
                cost = 0.0 if w >= ORPHAN_MIN else (ORPHAN_MIN - w) * ORPHAN_PENALTY
                if w > mc:
                    cost += (w - mc) ** 2 * 10
            else:
                cost = float((mc - w) ** 2) if w <= mc else (w - mc) ** 2 * 10.0
            tot = cost + dp[j + 1]
            if tot < dp[i]:
                dp[i] = tot
                choice[i] = j + 1
    lines: list[str] = []
    idx = 0
    while idx < n:
        j = choice[idx]
        if j == -1:
            lines.append("".join(toks[idx:]))
            break
        lines.append("".join(toks[idx:j]))
        idx = j
    return lines


# ---------------- テロップカード構築 ----------------

@dataclass
class Unit:
    ch: str
    src_start: float
    src_end: float
    b: int = 0       # このcharの直後の境界強度: 0=なし / 2=節(、) / 3=文(。！？/改行)
    sent: int = 0    # 文ID


@dataclass
class Card:
    text: str
    units: list[Unit]
    seg: int
    sent: int
    head: bool = False
    js: float = 0.0
    je: float = 0.0

    @property
    def src_start(self) -> float:
        return min(u.src_start for u in self.units)

    @property
    def src_end(self) -> float:
        return max(u.src_end for u in self.units)

    def plain(self) -> str:
        return self.text.replace("\n", "")


def build_units(speech_text: str, norm_script: str, span_to_src) -> list[Unit]:
    units: list[Unit] = []
    script_pos = 0
    for ch in speech_text:
        if ch in CLAUSE_PUNCT:
            if units:
                units[-1].b = max(units[-1].b, 2)
            continue
        if ch in SENT_PUNCT or ch == "\n":
            if units:
                units[-1].b = max(units[-1].b, 3)
            continue
        if not ch.strip():
            continue
        norm = normalize_text(ch)
        if norm:
            display = unicodedata.normalize("NFKC", ch)
            chars = list(display) if len(display) == len(norm) else list(norm)
            for dch in chars:
                if script_pos >= len(norm_script):
                    break
                s, e = span_to_src(script_pos, script_pos + 1)
                units.append(Unit(dch, s, e))
                script_pos += 1
        elif ch == "ー" and units:
            last = units[-1]
            u = Unit("ー", last.src_start, last.src_end)
            units.append(u)
    sent = 0
    for u in units:
        u.sent = sent
        if u.b >= 3:
            sent += 1
    return units


def build_cards_for_segment(units: list[Unit], seg_i: int) -> list[Card]:
    """セグメント内: 節に割る → 節ごとにDP改行 → 文>節境界を尊重して2行カードへパック。"""
    # 節分割（b>=2 の直後で切る）
    clauses: list[list[Unit]] = []
    cur: list[Unit] = []
    for u in units:
        cur.append(u)
        if u.b >= 2:
            clauses.append(cur)
            cur = []
    if cur:
        clauses.append(cur)

    cards: list[Card] = []
    cur_lines: list[tuple[str, list[Unit]]] = []

    def flush():
        nonlocal cur_lines
        if cur_lines:
            text = "\n".join(t for t, _ in cur_lines)
            us = [u for _, lus in cur_lines for u in lus]
            cards.append(Card(text, us, seg_i, us[0].sent))
            cur_lines = []

    prev_sent_end = True
    for cl in clauses:
        text = "".join(u.ch for u in cl)
        if not text:
            prev_sent_end = cl[-1].b >= 3 if cl else prev_sent_end
            continue
        lines = _dp_lines(text)
        # 行→unitスライス対応（1 unit = 1表示文字）
        line_units: list[tuple[str, list[Unit]]] = []
        off = 0
        for ln in lines:
            line_units.append((ln, cl[off:off + len(ln)]))
            off += len(ln)
        # 文境界 or 2行超過なら現カードを確定
        if cur_lines and (prev_sent_end or len(cur_lines) + len(line_units) > 2):
            flush()
        # 節単体で3行以上 → 2行ずつ切る（例外的にカード分割を許可、境界はBudouX行）
        idx = 0
        while len(line_units) - idx > 2 - len(cur_lines):
            take = 2 - len(cur_lines)
            cur_lines += line_units[idx:idx + take]
            flush()
            idx += take
        cur_lines += line_units[idx:]
        if len(cur_lines) == 2:
            flush()
        prev_sent_end = cl[-1].b >= 3
    flush()
    return cards


def reflow_merge(a: Card, b: Card) -> Card | None:
    """短尺カードの統合（同セグメント・同文のみ）。2行に収まらなければ None。"""
    plain = a.plain() + b.plain()
    if len(plain) > MAX_LINE_CHARS * 2:
        return None
    lines = _dp_lines(plain)
    if len(lines) > 2:
        return None
    m = Card("\n".join(lines), a.units + b.units, a.seg, a.sent, head=a.head)
    m.js, m.je = min(a.js, b.js), max(a.je, b.je)
    return m


# ---------------- SRT ----------------

def format_srt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        s += 1; ms -= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(caps: list[dict], path: Path) -> None:
    out = []
    for i, c in enumerate(caps, 1):
        out += [str(i), f"{format_srt_time(c['start'])} --> {format_srt_time(c['end'])}", c["text"], ""]
    path.write_text("\n".join(out), encoding="utf-8")


def subtract_intervals(base: tuple[float, float], subs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result = [base]
    for ss, se in subs:
        new = []
        for bs, be in result:
            if se <= bs or ss >= be:
                new.append((bs, be))
            else:
                if ss > bs:
                    new.append((bs, ss))
                if se < be:
                    new.append((se, be))
        result = new
    return [(round(s, 6), round(e, 6)) for s, e in result if e - s > 0.05]


# ---------------- main ----------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--model", default="small")
    ap.add_argument("--align", choices=["auto", "stable", "whisper"], default="auto",
                    help="auto=stable-tsがあれば強制アライメント、無ければWhisper+difflib")
    ap.add_argument("--noise-db", type=float, default=-32.0)
    ap.add_argument("--language", choices=["ja", "en"], default="ja",
                    help="en=英語動画モード（レガシーエンジンへ委譲・ジェットカットなし）")
    ap.add_argument("--extra-cuts", default=None,
                    help="強制カット区間（srcタイムライン秒）'start-end,start-end'")
    ap.add_argument("--word-safe-cuts", action="store_true",
                    help="語区間に食い込む無音カットを語端でクランプ（語尾切れ防止）")
    ap.add_argument("--telop-lead", type=float, default=0.0,
                    help="非先頭カードの先行表示秒。強制アライメント前提の既定0.0")
    ap.add_argument("--telop-min-dur", type=float, default=1.0,
                    help="この秒数未満のカードは同文の隣接カードへ統合")
    ap.add_argument("--max-line-chars", type=int, default=17)
    ap.add_argument("--min-silence", type=float, default=0.30)
    ap.add_argument("--keep-tail", type=float, default=0.40)
    ap.add_argument("--keep-lead", type=float, default=0.08)
    ap.add_argument("--timestamps-json", default=None,
                    help="HeyGen TTS等の単語タイムスタンプJSON。指定時はWhisper/alignを実行しない")
    ap.add_argument("--audio-offset", type=float, default=0.0)
    args = ap.parse_args(argv)

    if args.language == "en":
        # ENはレガシーエンジン（takumi-youtube-edit-en STEP5-6 が依存する挙動を完全温存）
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import analyze_pro_legacy
        return analyze_pro_legacy.main(argv)


    global FPS, MAX_LINE_CHARS, NO_BREAK_WORDS
    MAX_LINE_CHARS = args.max_line_chars

    pdir = Path(args.project_dir).expanduser().resolve()
    video = pdir / "video.mp4"
    script = pdir / "script.txt"
    for p in (video, script):
        if not p.exists():
            print(f"ERROR: missing {p}", file=sys.stderr)
            return 1

    ffmpeg = find_ffmpeg()
    FPS, total = probe_video(ffmpeg, video)
    print(f"[info] duration={total:.2f}s fps={FPS:g}")

    NO_BREAK_WORDS = load_no_break_words(pdir)
    print(f"[dict] no-break words: {len(NO_BREAK_WORDS)}")

    # 1) 沈黙検出（カット境界の材料。build_jetcutは語タイミング取得後）
    silences = detect_silences(ffmpeg, video, args.noise_db, args.min_silence)

    # 2) 語タイミング取得（timestamps-json > stable-ts align > whisper）+ 照合
    speech_text, events = parse_script(script)
    norm_script = normalize_text(speech_text)
    if args.timestamps_json:
        ts_path = Path(args.timestamps_json).expanduser().resolve()
        if not ts_path.exists():
            print(f"ERROR: missing {ts_path}", file=sys.stderr)
            return 1
        print(f"[transcript] using word timestamps: {ts_path} (offset={args.audio_offset:+.3f}s)")
        tr_text, tr_words = words_from_timestamp_json(ts_path, args.audio_offset)
        (pdir / "transcript.json").write_text(json.dumps(
            {"source": "timestamps_json", "path": str(ts_path),
             "audio_offset": args.audio_offset}, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        result = load_word_result(ffmpeg, video, pdir / "transcript.json",
                                  args.model, speech_text, args.align,
                                  silences, total)
        tr_text, tr_words = transcript_words(result)
    if not tr_text or not tr_words:
        print("ERROR: empty word timestamps", file=sys.stderr)
        return 1
    anchors = map_anchors(norm_script, tr_text)
    match_ratio = difflib.SequenceMatcher(None, norm_script, tr_text, autojunk=False).ratio()
    print(f"[match] script vs words: {match_ratio*100:.1f}%")
    if match_ratio < 0.80:
        print("[warn] 照合率が低い（台本と音声の乖離大）。テロップ位置の精度が落ちます", file=sys.stderr)

    # 2.5) ジェットカット構築（word-safeクランプ・強制カット対応）
    if args.word_safe_cuts:
        n0 = len(silences)
        silences = clamp_silences_to_words(
            silences, [(w.start, w.end) for w in tr_words])
        print(f"[jetcut] word-safe clamp: silences {n0} -> {len(silences)}")
    forced: list[tuple[float, float]] = []
    if args.extra_cuts:
        for part in args.extra_cuts.split(","):
            a, b = part.split("-")
            forced.append((float(a), float(b)))
        print(f"[jetcut] extra-cuts: {forced}")
    removed, kept, seg_bounds, total_jc = build_jetcut(
        silences, total, args.keep_tail, args.keep_lead, args.min_silence,
        forced=forced)
    print(f"[jetcut] silences={len(silences)} removed={len(removed)} "
          f"source={total:.2f}s -> jetcut={total_jc:.2f}s (cut {total-total_jc:.2f}s)")
    src_to_jc = make_src_to_jc(removed)

    jetcut_plan = {
        "fps": FPS,
        "total_source": round(total, 6), "total_jetcut": total_jc,
        "removed": [{"start": s, "end": e} for s, e in removed],
        "kept_chunks": kept,
        "seg_bounds": seg_bounds,
        "params": {"noise_db": args.noise_db, "min_silence": args.min_silence,
                   "keep_tail": args.keep_tail, "keep_lead": args.keep_lead,
                   "word_safe_cuts": bool(args.word_safe_cuts),
                   "extra_cuts": args.extra_cuts},
    }
    (pdir / "jetcut_plan.json").write_text(json.dumps(jetcut_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    def pos_to_src(pos: int) -> float:
        return word_for_transcript_index(tr_words, interp(anchors, pos)).start

    def span_to_src(pos_start: int, pos_end: int) -> tuple[float, float]:
        si = interp(anchors, pos_start)
        ei = interp(anchors, max(pos_end - 1, pos_start))
        sw = word_for_transcript_index(tr_words, si)
        ew = word_for_transcript_index(tr_words, ei)
        return sw.start, max(ew.end, sw.start + 0.05)

    def snap_to_segment(t: float, label: str = "") -> float:
        near = min(seg_bounds, key=lambda x: abs(x - t))
        if abs(near - t) > 1.0 and label:
            print(f"[warn] {label}: 境界スナップ距離が{abs(near-t):.2f}s（配置ズレの可能性）", file=sys.stderr)
        return round(near, 6)

    # 3) 素材・見出しイベント
    plan_events = []
    last_jc = 0.0
    for e in events:
        src = pos_to_src(e.script_pos)
        jc = max(snap_to_segment(src_to_jc(src), e.label), last_jc)
        plan_events.append({
            "type": e.type, "number": e.number, "text": e.text,
            "label": e.label, "src": round(src, 3), "jc_start": round(jc, 6),
        })
        last_jc = jc
    for i, it in enumerate(plan_events):
        if it["type"] in ("cam_return", "heading"):
            continue
        end = total_jc
        for nxt in plan_events[i + 1:]:
            if nxt["type"] in ("cam_return", "slide", "picture", "broll", "board"):
                end = nxt["jc_start"]
                break
        end = snap_to_segment(end)
        it["jc_end"] = round(min(max(end, it["jc_start"] + 0.3), total_jc), 6)

    # 4) テロップカード（文>節>文節の階層、改善点2）
    LEAD = args.telop_lead
    units_all = build_units(speech_text, norm_script, span_to_src)

    kept_starts = [float(c["start"]) for c in kept]
    kept_ends = [float(c["end"]) for c in kept]

    def kept_index_for_src(t: float) -> int:
        i = bisect.bisect_right(kept_starts, t + 0.001) - 1
        if 0 <= i < len(kept) and t <= kept_ends[i] + 0.001:
            return i
        jc = src_to_jc(t)
        i = bisect.bisect_right(seg_bounds, jc + 0.001) - 1
        return min(max(i, 0), len(kept) - 1)

    groups: list[list[Unit]] = [[] for _ in kept]
    last_seg = 0
    for u in units_all:
        seg_i = kept_index_for_src(max(u.src_start, u.src_end - 0.001))
        seg_i = max(seg_i, last_seg)  # 語タイミング揺れによる逆行を禁止
        groups[seg_i].append(u)
        last_seg = seg_i

    all_cards: list[Card] = []
    for seg_i, us in enumerate(groups):
        if not us:
            continue
        seg_start = seg_bounds[seg_i]
        seg_end = seg_bounds[seg_i + 1]
        cards = build_cards_for_segment(us, seg_i)
        for j, c in enumerate(cards):
            c.head = (j == 0)
            # 先頭カード=カット境界に固定（音声・素材・見出しと同一フレーム切替の契約）
            js = seg_start if c.head else max(seg_start, src_to_jc(c.src_start) - LEAD)
            js = min(js, max(seg_start, seg_end - 0.05))
            je = min(seg_end, max(src_to_jc(c.src_end), js + 0.4))
            c.js, c.je = round(js, 6), round(je, 6)
        # 短尺カードの統合（同文のみ・2行に収まる時のみ、改善点5）
        i = 0
        while i < len(cards) - 1:
            a, b = cards[i], cards[i + 1]
            short_a = (a.je - a.js) < args.telop_min_dur
            orphan_b = len(b.plain()) < 5
            if a.sent == b.sent and (short_a or orphan_b):
                m = reflow_merge(a, b)
                if m is not None:
                    cards[i:i + 2] = [m]
                    continue
            i += 1
        all_cards.extend(cards)

    caps_src = [{"text": c.text, "start": round(c.src_start, 3), "end": round(c.src_end, 3)}
                for c in all_cards]
    caps_jc = [{"text": c.text, "start": c.js, "end": c.je, "_head": c.head, "_seg": c.seg}
               for c in all_cards]

    caps_jc.sort(key=lambda x: (x["start"], x["_seg"], not x["_head"]))
    for i in range(1, len(caps_jc)):
        if not caps_jc[i]["_head"] and caps_jc[i]["start"] < caps_jc[i - 1]["start"]:
            caps_jc[i]["start"] = caps_jc[i - 1]["start"]
    for i, c in enumerate(caps_jc):
        if i + 1 < len(caps_jc):
            # 次カード開始まで表示（余韻中もテロップ維持→カット頭で切替）
            c["end"] = round(max(c["start"] + 0.3, caps_jc[i + 1]["start"]), 6)
        else:
            c["end"] = round(min(total_jc, max(c["end"], c["start"] + 0.6)), 6)
        c.pop("_seg", None)
        c.pop("_head", None)

    caps_src.sort(key=lambda x: x["start"])
    for i, c in enumerate(caps_src):
        if i + 1 < len(caps_src):
            c["end"] = round(max(c["start"] + 0.3, caps_src[i + 1]["start"]), 3)
        elif c["end"] <= c["start"]:
            c["end"] = round(c["start"] + 0.8, 3)

    write_srt(caps_src, pdir / "telop_source.srt")
    write_srt(caps_jc, pdir / "telop_jetcut.srt")
    (pdir / "telop_jetcut.json").write_text(json.dumps(caps_jc, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5) 見出しの可視区間（スライド/Bロール/ボード中は非表示）
    tel_starts = sorted(c["start"] for c in caps_jc)
    ALIGN_TOL = 0.8
    headings = [it for it in plan_events if it["type"] == "heading"]
    for h in headings:
        if tel_starts:
            near = min(tel_starts, key=lambda x: abs(x - h["jc_start"]))
            if abs(near - h["jc_start"]) <= ALIGN_TOL:
                h["jc_start"] = round(near, 6)
    fullscreen = [(it["jc_start"], it["jc_end"]) for it in plan_events
                  if it["type"] in ("slide", "broll", "board") and "jc_end" in it]
    for i, h in enumerate(headings):
        h_start = h["jc_start"]
        h_end = headings[i + 1]["jc_start"] if i + 1 < len(headings) else total_jc
        h["jc_end"] = round(h_end, 6)
        h["visible_intervals"] = subtract_intervals((h_start, h_end), fullscreen)

    # 5.5) 終端縮退サニティ: [match] はマッチしたブロックだけの比率なので
    # アライメント脱線（全イベントが末尾へ縮退）を検出できない。イベント側で見る。
    collapsed = [it for it in plan_events
                 if it["jc_start"] >= total_jc - 1.0
                 or (it["type"] != "heading" and "jc_end" in it
                     and it["jc_end"] - it["jc_start"] <= 0.31)]
    qa_fail = len(collapsed) >= 3
    if qa_fail:
        labels = ", ".join(it["label"] for it in collapsed[:5])
        print(f"[QA][FAIL] イベント{len(collapsed)}個が末尾縮退/ゼロ尺（{labels} ...）。"
              "語アライメント脱線の疑い。transcript.json を削除して --align whisper で"
              "再解析してください", file=sys.stderr)

    (pdir / "timing_plan.json").write_text(
        json.dumps({"fps": FPS, "total_jetcut": total_jc, "events": plan_events},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    # 6) レポート
    lines = ["# takumi-youtube-edit Timing Report", "",
             f"- source: {total:.2f}s / jetcut: {total_jc:.2f}s (cut {total-total_jc:.2f}s, {len(removed)} points)",
             f"- fps: {FPS:g} / word-timing: "
             + ("timestamps-json" if args.timestamps_json else "stable-ts/whisper (see transcript.json)"),
             f"- match ratio: {match_ratio*100:.1f}% / no-break dict: {len(NO_BREAK_WORDS)} words",
             f"- event collapse QA: {'FAIL (' + str(len(collapsed)) + ' events縮退 → --align whisper で再解析)' if qa_fail else 'ok'}",
             f"- silence params: noise={args.noise_db}dB min={args.min_silence}s "
             f"keep_tail={args.keep_tail}s keep_lead={args.keep_lead}s",
             "", "## Visual Events (jetcut timeline)", ""]
    for it in plan_events:
        if it["type"] == "heading":
            vis = ", ".join(f"{s:.1f}-{e:.1f}" for s, e in it.get("visible_intervals", []))
            lines.append(f"- 見出し「{it['text']}」 src={it['src']:.2f} 表示[{vis}]")
        elif it["type"] == "cam_return":
            lines.append(f"- カムリターン jc={it['jc_start']:.2f} (src={it['src']:.2f})")
        else:
            lab = f"{it['type']}{it['number']}" if it["number"] else it["type"]
            lines.append(f"- {lab} jc={it['jc_start']:.2f}->{it.get('jc_end', 0):.2f} (src={it['src']:.2f})")
    (pdir / "timing_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[telop] {len(caps_jc)} caption cards")
    print("\nDone:")
    for f in ["jetcut_plan.json", "transcript.json", "timing_plan.json", "timing_report.md",
              "telop_source.srt", "telop_jetcut.srt", "telop_jetcut.json"]:
        print(f"- {pdir / f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
