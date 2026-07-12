#!/usr/bin/env python3
"""
clbs-youtube-edit / コア解析
- ffmpeg silencedetect で「文間の沈黙」を検出し、ジェットカット計画を作る
  （タグは無発音なのでタグ検出はしない。沈黙だけ切って編集点を増やす）
- Whisper で文字起こし → difflib で台本本文と照合し、無発音タグの位置を逆算
- 見出しはスライド/Bロール全画面表示中は非表示にする
出力: transcript.json / jetcut_plan.json / timing_plan.json / timing_report.md
      telop_source.srt / telop_jetcut.srt
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

FPS = 30

# 言語モード（main で --language から設定。"ja" 既定 / "en" は英語動画モード）
LANG = "ja"

# 見出しは text を持つので別扱い。その他のタグ。
HEADING_RE = re.compile(r"[\[【]\s*見出し\s*[:：]\s*(.+?)\s*[\]】]")
TAG_RE = re.compile(
    r"[\[【]\s*(スライド|ピクチャー|ピクチャ|Bロール|ビーロール|ボード|Board|カムリターン)\s*(\d+)?\s*[\]】]",
)
BREAK_RE = re.compile(r"<\s*break\b[^>]*>", re.I)
PAUSE_OLD_RE = re.compile(r"<#[\d.]+#>")


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


def normalize_text(text: str) -> str:
    if LANG == "en":
        # 英語: 小文字化して英数字のみ残す（スペース・英語句読点 .,'"!?;:- 等は全除去）
        text = unicodedata.normalize("NFKC", text).lower()
        return re.sub(r"[^0-9a-z]", "", text)
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\s　]+", "", text)
    text = re.sub(r"[「」『』“”\"'’‘、。，．・：:；;！？!?…—―ー\-〜~（）()\[\]【】<>＜＞/／&＆]", "", text)
    text = re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]", "", text)
    return text


def strip_markup(text: str) -> str:
    """break と旧沈黙マーカーを除去（タグ[..]は別途処理）"""
    text = BREAK_RE.sub(" ", text)
    text = PAUSE_OLD_RE.sub(" ", text)
    return text


# ---------------- 台本解析 ----------------

@dataclass
class VisualEvent:
    type: str            # heading|slide|picture|broll|cam_return
    number: int | None
    text: str            # heading のテキスト
    label: str
    script_pos: int      # 正規化済み本文の文字インデックス


@dataclass
class TimedWord:
    norm: str
    start: float
    end: float
    t_start: int
    t_end: int


def parse_script(script_path: Path) -> tuple[str, list[VisualEvent]]:
    raw = script_path.read_text(encoding="utf-8")
    raw = strip_markup(raw)

    # heading とその他タグを一つの走査で拾う
    if LANG == "en":
        # 英語タグ: [Slide N] / [Picture N] / [B-Roll N]（BRoll/B Roll も許容）/
        #           [Cam Return] / [Heading: ...]
        combined = re.compile(
            r"(?P<heading>\[\s*Heading\s*[:：]\s*(?P<htext>.+?)\s*\])"
            r"|(?P<tag>\[\s*(?P<ttype>Slide|Picture|B[\s\-]?Roll|Board|Cam\s*Return)\s*(?P<tnum>\d+)?\s*\])",
            re.IGNORECASE,
        )
    else:
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
        elif LANG == "en":
            t = re.sub(r"[\s\-]", "", m.group("ttype")).lower()
            typ = {"slide": "slide", "picture": "picture", "broll": "broll",
                   "board": "board", "camreturn": "cam_return"}[t]
            num = int(m.group("tnum")) if m.group("tnum") else None
            events.append(VisualEvent(typ, num, "", m.group("tag"), pos))
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

    # 自動採番
    counters = {"slide": 0, "picture": 0, "broll": 0, "board": 0}
    for e in events:
        if e.type in counters:
            counters[e.type] += 1
            if e.number is None:
                e.number = counters[e.type]
    return speech_text, events


# ---------------- 沈黙検出 → ジェットカット ----------------

def get_duration(ffmpeg: str, path: Path) -> float:
    proc = subprocess.run([ffmpeg, "-i", str(path)], text=True, capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        print("ERROR: cannot read duration", file=sys.stderr)
        raise SystemExit(1)
    h, mm, ss = m.groups()
    return int(h) * 3600 + int(mm) * 60 + float(ss)


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
                 keep_tail: float, keep_lead: float, min_cut: float) -> tuple[list[tuple[float, float]], list[dict], float]:
    """沈黙を非対称に残して切る。
    silence [s,e] の s=喋り終わり（余韻側）, e=次の喋り出し側。
    keep_tail: 文末の後に残す余韻（中高年向けに長め）
    keep_lead: 次の喋り出し前に残す間（短め＝テンポ維持）
    removed = 切る区間, kept = V1に残す区間"""
    removed: list[tuple[float, float]] = []
    for s, e in silences:
        dur = e - s
        if dur < min_cut:
            continue
        rs = s + keep_tail
        re_ = e - keep_lead
        if re_ > rs:
            removed.append((rs, re_))
        # 沈黙が keep_tail+keep_lead より短い場合は全く切らない（余韻を潰さない）
    removed.sort()
    # kept = [0,total] の補集合
    kept: list[dict] = []
    cur = 0.0
    for rs, re_ in removed:
        if rs > cur:
            kept.append({"start": round(cur, 3), "end": round(rs, 3)})
        cur = max(cur, re_)
    if cur < total:
        kept.append({"start": round(cur, 3), "end": round(total, 3)})
    removed_total = sum(re_ - rs for rs, re_ in removed)
    return removed, kept, round(total - removed_total, 3)


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
        return max(0.0, round(jc, 3))
    return f


# ---------------- Whisper + 照合 ----------------

def transcribe(ffmpeg: str, video: Path, out_json: Path, model_name: str, language: str = "ja") -> dict:
    if out_json.exists():
        cached = json.loads(out_json.read_text(encoding="utf-8"))
        segs = cached.get("segments", [])
        if segs and any(s.get("words") for s in segs):
            print(f"Reuse transcript (word-level): {out_json}")
            return cached
        # word-level タイムスタンプが無い旧キャッシュは作り直す（テロップ精度のため必須）
        print(f"[transcript] cache lacks word timestamps -> re-transcribing: {out_json}")
    import whisper
    wav = out_json.parent / "_audio16k.wav"
    subprocess.run([ffmpeg, "-y", "-i", str(video), "-vn", "-acodec", "pcm_s16le",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True, capture_output=True)
    print(f"Loading Whisper: {model_name}")
    model = whisper.load_model(model_name)
    result = model.transcribe(str(wav), language=language, fp16=False, verbose=False,
                              condition_on_previous_text=True, word_timestamps=True)
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        wav.unlink()
    except OSError:
        pass
    return result


def word_timestamps_char_times(ts_path: Path, audio_offset: float = 0.0) -> tuple[str, list[float]]:
    """HeyGen TTS等の単語タイムスタンプJSONを transcript 相当の
    （正規化文字列, 文字ごとの時刻）に変換する。単語内は線形補間。
    形式: {"word_timestamps":[{"word":"Hey,","start":0.119,"end":0.34}, ...]}"""
    data = json.loads(ts_path.read_text(encoding="utf-8"))
    words = data.get("word_timestamps") or data.get("words") or []
    chars: list[str] = []
    times: list[float] = []
    for w in words:
        norm = normalize_text(str(w.get("word", "")))
        if not norm:
            continue
        s = float(w.get("start", 0.0)) + audio_offset
        e = float(w.get("end", s)) + audio_offset
        span = max(e - s, 0.001)
        n = len(norm)
        for i, ch in enumerate(norm):
            chars.append(ch)
            times.append(s + span * (i / max(n, 1)))
    return "".join(chars), times


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


def transcript_char_times(result: dict) -> tuple[str, list[float]]:
    chars: list[str] = []
    times: list[float] = []
    last = 0.0
    for seg in result.get("segments", []):
        norm = normalize_text(seg.get("text", ""))
        if not norm:
            continue
        start = float(seg.get("start", last))
        end = float(seg.get("end", start))
        span = max(end - start, 0.001)
        n = len(norm)
        for i, ch in enumerate(norm):
            chars.append(ch)
            times.append(start + span * (i / max(n, 1)))
        last = end
    return "".join(chars), times


def transcript_char_times_wordlevel(result: dict) -> tuple[str, list[float]]:
    """Whisper の word-level timestamps から（正規化文字列, 文字ごとの時刻）を作る。
    char_rate（segment全体の線形補間）方式の置き換え（既知メモリ feedback_jetcut_wordlevel）。
    補間は word 内だけに閉じるので、テロップ境界が実発話時刻からずれにくい。
    words を持たない segment は segment 全体で補間（フォールバック）。"""
    chars: list[str] = []
    times: list[float] = []
    last = 0.0
    for seg in result.get("segments", []):
        words = seg.get("words") or []
        if words:
            for w in words:
                norm = normalize_text(str(w.get("word", "")))
                if not norm:
                    continue
                s = float(w.get("start", last))
                e = float(w.get("end", s))
                span = max(e - s, 0.001)
                n = len(norm)
                for i, ch in enumerate(norm):
                    chars.append(ch)
                    times.append(s + span * (i / max(n, 1)))
                last = e
        else:
            norm = normalize_text(seg.get("text", ""))
            if not norm:
                continue
            s = float(seg.get("start", last))
            e = float(seg.get("end", s))
            span = max(e - s, 0.001)
            n = len(norm)
            for i, ch in enumerate(norm):
                chars.append(ch)
                times.append(s + span * (i / max(n, 1)))
            last = e
    return "".join(chars), times


def map_anchors(script_text: str, transcript_text: str) -> list[tuple[int, int]]:
    matcher = difflib.SequenceMatcher(None, script_text, transcript_text, autojunk=False)
    anchors = [(0, 0)]
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "replace") and i2 > i1 and j2 > j1:
            anchors.append((i1, j1))
            anchors.append((i2, j2))
    anchors.append((len(script_text), max(len(transcript_text) - 1, 0)))
    anchors = sorted(set(anchors))
    cleaned, max_t = [], -1
    for si, ti in anchors:
        ti = min(max(ti, max_t), max(len(transcript_text) - 1, 0))
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


# ---------------- テロップ ----------------

@dataclass
class CaptionBlock:
    text: str
    s_start: int
    s_end: int
    sent_final: bool = False   # 文（。！？）の最終節の最終ブロックか（短尺なら後方統合する）


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
    return list(sentence)  # フォールバック（文字単位）


MAX_LINE_CHARS = 17
# 文頭フィラー（節の先頭にあるときだけ削除）。「で」=「それで」の略など。
_LEADING_FILLERS = ["それで", "でも", "でで", "で", "あの", "あのー", "えーと", "えっと",
                    "えー", "えっ", "まあ", "まぁ", "なんか", "こう", "そのー", "その"]


def _strip_leading_filler(s: str) -> str:
    """節の先頭にあるフィラーを1つだけ除去（読点直後の「で、」等）。"""
    t = s.lstrip()
    for f in sorted(_LEADING_FILLERS, key=len, reverse=True):
        if t.startswith(f):
            rest = t[len(f):]
            # フィラー直後が読点/空なら除去（助詞の「で」を誤除去しないため）
            if rest == "" or rest[0] in "、，,":
                return rest.lstrip("、，, ")
    return s


_PUNCT_RE = re.compile(r"[、。，．・…！？!?；;：:「」『』（）()　]")


def _strip_telop_punct(s: str) -> str:
    """テロップ表示用に句読点・記号を全削除（スペースは1つに）"""
    s = _PUNCT_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _dp_lines(text: str, max_chars: int = MAX_LINE_CHARS) -> list[str]:
    """BudouX文節トークン化 + DPで各行の余白二乗和を最小化（@hikarun_videoai方式）"""
    if len(text) <= max_chars:
        return [text]
    toks = _budoux_parse(text)
    n = len(toks)
    if n == 0:
        return [text]
    INF = float("inf")
    dp = [INF] * (n + 1); dp[n] = 0
    choice = [-1] * (n + 1)
    for i in range(n - 1, -1, -1):
        w = 0
        for j in range(i, n):
            w += len(toks[j])
            if w > max_chars * 1.2:   # 1行が長くなりすぎる前に打ち切り→2行に割れる
                break
            cost = (max_chars - w) ** 2 + dp[j + 1]
            if cost < dp[i]:
                dp[i] = cost; choice[i] = j + 1
    lines, idx = [], 0
    while idx < n:
        j = choice[idx]
        if j == -1:
            lines.append("".join(toks[idx:])); break
        lines.append("".join(toks[idx:j])); idx = j
    return lines


def split_captions(text: str) -> list[CaptionBlock]:
    """節（読点・句点）単位でブロック化。各節は独立してDP改行→2行に収め、
    節の途中ではブロックを割らない。文頭フィラー（で/あの等）は除去。
    句読点はテロップから全削除。"""
    blocks, cursor = [], 0
    for line in text.splitlines():
        stripped = line.strip("\u300c\u300d\" \t")
        if not stripped:
            continue
        # まず文（。！？）に割り、さらに読点（、，）で節に割る
        for sent in re.findall(r"[^\u3002\uff01\uff1f!?]+[\u3002\uff01\uff1f!?]?", stripped) or [stripped]:
            clauses = re.split(r"[\u3001\uff0c,]", sent)
            # \u6587\u306e\u300c\u6700\u5f8c\u306b\u5185\u5bb9\u3092\u6301\u3064\u7bc0\u300d\u306e\u30a4\u30f3\u30c7\u30c3\u30af\u30b9\uff08\uff1d\u6587\u672b\u7bc0\u3002\u77ed\u5c3a\u306a\u3089\u5f8c\u65b9\u7d71\u5408\u3059\u308b\uff09
            last_idx = -1
            for ci, cl in enumerate(clauses):
                if _strip_telop_punct(_strip_leading_filler(cl)):
                    last_idx = ci
            for ci, clause in enumerate(clauses):
                clause = _strip_leading_filler(clause)
                c = _strip_telop_punct(clause)
                if not c:
                    continue
                lines = _dp_lines(c)
                i = 0
                while i < len(lines):
                    block_text = "\n".join(lines[i:i + 2])
                    norm = normalize_text(block_text)
                    if norm:
                        s0 = cursor
                        cursor += len(norm)
                        is_last_block = (i + 2 >= len(lines))
                        blocks.append(CaptionBlock(
                            block_text, s0, cursor,
                            sent_final=(ci == last_idx and is_last_block)))
                    i += 2
    return blocks


EN_MAX_LINE_CHARS = 38


def _wrap_en(text: str, max_chars: int = EN_MAX_LINE_CHARS) -> list[str]:
    """英語: BudouXを使わず単語スペースで素朴に改行する。"""
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [text]


def split_captions_en(text: str) -> list[CaptionBlock]:
    """英語: 文（. ! ?）単位でブロック化し、単語スペースで改行→2行に収める。
    フィラー除去・句読点削除は行わない（英語字幕は句読点を残す）。"""
    blocks: list[CaptionBlock] = []
    cursor = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for sent in re.findall(r"[^.!?]+[.!?]*", stripped) or [stripped]:
            sent = sent.strip()
            if not sent:
                continue
            lines = _wrap_en(sent)
            i = 0
            while i < len(lines):
                block_text = "\n".join(lines[i:i + 2])
                norm = normalize_text(block_text)
                if norm:
                    s0 = cursor
                    cursor += len(norm)
                    blocks.append(CaptionBlock(block_text, s0, cursor))
                i += 2
    return blocks


def _join_reflow(a: str, b: str) -> str:
    """テロップ断片統合時の結合＋再改行（ja: BudouX DP / en: 単語スペース）。"""
    if LANG == "en":
        plain = f"{a.replace(chr(10), ' ')} {b.replace(chr(10), ' ')}".strip()
        plain = re.sub(r"\s+", " ", plain)
        return "\n".join(_wrap_en(plain))
    plain = a.replace("\n", "") + b.replace("\n", "")
    return "\n".join(_dp_lines(plain))


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


# ---------------- 区間ユーティリティ ----------------

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
    return [(round(s, 3), round(e, 3)) for s, e in result if e - s > 0.05]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--model", default="small")
    ap.add_argument("--noise-db", type=float, default=-32.0)
    ap.add_argument("--telop-lead", type=float, default=0.12,
                    help="テロップを音声より何秒早く出すか（先行表示）")
    ap.add_argument("--telop-min-dur", type=float, default=1.0,
                    help="この秒数未満のテロップ断片は次に統合（「あの感覚」等の重なり防止）")
    ap.add_argument("--min-silence", type=float, default=0.30, help="この長さ以上の沈黙を切る対象に")
    ap.add_argument("--keep-tail", type=float, default=0.40, help="文末の後に残す余韻（中高年向けに長め）")
    ap.add_argument("--keep-lead", type=float, default=0.08, help="次の喋り出し前に残す間（短め＝テンポ維持）")
    ap.add_argument("--language", choices=["ja", "en"], default="ja",
                    help="動画の言語。en=英語動画モード（ジェットカットなし・英語タグ・英語テロップ分割）")
    ap.add_argument("--timestamps-json", default=None,
                    help="HeyGen TTS等の単語タイムスタンプJSON。指定時はWhisperを実行せずこれを使う")
    ap.add_argument("--audio-offset", type=float, default=0.0,
                    help="動画先頭に対する音声タイムスタンプのオフセット秒（timestamps-json使用時）")
    args = ap.parse_args(argv)

    global LANG
    LANG = args.language

    pdir = Path(args.project_dir).expanduser().resolve()
    video = pdir / "video.mp4"
    script = pdir / "script.txt"
    for p in (video, script):
        if not p.exists():
            print(f"ERROR: missing {p}", file=sys.stderr)
            return 1

    ffmpeg = find_ffmpeg()
    total = get_duration(ffmpeg, video)
    print(f"[info] duration={total:.2f}s")

    # 1) 沈黙ジェットカット（en: 全スキップ。全区間を1チャンクで残し下流互換を保つ）
    if LANG == "en":
        silences = []
        removed = []
        kept = [{"start": 0.0, "end": round(total, 3)}]
        total_jc = round(total, 3)
        print(f"[jetcut] disabled (language=en) source={total:.2f}s -> jetcut={total_jc:.2f}s (cut 0.00s)")
    else:
        silences = detect_silences(ffmpeg, video, args.noise_db, args.min_silence)
        removed, kept, total_jc = build_jetcut(silences, total, args.keep_tail, args.keep_lead, args.min_silence)
        print(f"[jetcut] silences={len(silences)} removed={len(removed)} "
              f"source={total:.2f}s -> jetcut={total_jc:.2f}s (cut {total-total_jc:.2f}s)")
    src_to_jc = make_src_to_jc(removed)

    jetcut_plan = {
        "total_source": round(total, 3), "total_jetcut": total_jc,
        "removed": [{"start": round(s, 3), "end": round(e, 3)} for s, e in removed],
        "kept_chunks": kept,
        "params": {"noise_db": args.noise_db, "min_silence": args.min_silence, "keep_tail": args.keep_tail, "keep_lead": args.keep_lead},
    }
    if LANG == "en":
        jetcut_plan["params"]["language"] = "en"
        jetcut_plan["params"]["jetcut"] = "disabled"
    (pdir / "jetcut_plan.json").write_text(json.dumps(jetcut_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) Whisper（または単語タイムスタンプJSON）+ 照合
    speech_text, events = parse_script(script)
    norm_script = normalize_text(speech_text)
    tr_words: list[TimedWord] = []
    if args.timestamps_json:
        ts_path = Path(args.timestamps_json).expanduser().resolve()
        if not ts_path.exists():
            print(f"ERROR: missing {ts_path}", file=sys.stderr)
            return 1
        print(f"[transcript] using word timestamps: {ts_path} (offset={args.audio_offset:+.3f}s)")
        if LANG == "en":
            tr_text, tr_times = word_timestamps_char_times(ts_path, args.audio_offset)
        else:
            tr_text, tr_words = words_from_timestamp_json(ts_path, args.audio_offset)
            tr_times = [w.start for w in tr_words]
        # トレーサビリティ用に transcript.json も書き出す（Whisper互換ではなくソース記録）
        (pdir / "transcript.json").write_text(json.dumps(
            {"source": "timestamps_json", "path": str(ts_path),
             "audio_offset": args.audio_offset, "language": LANG},
            ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        result = transcribe(ffmpeg, video, pdir / "transcript.json", args.model, language=args.language)
        if LANG == "en":
            tr_text, tr_times = transcript_char_times(result)
        else:
            tr_text, tr_words = transcript_words(result)
            tr_times = [w.start for w in tr_words]
    if not tr_text:
        print("ERROR: empty transcript", file=sys.stderr)
        return 1
    if LANG != "en" and not tr_words:
        print("ERROR: empty word timestamps; cannot build word-based Japanese telops", file=sys.stderr)
        return 1
    anchors = map_anchors(norm_script, tr_text)

    def pos_to_src(pos: int) -> float:
        ti = interp(anchors, pos)
        if LANG != "en":
            return word_for_transcript_index(tr_words, ti).start
        ti = min(max(ti, 0), len(tr_times) - 1)
        return tr_times[ti]

    def span_to_src(pos_start: int, pos_end: int) -> tuple[float, float]:
        si = interp(anchors, pos_start)
        ei = interp(anchors, max(pos_end - 1, pos_start))
        sw = word_for_transcript_index(tr_words, si)
        ew = word_for_transcript_index(tr_words, ei)
        return sw.start, max(ew.end, sw.start + 0.05)

    # ジェットカット・セグメント境界（kept chunk 末端）を jetcut タイムラインで算出
    seg_bounds = [0.0]
    _acc = 0.0
    for c in kept:
        _acc += (c["end"] - c["start"])
        seg_bounds.append(round(_acc, 3))

    def snap_to_segment(t: float) -> float:
        return round(min(seg_bounds, key=lambda x: abs(x - t)), 3)

    # イベントの source / jetcut 時刻。開始もセグメント境界にスナップして
    # テロップ（同じ境界にスナップ）と同時に表示・切替させる。
    plan_events = []
    last_jc = 0.0
    for e in events:
        src = pos_to_src(e.script_pos)
        # en: セグメントが[0,total]の1個しかないためスナップすると0/末尾に飛ぶ → スナップしない
        jc_raw = round(src_to_jc(src), 3) if LANG == "en" else snap_to_segment(src_to_jc(src))
        jc = max(jc_raw, last_jc)
        plan_events.append({
            "type": e.type, "number": e.number, "text": e.text,
            "label": e.label, "src": round(src, 3), "jc_start": round(jc, 3),
        })
        last_jc = jc

    # 終了時刻（slide/picture/broll は次イベントまで or 末尾）→ セグメント末端にスナップ
    for i, it in enumerate(plan_events):
        if it["type"] in ("cam_return", "heading"):
            continue
        end = total_jc
        for nxt in plan_events[i + 1:]:
            if nxt["type"] in ("cam_return", "slide", "picture", "broll", "board"):
                end = nxt["jc_start"]
                break
        if LANG != "en":
            end = snap_to_segment(end)
        it["jc_end"] = round(min(max(end, it["jc_start"] + 0.3), total_jc), 3)

    # 3.5) テロップ（表示文字=台本本文、タイミング=Whisper word timestamps）
    LEAD = max(1.0 / FPS, args.telop_lead)
    cap_blocks = split_captions_en(speech_text) if LANG == "en" else split_captions(speech_text)

    if LANG == "en":
        # 英語パスは従来どおり。language=en はジェットカット無しなので境界固定を適用しない。
        caps_src: list[dict] = []
        for b in cap_blocks:
            si = min(max(interp(anchors, b.s_start), 0), len(tr_times) - 1)
            ei = min(max(interp(anchors, max(b.s_end - 1, b.s_start)), 0), len(tr_times) - 1)
            s = tr_times[si]
            e = min(total, max(tr_times[ei], s + 0.8))
            caps_src.append({"text": b.text, "start": round(s, 3), "end": round(e, 3)})

        caps_jc = []
        for c in caps_src:
            js = max(0.0, src_to_jc(c["start"]) - LEAD)
            je = max(src_to_jc(c["end"]), js + 0.4)
            caps_jc.append({"text": c["text"], "start": round(js, 3),
                            "end": round(min(je, total_jc), 3)})
        caps_jc.sort(key=lambda x: x["start"])
        for i in range(len(caps_jc) - 1):
            c = caps_jc[i]
            c["end"] = round(max(c["start"] + 0.3, caps_jc[i + 1]["start"]), 3)
        if caps_jc:
            caps_jc[-1]["end"] = round(min(total_jc, max(caps_jc[-1]["end"], caps_jc[-1]["start"] + 0.6)), 3)

        for c in caps_src:
            c["start"] = round(max(0.0, c["start"] - LEAD), 3)
        for i, c in enumerate(caps_src):
            if i + 1 < len(caps_src):
                c["end"] = round(max(c["start"] + 0.3, caps_src[i + 1]["start"]), 3)
            elif c["end"] <= c["start"]:
                c["end"] = round(c["start"] + 0.8, 3)
    else:
        kept_starts = [float(c["start"]) for c in kept]
        kept_ends = [float(c["end"]) for c in kept]

        def kept_index_for_src(t: float) -> int | None:
            i = bisect.bisect_right(kept_starts, t + 0.001) - 1
            if 0 <= i < len(kept) and t <= kept_ends[i] + 0.001:
                return i
            jc = src_to_jc(t)
            i = bisect.bisect_right(seg_bounds, jc + 0.001) - 1
            return min(max(i, 0), len(kept) - 1) if kept else None

        groups: list[list[dict]] = [[] for _ in kept]
        script_pos = 0
        last_unit: dict | None = None
        for ch in speech_text:
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
                    seg_i = kept_index_for_src(max(s, e - 0.001))
                    unit = {"ch": dch, "src_start": s, "src_end": e}
                    if seg_i is not None:
                        groups[seg_i].append(unit)
                        last_unit = unit
                    script_pos += 1
            elif ch == "ー" and last_unit is not None:
                # normalize_text では長音符を照合対象から外すが、表示上は残す。
                unit = dict(last_unit)
                unit["ch"] = ch
                seg_i = kept_index_for_src(max(unit["src_start"], unit["src_end"] - 0.001))
                if seg_i is not None:
                    groups[seg_i].append(unit)

        def split_segment_units(units: list[dict]) -> list[tuple[str, list[dict]]]:
            text = "".join(u["ch"] for u in units)
            if not text:
                return []
            lines = _dp_lines(text)
            chunks: list[tuple[str, list[dict]]] = []
            cursor = 0
            for i in range(0, len(lines), 2):
                block_lines = lines[i:i + 2]
                n = sum(len(line) for line in block_lines)
                chunk_units = units[cursor:cursor + n]
                cursor += n
                if chunk_units:
                    chunks.append(("\n".join(block_lines), chunk_units))
            return chunks

        caps_src = []
        caps_jc = []
        for seg_i, units in enumerate(groups):
            if not units:
                continue
            seg_start = seg_bounds[seg_i]
            seg_end = seg_bounds[seg_i + 1]
            for j, (text, chunk) in enumerate(split_segment_units(units)):
                if not text:
                    continue
                src_s = min(b["src_start"] for b in chunk)
                src_e = max(b["src_end"] for b in chunk)
                # 先頭ブロック=カット境界(seg_start)に固定（素材/見出しと同一フレーム切替の契約を守る）。
                # 非先頭ブロックは Whisper の word-start がやや遅れるため --telop-lead 分だけ前倒しして
                # 音声の喋り出しに合わせる（ただし seg_start より前には出さない）。
                js = seg_start if j == 0 else max(seg_start, src_to_jc(src_s) - LEAD)
                js = min(js, max(seg_start, seg_end - 0.05))
                je = min(seg_end, max(src_to_jc(src_e), js + 0.4))
                caps_src.append({"text": text, "start": round(src_s, 3), "end": round(src_e, 3)})
                caps_jc.append({"text": text, "start": round(js, 3), "end": round(je, 3),
                                "_seg": seg_i, "_head": j == 0})

        caps_jc.sort(key=lambda x: (x["start"], x["_seg"], not x["_head"]))
        for i in range(1, len(caps_jc)):
            prev = caps_jc[i - 1]
            cur = caps_jc[i]
            if cur["_head"]:
                continue
            if cur["start"] < prev["start"]:
                cur["start"] = prev["start"]

        for i, c in enumerate(caps_jc):
            if i + 1 < len(caps_jc):
                c["end"] = round(max(c["start"] + 0.3, caps_jc[i + 1]["start"]), 3)
            else:
                c["end"] = round(min(total_jc, max(c["end"], c["start"] + 0.6)), 3)
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

    # 3.6) 素材（picture/slide/broll）の開始を、ジェットカット・セグメント境界（カット頭）へスナップ。
    # → ユーザー要望「ピクチャー/スライドもメインカメラ切替の頭に揃えて出す」。
    #    セグメント中盤・カット前には絶対に出さない（テロップ開始ではなくカット境界に合わせる）。
    tel_starts = sorted(c["start"] for c in caps_jc)   # 見出し整列(後段)で使用
    ALIGN_TOL = 0.8                                     # 見出し整列(後段)で使用
    for it in plan_events:
        if it["type"] not in ("picture", "slide", "broll", "board"):
            continue
        it["jc_start"] = round(min(seg_bounds, key=lambda x: abs(x - it["jc_start"])), 3)

    # 終了を再計算（次イベント開始＝対応テロップ境界に揃う）
    for i, it in enumerate(plan_events):
        if it["type"] in ("cam_return", "heading"):
            continue
        end = total_jc
        for nxt in plan_events[i + 1:]:
            if nxt["type"] in ("cam_return", "slide", "picture", "broll", "board"):
                end = nxt["jc_start"]
                break
        if LANG != "en":
            end = min(seg_bounds, key=lambda x: abs(x - end))
        it["jc_end"] = round(min(max(end, it["jc_start"] + 0.3), total_jc), 3)

    # 見出しの可視区間（スライド/Bロール全画面中は非表示）＋ 見出し開始もテロップへ整列
    headings = [it for it in plan_events if it["type"] == "heading"]
    for h in headings:
        near = min(tel_starts, key=lambda x: abs(x - h["jc_start"]))
        if abs(near - h["jc_start"]) <= ALIGN_TOL:
            h["jc_start"] = round(near, 3)
    fullscreen = [(it["jc_start"], it["jc_end"]) for it in plan_events
                  if it["type"] in ("slide", "broll", "board") and "jc_end" in it]
    for i, h in enumerate(headings):
        h_start = h["jc_start"]
        h_end = headings[i + 1]["jc_start"] if i + 1 < len(headings) else total_jc
        h["jc_end"] = round(h_end, 3)
        h["visible_intervals"] = subtract_intervals((h_start, h_end), fullscreen)

    (pdir / "timing_plan.json").write_text(
        json.dumps({"total_jetcut": total_jc, "events": plan_events}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 4) レポート
    lines = ["# clbs-youtube-edit Timing Report", "",
             f"- source: {total:.2f}s / jetcut: {total_jc:.2f}s (cut {total-total_jc:.2f}s, {len(removed)} points)",
             f"- silence params: noise={args.noise_db}dB min={args.min_silence}s keep_tail={args.keep_tail}s keep_lead={args.keep_lead}s",
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

    print(f"[telop] {len(caps_jc)} caption blocks")
    print("\nDone:")
    for f in ["jetcut_plan.json", "transcript.json", "timing_plan.json", "timing_report.md",
              "telop_source.srt", "telop_jetcut.srt", "telop_jetcut.json"]:
        print(f"- {pdir / f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
